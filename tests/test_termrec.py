"""Tests for the terminal recorder (stdlib ``unittest`` only)."""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from tests.pty_harness import BIN_DIR, PtyProcess, build_env
from termrec.document import finalize
from termrec.events import SessionLog, TextDecoder

SPOOL_SUFFIX = ".partial.jsonl"
READY_TOKEN = b"__TR_READY__"


def wait_ready(proc: PtyProcess, token: bytes = READY_TOKEN) -> None:
    """Wait until the recorded shell executes a command.

    Prompts are not used as synchronization points because system-wide bashrc
    files may redefine ``PS1``.  The echoed command and its output are both
    checked so the marker cannot be matched from the input echo alone.
    """
    command = b"echo " + token
    proc.send(command + b"\r")
    proc.expect(command + b"\r\n")
    proc.expect(token + b"\r\n")


def events(document: dict, direction: str) -> list[dict]:
    return [event for event in document["events"] if event["dir"] == direction]


def text(document: dict, direction: str) -> str:
    return "".join(event["data"] for event in events(document, direction))


class RecorderIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp(prefix="termrec-test-")
        self.addCleanup(shutil.rmtree, self.tmpdir, True)
        self.workdir = Path(self.tmpdir)
        self.home = self.workdir / "home"
        self.home.mkdir()
        self.target = self.workdir / "out.json"
        self.env = build_env(self.home)

    # -- helpers -----------------------------------------------------------

    def start(
        self,
        *extra: str,
        target: Path | None = None,
        rows: int = 30,
        cols: int = 100,
    ) -> PtyProcess:
        target = self.target if target is None else target
        return PtyProcess(
            [str(BIN_DIR / "tr_start"), *extra, str(target)],
            env=self.env,
            cwd=self.workdir,
            rows=rows,
            cols=cols,
        )

    def load_document(self, target: Path | None = None) -> dict:
        target = self.target if target is None else target
        self.assertTrue(target.exists(), f"missing document: {target}")
        with open(target, "r", encoding="utf-8") as stream:
            return json.load(stream)

    def spool_path(self, target: Path | None = None) -> Path:
        target = self.target if target is None else target
        return target.with_name(target.name + SPOOL_SUFFIX)

    def assert_clean(self, target: Path | None = None) -> None:
        target = self.target if target is None else target
        self.assertFalse(self.spool_path(target).exists(), "spool was not cleaned up")
        self.assertFalse(
            Path(str(target) + ".tmp").exists(), "temporary file was not cleaned up"
        )

    # -- tests -------------------------------------------------------------

    def test_records_input_output_and_writes_document(self) -> None:
        with self.start() as proc:
            wait_ready(proc)
            proc.send(b"echo hello-termrec\r")
            proc.expect(b"echo hello-termrec\r\n")
            proc.expect(b"hello-termrec\r\n")
            proc.send(b"tr_end\r")
            status = proc.wait()

        self.assertEqual(status, 0)
        document = self.load_document()
        session = document["session"]

        self.assertEqual(document["format"], "termrec")
        self.assertEqual(document["version"], 1)
        self.assertEqual(session["end_reason"], "tr_end")
        self.assertEqual(session["shell_exit_status"], 0)
        self.assertTrue(session["started_at"])
        self.assertTrue(session["ended_at"])
        self.assertIsInstance(session["duration_ms"], int)
        self.assertEqual(session["target"], str(self.target.resolve()))
        self.assertIn("tr_start", session["argv"][0])
        self.assertIn("bash", session["shell"])
        self.assertEqual(session["terminal"]["columns"], 100)
        self.assertEqual(session["terminal"]["rows"], 30)

        self.assertIn("echo hello-termrec", text(document, "input"))
        self.assertIn("echo hello-termrec\r", text(document, "input"))
        self.assertIn("hello-termrec", text(document, "output"))

        seqs = [event["seq"] for event in document["events"]]
        self.assertEqual(seqs, list(range(1, len(seqs) + 1)))
        timestamps = [event["ts"] for event in document["events"]]
        self.assertEqual(timestamps, sorted(timestamps))

        stats = document["stats"]
        self.assertEqual(stats["events"], len(document["events"]))
        self.assertEqual(stats["input_events"], len(events(document, "input")))
        self.assertEqual(stats["output_events"], len(events(document, "output")))
        self.assertGreater(stats["input_bytes"], 0)
        self.assertGreater(stats["output_bytes"], 0)
        self.assert_clean()

    def test_exit_command_ends_recording(self) -> None:
        with self.start() as proc:
            wait_ready(proc)
            proc.send(b"exit\r")
            status = proc.wait()

        self.assertEqual(status, 0)
        document = self.load_document()
        self.assertEqual(document["session"]["end_reason"], "shell_exit")
        self.assertEqual(document["session"]["shell_exit_status"], 0)
        self.assert_clean()

    def test_spool_exists_during_session(self) -> None:
        with self.start() as proc:
            wait_ready(proc)
            spool = self.spool_path()
            self.assertTrue(spool.exists(), "spool should exist while recording")
            self.assertGreater(spool.stat().st_size, 0)
            proc.send(b"tr_end\r")
            proc.wait()
        self.assertFalse(self.spool_path().exists())

    def test_existing_target_requires_force(self) -> None:
        self.target.write_text("ORIGINAL", encoding="utf-8")
        result = subprocess.run(
            [str(BIN_DIR / "tr_start"), str(self.target)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=self.env,
            cwd=self.workdir,
            timeout=30,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn(b"already exists", result.stderr)
        self.assertEqual(self.target.read_text(encoding="utf-8"), "ORIGINAL")

        with self.start("-f") as proc:
            wait_ready(proc)
            proc.send(b"tr_end\r")
            self.assertEqual(proc.wait(), 0)
        self.assertEqual(self.load_document()["session"]["end_reason"], "tr_end")
        self.assert_clean()

    def test_rejects_non_tty(self) -> None:
        result = subprocess.run(
            [str(BIN_DIR / "tr_start"), str(self.target)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=self.env,
            cwd=self.workdir,
            timeout=30,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn(b"terminal", result.stderr)
        self.assertFalse(self.target.exists())

    def test_nested_session_refused(self) -> None:
        inner = self.workdir / "inner.json"
        with self.start() as proc:
            wait_ready(proc)
            proc.send(f"tr_start {inner}\r".encode())
            proc.expect(b"already inside a recording session")
            proc.send(b"echo outer-still-records\r")
            proc.expect(b"echo outer-still-records\r\n")
            proc.expect(b"outer-still-records\r\n")
            proc.send(b"tr_end\r")
            status = proc.wait()

        self.assertEqual(status, 0)
        self.assertFalse(inner.exists())
        document = self.load_document()
        self.assertEqual(document["session"]["end_reason"], "tr_end")
        self.assertIn("outer-still-records", text(document, "output"))

    def test_tr_end_executable_fallback_stops_session(self) -> None:
        # ``command`` bypasses the injected bash function and uses PATH.
        with self.start() as proc:
            wait_ready(proc)
            proc.send(b"command tr_end\r")
            status = proc.wait()

        self.assertEqual(status, 0)
        document = self.load_document()
        self.assertEqual(document["session"]["end_reason"], "tr_end")
        self.assertIn("command tr_end", text(document, "input"))
        self.assert_clean()

    def test_ctrl_c_forwarded_and_recorded(self) -> None:
        started = time.monotonic()
        with self.start() as proc:
            wait_ready(proc)
            proc.send(b"sleep 30\r")
            proc.expect(b"sleep 30\r\n")
            time.sleep(0.3)
            proc.send(b"\x03")
            wait_ready(proc)
            proc.send(b"tr_end\r")
            status = proc.wait()
        elapsed = time.monotonic() - started

        self.assertEqual(status, 0)
        self.assertLess(elapsed, 15.0, "SIGINT was not forwarded to the shell")
        document = self.load_document()
        self.assertEqual(document["session"]["end_reason"], "tr_end")
        self.assertIn("\x03", text(document, "input"))

    def test_sigterm_finalizes_recording(self) -> None:
        with self.start() as proc:
            wait_ready(proc)
            os.kill(proc.pid, signal.SIGTERM)
            status = proc.wait()

        self.assertEqual(status, 0)
        document = self.load_document()
        self.assertEqual(document["session"]["end_reason"], "signal:SIGTERM")
        self.assert_clean()

    def test_invalid_utf8_output(self) -> None:
        with self.start() as proc:
            wait_ready(proc)
            proc.send(b"printf '\\377\\376'\r")
            proc.expect(b"printf '\\377\\376'\r\n")
            wait_ready(proc)
            proc.send(b"tr_end\r")
            status = proc.wait()

        self.assertEqual(status, 0)
        document = self.load_document()
        self.assertIn("\ufffd", text(document, "output"))
        self.assert_clean()

    def test_window_size_propagated(self) -> None:
        with self.start() as proc:
            wait_ready(proc)
            proc.send(b"stty size\r")
            proc.expect(b"30 100\r\n")
            proc.resize(40, 120)
            time.sleep(0.6)
            proc.send(b"stty size\r")
            proc.expect(b"40 120\r\n")
            proc.send(b"tr_end\r")
            self.assertEqual(proc.wait(), 0)

    def test_verbose_prompt_command_still_recorded(self) -> None:
        with self.start() as proc:
            wait_ready(proc)
            proc.send(b"for i in 1 2 3; do echo line-$i; done\r")
            proc.expect(b"line-3\r\n")
            proc.send(b"tr_end\r")
            self.assertEqual(proc.wait(), 0)

        output = text(self.load_document(), "output")
        for line in ("line-1", "line-2", "line-3"):
            self.assertIn(line, output)
        self.assertLess(output.index("line-1"), output.index("line-3"))


class DecoderTests(unittest.TestCase):
    def test_incremental_decoder_split_multibyte(self) -> None:
        decoder = TextDecoder()
        data = "你好".encode("utf-8")
        chunks = [data[0:1], data[1:3], data[3:5], data[5:6]]
        decoded = "".join(decoder.decode(chunk) for chunk in chunks)
        self.assertEqual(decoded, "你好")
        self.assertNotIn("\ufffd", decoded)
        self.assertEqual(decoder.flush(), "")

    def test_invalid_bytes_are_replaced(self) -> None:
        decoder = TextDecoder()
        self.assertEqual(decoder.decode(b"a\xffb"), "a\ufffdb")

    def test_trailing_partial_sequence_flushed(self) -> None:
        decoder = TextDecoder()
        self.assertEqual(decoder.decode(b"\xe4"), "")
        self.assertEqual(decoder.flush(), "\ufffd")


class DocumentTests(unittest.TestCase):
    def test_split_multibyte_bytes_are_counted_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            spool = Path(tmp) / "split.partial.jsonl"
            log = SessionLog(spool)
            data = "你".encode("utf-8")
            log.record(0.1, "output", data[:1])
            self.assertEqual(log.stats.output_events, 0)
            log.record(0.2, "output", data[1:])
            log.close()
            self.assertEqual(log.stats.events, 1)
            self.assertEqual(log.stats.output_events, 1)
            self.assertEqual(log.stats.output_bytes, len(data))
            self.assertEqual(log.stats.input_bytes, 0)

    def test_spool_and_document_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "session.json"
            spool = target.with_name(target.name + SPOOL_SUFFIX)
            log = SessionLog(spool)
            log.record(0.001, "output", b"PROMPT> ")
            log.record(0.500, "input", b"echo hi\r")
            log.record(0.700, "output", b"hi\r\nPROMPT> ")
            log.flush(1.0)
            stats = log.stats.as_dict()
            log.close()

            finalize(
                target=target,
                spool_path=spool,
                session={"id": "test", "started_at": "2025-01-01T00:00:00+00:00"},
                stats=stats,
                ended_at="2025-01-01T00:00:01+00:00",
                duration_ms=1000,
                end_reason="tr_end",
                shell_exit_status=0,
            )

            document = json.loads(target.read_text(encoding="utf-8"))
            self.assertEqual(document["format"], "termrec")
            self.assertEqual(document["session"]["end_reason"], "tr_end")
            self.assertEqual(document["stats"], stats)
            self.assertEqual([event["seq"] for event in document["events"]], [1, 2, 3])
            self.assertEqual(document["events"][0]["data"], "PROMPT> ")
            self.assertFalse(spool.exists())
            self.assertFalse(Path(str(target) + ".tmp").exists())


class TrEndExecutableTests(unittest.TestCase):
    def test_outside_session_reports_error(self) -> None:
        env = dict(os.environ)
        for key in [k for k in env if k.startswith("TERMREC_")]:
            env.pop(key, None)
        result = subprocess.run(
            [str(BIN_DIR / "tr_end")],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            timeout=30,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn(b"not inside a recording session", result.stderr)


if __name__ == "__main__":
    unittest.main()
