"""Event capture primitives: decoding, spooling and statistics.

The recorder never buffers a whole session in memory.  Every read from stdin
or from the pty master is decoded and appended to an NDJSON spool immediately,
which is also what makes a crashed session recoverable.
"""

from __future__ import annotations

import codecs
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, TextIO

Direction = Literal["input", "output"]


class TextDecoder:
    """Decode a byte stream as UTF-8, replacing invalid bytes.

    Terminal input/output can split a multi-byte character across reads and
    can contain arbitrary non-UTF-8 bytes.  An incremental decoder keeps an
    incomplete character until the rest arrives and substitutes U+FFFD for
    bytes that are not valid UTF-8, so the document is always valid JSON.
    """

    def __init__(self) -> None:
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")

    def decode(self, data: bytes) -> str:
        return self._decoder.decode(data)

    def flush(self) -> str:
        return self._decoder.decode(b"", final=True)


@dataclass
class Stats:
    events: int = 0
    input_events: int = 0
    output_events: int = 0
    input_bytes: int = 0
    output_bytes: int = 0

    def add(self, direction: Direction, raw_len: int) -> None:
        self.events += 1
        if direction == "input":
            self.input_events += 1
            self.input_bytes += raw_len
        else:
            self.output_events += 1
            self.output_bytes += raw_len

    def as_dict(self) -> dict[str, int]:
        return {
            "events": self.events,
            "input_events": self.input_events,
            "output_events": self.output_events,
            "input_bytes": self.input_bytes,
            "output_bytes": self.output_bytes,
        }


class EventSpool:
    """Append-only NDJSON spool: one event per line, flushed immediately."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._seq = 0
        self._file: TextIO = open(self.path, "a", encoding="utf-8")

    @property
    def seq(self) -> int:
        return self._seq

    def write(self, ts: float, direction: Direction, text: str) -> None:
        self._seq += 1
        record = {
            "seq": self._seq,
            "ts": round(max(ts, 0.0), 6),
            "dir": direction,
            "data": text,
        }
        self._file.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._file.flush()

    def close(self) -> None:
        if not self._file.closed:
            self._file.close()


class SessionLog:
    """Records raw chunks into an :class:`EventSpool` and keeps statistics."""

    _DIRECTIONS: tuple[Direction, Direction] = ("input", "output")

    def __init__(self, spool_path: Path) -> None:
        self.spool = EventSpool(spool_path)
        self.stats = Stats()
        self._decoders: dict[Direction, TextDecoder] = {
            direction: TextDecoder() for direction in self._DIRECTIONS
        }
        self._pending_bytes: dict[Direction, int] = {
            direction: 0 for direction in self._DIRECTIONS
        }

    def record(self, ts: float, direction: Direction, raw: bytes) -> None:
        pending = self._pending_bytes[direction] + len(raw)
        text = self._decoders[direction].decode(raw)
        if not text:
            # An incomplete multi-byte character: keep it for the next chunk.
            self._pending_bytes[direction] = pending
            return
        self.spool.write(ts, direction, text)
        self.stats.add(direction, pending)
        self._pending_bytes[direction] = 0

    def flush(self, ts: float) -> None:
        """Emit whatever the decoders still hold (truncated trailing bytes)."""
        for direction in self._DIRECTIONS:
            text = self._decoders[direction].flush()
            if text:
                pending = self._pending_bytes[direction]
                self.spool.write(ts, direction, text)
                self.stats.add(direction, pending)
                self._pending_bytes[direction] = 0

    def close(self) -> None:
        self.spool.close()
