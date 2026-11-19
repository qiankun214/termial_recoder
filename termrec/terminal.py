"""Terminal helpers: raw mode and window size."""

from __future__ import annotations

import contextlib
import fcntl
import struct
import termios
import tty
from typing import Iterator

DEFAULT_ROWS = 24
DEFAULT_COLS = 80


def get_winsize(fd: int) -> tuple[int, int]:
    """Return ``(rows, columns)`` for the terminal behind *fd*."""
    try:
        packed = fcntl.ioctl(fd, termios.TIOCGWINSZ, b"\x00" * 8)
        rows, cols, _xpixel, _ypixel = struct.unpack("HHHH", packed)
    except OSError:
        return DEFAULT_ROWS, DEFAULT_COLS
    return (rows or DEFAULT_ROWS, cols or DEFAULT_COLS)


def set_winsize(fd: int, rows: int, cols: int) -> None:
    """Apply a window size to a pty (master or slave)."""
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


@contextlib.contextmanager
def raw_terminal(fd: int) -> Iterator[None]:
    """Put *fd* into raw mode and always restore the previous attributes.

    Raw mode disables echo and ``ISIG``, so keystrokes such as Ctrl-C are read
    as ordinary bytes and forwarded to the recorded shell's own terminal.
    """
    previous = termios.tcgetattr(fd)
    tty.setraw(fd, termios.TCSADRAIN)
    try:
        yield
    finally:
        try:
            termios.tcsetattr(fd, termios.TCSADRAIN, previous)
        except (OSError, termios.error):
            pass
