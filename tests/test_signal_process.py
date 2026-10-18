"""Harmless real-process test: only a local file writer, never phone tools."""
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from operation_signals import CancelRequested, OperationSignals


class SignalProcessTests(unittest.TestCase):
    def test_parent_sigint_does_not_kill_writer_before_journal(self):
        with tempfile.TemporaryDirectory(prefix=".test-balmunlock-", dir=ROOT) as folder:
            folder = Path(folder)
            writer = folder / "writer.py"
            result_file = folder / "result.txt"
            journal = folder / "journal.txt"
            writer.write_text("import pathlib, sys, time\ntime.sleep(0.25)\n"
                              "pathlib.Path(sys.argv[1]).write_text('complete')\n", encoding="utf-8")
            guard = OperationSignals()
            with guard:
                with guard.defer():
                    timer = threading.Timer(0.1, signal.raise_signal, args=(signal.SIGINT,))
                    timer.start()
                    try:
                        result = subprocess.run([sys.executable, str(writer), str(result_file)],
                                                capture_output=True, timeout=None, **guard.child_options())
                    finally:
                        timer.join()  # No late signal after handlers are restored.
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result_file.read_text(), "complete")
                    journal.write_text("write completed")
                with self.assertRaises(CancelRequested):
                    guard.checkpoint()
            self.assertEqual(journal.read_text(), "write completed")
