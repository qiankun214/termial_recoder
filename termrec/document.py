"""Assemble the final JSON document from the event spool."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

FORMAT = "termrec"
VERSION = 1


class DocumentError(RuntimeError):
    """Raised when the final document cannot be written."""


def finalize(
    *,
    target: Path,
    spool_path: Path,
    session: dict[str, Any],
    stats: dict[str, int],
    ended_at: str,
    duration_ms: int,
    end_reason: str,
    shell_exit_status: int | None,
) -> None:
    """Write *target* atomically, streaming events from the NDJSON spool.

    On success the spool is removed.  On failure the temporary file is removed,
    the spool is kept and :class:`DocumentError` is raised.
    """
    session = dict(session)
    session["ended_at"] = ended_at
    session["duration_ms"] = duration_ms
    session["end_reason"] = end_reason
    session["shell_exit_status"] = shell_exit_status

    header = {"format": FORMAT, "version": VERSION, "session": session, "stats": stats}
    tmp_path = target.with_name(target.name + ".tmp")

    try:
        with open(tmp_path, "w", encoding="utf-8") as out:
            prefix = json.dumps(header, ensure_ascii=False, indent=2)
            # ``prefix`` ends with "\n}": reopen the object to append "events".
            out.write(prefix[:-2])
            out.write(',\n  "events": [\n')
            first = True
            with open(spool_path, "r", encoding="utf-8") as spool:
                for line in spool:
                    record = line.rstrip("\n")
                    if not record:
                        continue
                    try:
                        json.loads(record)
                    except json.JSONDecodeError as exc:
                        raise DocumentError(
                            f"corrupt event spool {spool_path}: {exc}"
                        ) from exc
                    if not first:
                        out.write(",\n")
                    out.write("    " + record)
                    first = False
            out.write("\n  ]\n}\n" if not first else "]\n}\n")
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp_path, target)
    except DocumentError:
        _remove(tmp_path)
        raise
    except OSError as exc:
        _remove(tmp_path)
        raise DocumentError(
            f"cannot write {target}: {exc} (events kept in {spool_path})"
        ) from exc
    except Exception:
        _remove(tmp_path)
        raise

    _remove(spool_path)


def _remove(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass
