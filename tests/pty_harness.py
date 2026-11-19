"""Tiny pty harness used by the integration tests.

The tests drive ``bin/tr_start`` exactly like a user would: a fresh pty is
created, the program is forked onto it, and the test reads/writes the pty
master while looking for expected output.
"""

from __future__ import annotations

import errno
import fcntl
import os
import pty
import select
import signal
import struct
import termios
import time
from pathlib import Path
from typing import Sequence

REPO_ROOT = Path(__file__).resolve().parent.parent
BIN_DIR = REPO_ROOT / "bin"


def build_env(home: Path) -> dict[str, str]:
    """A clean environment for a recorded session (no user rc, stable prompt)."""
    env = dict(os.environ)
    for key in [k for k in env if k.startswith("TERMREC_") or k.startswith("BASH_FUNC_")]:
        env.pop(key, None)
    env.pop("BASH_ENV", None)
    env.pop("ENV", None)
    env["HOME"] = str(home)
    env["PS1"] = "PROMPT> "
    env["TERM"] = "xterm"
    env["PATH"] = f"{BIN_DIR}{os.pathsep}{env.get('PATH', '')}"
    return env


class PtyProcess:
    """Run *argv* on a new pty and interact with it."""

    def __init__(
        self,
        argv: Sequence[str],
        env: dict[str, str] | None = None,
        cwd: str | Path | None = None,
        rows: int = 30,
        cols: int = 100,
    ) -> None:
        self.argv = [str(arg) for arg in argv]
        self._buffer = bytearray()
        self._position = 0
        self._eof = False
        self._status: int | None = None
        self._closed = False

        master, slave = pty.openpty()
        self.master = master
        fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        pid = os.fork()
        if pid == 0:  # pragma: no cover - child process
            try:
                os.close(master)
                os.setsid()
                fcntl.ioctl(slave, termios.TIOCSCTTY, 0)
                os.dup2(slave, 0)
                os.dup2(slave, 1)
                os.dup2(slave, 2)
                if slave > 2:
                    os.close(slave)
                if cwd is not None:
                    os.chdir(cwd)
                child_env = os.environ if env is None else env
                os.execvpe(self.argv[0], self.argv, child_env)
            except BaseException as exc:  # noqa: BLE001
                try:
                    os.write(2, f"harness: exec failed: {exc}\r\n".encode())
                except OSError:
                    pass
                os._exit(127)
        os.close(slave)
        self.pid = pid

    # -- lifecycle ---------------------------------------------------------

    def __enter__(self) -> "PtyProcess":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._status is None:
            self.kill()
        self.close()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            os.close(self.master)
        except OSError:
            pass

    def kill(self) -> None:
        if self._status is not None:
            return
        try:
            os.killpg(self.pid, signal.SIGKILL)
        except OSError:
            pass
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            wpid, status = self._wait_nohang()
            if wpid == self.pid:
                self._status = status
                return
            time.sleep(0.02)
        self._status = -1

    def _wait_nohang(self) -> tuple[int, int]:
        try:
            return os.waitpid(self.pid, os.WNOHANG)
        except ChildProcessError:
            return self.pid, 0

    # -- interaction -------------------------------------------------------

    def _read_some(self, timeout: float) -> None:
        if self._eof:
            return
        try:
            ready, _, _ = select.select([self.master], [], [], timeout)
        except InterruptedError:
            return
        if not ready:
            return
        try:
            data = os.read(self.master, 65536)
        except OSError as exc:
            if exc.errno == errno.EIO:
                data = b""
            else:
                raise
        if data:
            self._buffer.extend(data)
        else:
            self._eof = True

    def send(self, data: bytes) -> None:
        if self._eof:
            raise AssertionError("cannot send to a closed pty")
        view = memoryview(bytes(data))
        while view:
            written = os.write(self.master, view)
            view = view[written:]

    def expect(self, needle: bytes, timeout: float = 15.0) -> bytes:
        deadline = time.monotonic() + timeout
        while True:
            index = self._buffer.find(needle, self._position)
            if index >= 0:
                end = index + len(needle)
                segment = bytes(self._buffer[self._position:end])
                self._position = end
                return segment
            if self._eof:
                raise AssertionError(
                    f"pty closed while waiting for {needle!r}; "
                    f"tail={bytes(self._buffer[-2000:])!r}"
                )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError(
                    f"timed out after {timeout}s waiting for {needle!r}; "
                    f"tail={bytes(self._buffer[-2000:])!r}"
                )
            self._read_some(min(remaining, 0.2))

    def resize(self, rows: int, cols: int) -> None:
        fcntl.ioctl(
            self.master,
            termios.TIOCSWINSZ,
            struct.pack("HHHH", rows, cols, 0, 0),
        )

    def wait(self, timeout: float = 20.0) -> int:
        deadline = time.monotonic() + timeout
        while self._status is None:
            wpid, status = self._wait_nohang()
            if wpid == self.pid:
                self._status = status
                break
            if time.monotonic() > deadline:
                self.kill()
                raise AssertionError(
                    f"{self.argv[0]} did not exit within {timeout}s; "
                    f"tail={bytes(self._buffer[-3000:])!r}"
                )
            self._read_some(0.1)
        while not self._eof and time.monotonic() < deadline:
            self._read_some(0.0)
        return os.waitstatus_to_exitcode(self._status)

    def output_bytes(self) -> bytes:
        return bytes(self._buffer)
