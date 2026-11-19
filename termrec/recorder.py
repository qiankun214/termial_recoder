"""Run an interactive shell behind a pty and record every byte exchanged.

``tr_start`` cannot observe the shell that launched it, so it starts a
dedicated nested ``bash`` on a fresh pty and proxies bytes both ways.  The
nested shell is a normal interactive bash (``bash -i``): the user's rc file,
prompt, aliases and job control all work as usual.

``tr_end`` ends the session through a bash function injected via the
environment (``BASH_FUNC_tr_end%%``) which writes one byte to an inherited
pipe.  A real ``tr_end`` executable provides the same signal as a fallback.
"""

from __future__ import annotations

import errno
import fcntl
import os
import platform
import pty
import pwd
import select
import signal
import socket
import sys
import termios
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from . import document, terminal
from .events import SessionLog

ACTIVE_ENV = "TERMREC_ACTIVE"
STOP_ENV = "TERMREC_STOP_FD"
TARGET_ENV = "TERMREC_TARGET"
SESSION_ENV = "TERMREC_SESSION_ID"

# Injected into the recorded bash.  A variable file-descriptor redirect is
# expanded by bash, so no hard-coded fd number is needed.
TR_END_FUNCTION = '() { { printf x >&"$TERMREC_STOP_FD"; } 2>/dev/null; exit 0; }'

READ_CHUNK = 64 * 1024
SELECT_INTERVAL = 0.2
PENDING_LIMIT = 1024 * 1024
SHUTDOWN_GRACE = 0.25
KILL_GRACE = 2.0
DRAIN_LIMIT = 0.25
FORCE_BREAK_GRACE = 2.0

STOP_FUNCTION_ENV = "BASH_FUNC_tr_end%%"


@dataclass
class SessionResult:
    target: Path
    end_reason: str
    stats: dict[str, int]
    shell_exit_status: int | None
    saved: bool
    error: str | None = None


class _Signals:
    """Flags set from signal handlers; all I/O stays in the pump loop."""

    def __init__(self) -> None:
        self.reason: str | None = None
        self.stop = False
        self.resized = False

    def on_stop_signal(self, signum: int, _frame: Any) -> None:
        if self.reason is None:
            self.reason = "signal:" + signal.Signals(signum).name
        self.stop = True

    def on_resize(self, _signum: int, _frame: Any) -> None:
        self.resized = True


