"""Command line entry points: ``tr_start`` and ``tr_end``."""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

from . import __version__, recorder

SPOOL_SUFFIX = ".partial.jsonl"

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2


def _error(message: str) -> None:
    print(f"tr_start: {message}", file=sys.stderr)


def start_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="tr_start",
        description="Record an interactive bash session into a JSON file.",
    )
    parser.add_argument("target", metavar="TARGET", help="JSON file to create")
    parser.add_argument(
        "-f",
        "--force",
        action="store_true",
        help="overwrite TARGET and discard a stale spool",
    )
    parser.add_argument(
        "--shell",
        default=None,
        help="bash binary to record (default: bash on PATH)",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    args = parser.parse_args(argv)
    invocation = list(sys.argv) if argv is None else list(argv)

    shell = shutil.which(args.shell or "bash")
    if not shell:
        _error(f"cannot find bash ({args.shell or 'bash'}) on PATH")
        return EXIT_USAGE

    target = Path(args.target).expanduser().resolve()
    spool_path = target.with_name(target.name + SPOOL_SUFFIX)

    if target.exists() and target.is_dir():
        _error(f"target is a directory: {target}")
        return EXIT_USAGE
    if not target.parent.is_dir():
        _error(f"target directory does not exist: {target.parent}")
        return EXIT_USAGE
    if target.exists() and not args.force:
        _error(f"target already exists: {target} (use --force to overwrite)")
        return EXIT_USAGE
    if spool_path.exists():
        if not args.force:
            _error(
                f"unfinished recording spool exists: {spool_path} "
                "(use --force to discard it)"
            )
            return EXIT_USAGE
        try:
            spool_path.unlink()
        except OSError as exc:
            _error(f"cannot remove stale spool {spool_path}: {exc}")
            return EXIT_ERROR

    if os.environ.get(recorder.ACTIVE_ENV):
        _error("already inside a recording session (tr_start refused)")
        return EXIT_USAGE
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        _error("standard input and output must be a terminal")
        return EXIT_USAGE

    try:
        fd = os.open(spool_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(fd)
    except FileExistsError:
        _error(f"unfinished recording spool exists: {spool_path}")
        return EXIT_USAGE
    except OSError as exc:
        _error(f"cannot create spool file {spool_path}: {exc}")
        return EXIT_ERROR

    print(
        f"tr_start: recording to {target} - type tr_end to stop",
        file=sys.stderr,
    )

    try:
        result = recorder.run_session(
            target=target,
            spool_path=spool_path,
            shell=shell,
            invocation=invocation,
        )
    except Exception as exc:  # noqa: BLE001 - report instead of a traceback
        _error(f"recording failed: {exc}")
        return EXIT_ERROR

    if result.saved:
        print(
            f"tr_start: saved {result.stats.get('events', 0)} events "
            f"({result.end_reason}) -> {result.target}",
            file=sys.stderr,
        )
    else:
        _error(result.error or "recording was not saved")
        return EXIT_ERROR

    if result.error:
        _error(result.error)
        return EXIT_ERROR
    return EXIT_OK


def end_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="tr_end",
        description="Stop the active terminal recording session.",
    )
    parser.parse_args(sys.argv[1:] if argv is None else list(argv))

    raw_fd = os.environ.get(recorder.STOP_ENV)
    if not raw_fd or not os.environ.get(recorder.ACTIVE_ENV):
        print("tr_end: not inside a recording session", file=sys.stderr)
        return EXIT_ERROR
    try:
        fd = int(raw_fd)
        os.write(fd, b"x")
    except (TypeError, ValueError, OSError) as exc:
        print(f"tr_end: not inside a recording session ({exc})", file=sys.stderr)
        return EXIT_ERROR
    return EXIT_OK