def run_session(
    *,
    target: Path,
    spool_path: Path,
    shell: str,
    invocation: list[str],
) -> SessionResult:
    """Record one interactive shell session and write ``target``."""
    stdin_fd = sys.stdin.fileno()
    stdout_fd = sys.stdout.fileno()
    rows, cols = terminal.get_winsize(stdin_fd)
    session_id = _new_session_id()
    started_at = _now_iso()
    started_monotonic = time.monotonic()
    log = SessionLog(spool_path)
    session = _session_metadata(
        target=target,
        shell=shell,
        invocation=invocation,
        session_id=session_id,
        started_at=started_at,
        rows=rows,
        cols=cols,
    )

    state = _Signals()
    master_fd = -1
    slave_fd = -1
    stop_r = -1
    stop_w = -1
    child_pid = -1
    previous_handlers: dict[int, Any] = {}
    reaped = False
    shell_status: int | None = None
    reason = "shell_exit"
    failure: Exception | None = None

    try:
        previous_handlers = _install_handlers(state)
        # Enter raw mode before spawning the shell so keystrokes typed while
        # the session is starting are recorded verbatim instead of being
        # transformed by the outer terminal's line discipline.
        with terminal.raw_terminal(stdin_fd):
            stop_r, stop_w = os.pipe()
            os.set_inheritable(stop_w, True)

            env = dict(os.environ)
            env[ACTIVE_ENV] = "1"
            env[STOP_ENV] = str(stop_w)
            env[TARGET_ENV] = str(target)
            env[SESSION_ENV] = session_id
            env[STOP_FUNCTION_ENV] = TR_END_FUNCTION

            master_fd, slave_fd = pty.openpty()
            terminal.set_winsize(master_fd, rows, cols)
            child_pid = os.fork()
            if child_pid == 0:  # pragma: no cover - child process
                _child_exec(master_fd, slave_fd, shell, env)

            session["shell_pid"] = child_pid
            os.close(slave_fd)
            slave_fd = -1
            os.close(stop_w)
            stop_w = -1

            reason, shell_status, reaped = _pump(
                master_fd=master_fd,
                stdin_fd=stdin_fd,
                stdout_fd=stdout_fd,
                stop_r=stop_r,
                child_pid=child_pid,
                state=state,
                log=log,
                started_monotonic=started_monotonic,
            )
    except Exception as exc:
        failure = exc
        reason = "error:" + type(exc).__name__
    finally:
        for signum, handler in previous_handlers.items():
            try:
                signal.signal(signum, handler)
            except (OSError, ValueError):
                pass
        if child_pid > 0 and not reaped:
            _terminate(child_pid)
        for fd in (master_fd, slave_fd, stop_r, stop_w):
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass

    ended_monotonic = time.monotonic()
    ended_at = _now_iso()
    duration_ms = int(round((ended_monotonic - started_monotonic) * 1000))
    stats = log.stats.as_dict()
    saved = False
    save_error: str | None = None

    try:
        log.flush(ended_monotonic - started_monotonic)
        stats = log.stats.as_dict()
        log.close()
    except OSError as exc:
        save_error = f"cannot write event spool {spool_path}: {exc}"
        try:
            log.close()
        except OSError:
            pass

    if save_error is None:
        try:
            document.finalize(
                target=target,
                spool_path=spool_path,
                session=session,
                stats=stats,
                ended_at=ended_at,
                duration_ms=duration_ms,
                end_reason=reason,
                shell_exit_status=shell_status,
            )
            saved = True
        except Exception as exc:  # noqa: BLE001 - surface any save failure
            save_error = str(exc)

    if failure is not None:
        save_error = f"recording aborted by {failure!r}"

    return SessionResult(
        target=target,
        end_reason=reason,
        stats=stats,
        shell_exit_status=shell_status,
        saved=saved,
        error=save_error,
    )


# ---------------------------------------------------------------------------
# pty pump
# ---------------------------------------------------------------------------


def _pump(
    *,
    master_fd: int,
    stdin_fd: int,
    stdout_fd: int,
    stop_r: int,
    child_pid: int,
    state: _Signals,
    log: SessionLog,
    started_monotonic: float,
) -> tuple[str, int | None, bool]:
    """Proxy bytes until the session ends.

    Returns ``(end_reason, shell_exit_status, child_reaped)``.
    """
    to_master = bytearray()
    to_stdout = bytearray()
    master_eof = False
    stdin_open = True
    child_exited = False
    shell_status: int | None = None
    shutdown_started: float | None = None
    drain_deadline: float | None = None
    term_sent = False
    kill_sent = False
    stop_eof = False
    pty_closed_early = False
    pty_closed_deadline: float | None = None

    def elapsed() -> float:
        return time.monotonic() - started_monotonic

    def fdset() -> tuple[list[int], list[int]]:
        readable: list[int] = []
        writable: list[int] = []
        if stdin_open and not state.stop and not child_exited and len(to_master) < PENDING_LIMIT:
            readable.append(stdin_fd)
        if not master_eof:
            readable.append(master_fd)
        if not state.stop and not stop_eof:
            readable.append(stop_r)
        if to_stdout:
            writable.append(stdout_fd)
        if to_master and not master_eof and not child_exited:
            writable.append(master_fd)
        return readable, writable

    while True:
        now = time.monotonic()

        # --- reap the child periodically -----------------------------------
        if not child_exited:
            wpid, wstatus = _reap_nohang(child_pid)
            if wpid == child_pid:
                child_exited = True
                if os.WIFEXITED(wstatus):
                    shell_status = os.WEXITSTATUS(wstatus)
                drain_deadline = now + DRAIN_LIMIT
                to_master.clear()

        if state.stop and shutdown_started is None:
            shutdown_started = now

        # --- escalate against a shell that does not stop on demand ---------
        if state.stop and not child_exited and shutdown_started is not None:
            waited = now - shutdown_started
            if waited >= SHUTDOWN_GRACE and not term_sent:
                _signal_group(child_pid, signal.SIGTERM)
                term_sent = True
            if waited >= SHUTDOWN_GRACE + KILL_GRACE:
                if not kill_sent:
                    _signal_group(child_pid, signal.SIGKILL)
                    kill_sent = True
                if waited >= SHUTDOWN_GRACE + KILL_GRACE + FORCE_BREAK_GRACE:
                    master_eof = True
                    to_stdout.clear()
                    break

        # --- propagate window size changes ---------------------------------
        if state.resized:
            state.resized = False
            try:
                new_rows, new_cols = terminal.get_winsize(stdin_fd)
                terminal.set_winsize(master_fd, new_rows, new_cols)
            except OSError:
                pass

        # --- a shell that closed its terminal but keeps running -------------
        # The pty often reports EOF just before ``waitpid`` reaps the shell,
        # so give the child a short grace period to exit on its own before
        # treating the closed pty as the end of the session.
        if (
            pty_closed_early
            and not child_exited
            and not state.stop
            and pty_closed_deadline is not None
            and now >= pty_closed_deadline
        ):
            state.stop = True
            if state.reason is None:
                state.reason = "pty_eof"

        readable, writable = fdset()
        if not readable and not writable:
            break
        try:
            ready_r, ready_w, _ = select.select(readable, writable, [], SELECT_INTERVAL)
        except InterruptedError:
            continue
        except OSError as exc:
            if exc.errno == errno.EINTR:
                continue
            raise

        # --- stop byte (tr_end) --------------------------------------------
        if stop_r in ready_r:
            try:
                stop_data = os.read(stop_r, 64)
            except OSError:
                stop_data = b""
            if stop_data:
                state.stop = True
                if state.reason is None:
                    state.reason = "tr_end"
            else:
                stop_eof = True

        # --- user keystrokes ----------------------------------------------
        if stdin_fd in ready_r:
            try:
                data = os.read(stdin_fd, READ_CHUNK)
            except OSError as exc:
                if exc.errno == errno.EINTR:
                    data = None  # type: ignore[assignment]
                else:
                    data = b""
            if data == b"":
                stdin_open = False
                if not state.stop:
                    state.stop = True
                    if state.reason is None:
                        state.reason = "stdin_eof"
            elif data:
                log.record(elapsed(), "input", data)
                if not child_exited:
                    to_master.extend(data)

        # --- shell output --------------------------------------------------
        if master_fd in ready_r:
            try:
                data = os.read(master_fd, READ_CHUNK)
            except OSError as exc:
                if exc.errno == errno.EINTR:
                    data = None  # type: ignore[assignment]
                elif exc.errno in (errno.EIO, errno.EBADF):
                    data = b""
                else:
                    raise
            if data == b"":
                master_eof = True
                to_master.clear()
                if not child_exited and not state.stop:
                    pty_closed_early = True
                    if pty_closed_deadline is None:
                        pty_closed_deadline = time.monotonic() + SHUTDOWN_GRACE
            elif data:
                log.record(elapsed(), "output", data)
                to_stdout.extend(data)

        # --- relay to the user's terminal ----------------------------------
        if stdout_fd in ready_w and to_stdout:
            try:
                written = os.write(stdout_fd, bytes(to_stdout[:READ_CHUNK]))
            except OSError as exc:
                if exc.errno == errno.EINTR:
                    written = 0
                else:
                    written = -1
            if written < 0:
                to_stdout.clear()
                if not state.stop:
                    state.stop = True
                    if state.reason is None:
                        state.reason = "stdout_eof"
            elif written:
                del to_stdout[:written]

        # --- forward keystrokes to the shell -------------------------------
        if master_fd in ready_w and to_master and not child_exited:
            try:
                written = os.write(master_fd, bytes(to_master[:READ_CHUNK]))
            except OSError as exc:
                if exc.errno == errno.EINTR:
                    written = 0
                elif exc.errno in (errno.EIO, errno.EBADF):
                    written = -1
                else:
                    raise
            if written < 0:
                master_eof = True
                to_master.clear()
            elif written:
                del to_master[:written]

        # --- termination ---------------------------------------------------
        if child_exited:
            if drain_deadline is not None and time.monotonic() >= drain_deadline and not master_eof:
                master_eof = True
                to_master.clear()
            if not to_stdout and master_eof:
                break

    return state.reason or "shell_exit", shell_status, child_exited


# ---------------------------------------------------------------------------
# process / signal helpers
# ---------------------------------------------------------------------------


def _child_exec(master_fd: int, slave_fd: int, shell: str, env: dict[str, str]) -> None:
    """Child side of the fork: become the recorded interactive shell."""
    try:
        os.close(master_fd)
        os.setsid()
        fcntl.ioctl(slave_fd, termios.TIOCSCTTY, 0)
        os.dup2(slave_fd, 0)
        os.dup2(slave_fd, 1)
        os.dup2(slave_fd, 2)
        if slave_fd > 2:
            os.close(slave_fd)
        for signum in (
            signal.SIGWINCH,
            signal.SIGTERM,
            signal.SIGINT,
            signal.SIGHUP,
            signal.SIGCHLD,
            signal.SIGPIPE,
        ):
            signal.signal(signum, signal.SIG_DFL)
        os.execvpe(shell, [shell, "-i"], env)
    except BaseException as exc:  # noqa: BLE001 - must never return into parent
        try:
            os.write(2, f"tr_start: cannot start shell {shell}: {exc}\r\n".encode())
        except OSError:
            pass
        os._exit(127)


def _install_handlers(state: _Signals) -> dict[int, Any]:
    previous: dict[int, Any] = {}

    def install(signum: int, handler: Any) -> None:
        previous[signum] = signal.getsignal(signum)
        signal.signal(signum, handler)

    install(signal.SIGWINCH, state.on_resize)
    for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        install(signum, state.on_stop_signal)
    return previous


def _reap_nohang(pid: int) -> tuple[int, int]:
    try:
        return os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        return pid, 0


def _signal_group(pid: int, signum: int) -> None:
    try:
        os.killpg(pid, signum)
    except OSError:
        pass


def _terminate(pid: int) -> None:
    """Ensure a child shell is dead and reaped (bounded wait)."""
    _signal_group(pid, signal.SIGTERM)
    if _wait_reaped(pid, 1.0):
        return
    _signal_group(pid, signal.SIGKILL)
    _wait_reaped(pid, 2.0)


def _wait_reaped(pid: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        wpid, _status = _reap_nohang(pid)
        if wpid == pid:
            return True
        time.sleep(0.02)
    return False


# ---------------------------------------------------------------------------
# session metadata
# ---------------------------------------------------------------------------


def _new_session_id() -> str:
    return datetime.now().strftime("%Y%m%dT%H%M%S") + "-" + os.urandom(3).hex()


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="microseconds")


def _session_metadata(
    *,
    target: Path,
    shell: str,
    invocation: list[str],
    session_id: str,
    started_at: str,
    rows: int,
    cols: int,
) -> dict[str, Any]:
    try:
        user = pwd.getpwuid(os.getuid()).pw_name
    except (KeyError, OSError):
        user = os.environ.get("USER") or str(os.getuid())
    return {
        "id": session_id,
        "started_at": started_at,
        "ended_at": None,
        "duration_ms": None,
        "end_reason": None,
        "shell_exit_status": None,
        "target": str(target),
        "argv": [str(arg) for arg in invocation],
        "cwd": os.getcwd(),
        "user": user,
        "hostname": socket.gethostname(),
        "uname": platform.platform(),
        "python": platform.python_version(),
        "shell": shell,
        "shell_argv": [shell, "-i"],
        "shell_pid": None,
        "recorder_pid": os.getpid(),
        "terminal": {
            "term": os.environ.get("TERM"),
            "columns": cols,
            "rows": rows,
        },
    }
