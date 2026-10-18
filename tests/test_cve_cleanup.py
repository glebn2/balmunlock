"""CVE cleanup tests: all ADB subprocesses are simulated, no phone access."""
import contextlib
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))
import cve31317 as cve
import cve_cleanup as cleanup
import unlock


class FakeADB:
    def __init__(self):
        self.hidden = None
        self.usap = None
        self.payload = False
        self.injected = False
        self.online = True
        self.fail_start = False
        self.calls = []

    def run(self, args, **kwargs):
        args = [str(arg) for arg in args]
        self.calls.append(args)
        if not self.online:
            return subprocess.CompletedProcess(args, 1, "", "device disconnected")
        if "-s" in args:
            if args[args.index("-s") + 1] != "SERIAL":
                raise AssertionError("Wrong device selected")
        output, code = "", 0
        if args[-1] == "get-serialno":
            output = "SERIAL"
        elif "push" in args:
            self.payload = True
        elif "shell" in args:
            cmd = args[-1]
            if cmd == "getprop sys.boot_completed":
                output = "1"
            elif cmd == "getprop sys.usb.config":
                output = "diag,modem,adb" if self.injected else "mtp,adb"
            elif cmd == cleanup.HIDDEN_GET:
                output = "null" if self.hidden is None else self.hidden
            elif cmd == cleanup.USAP_GET:
                output = "null" if self.usap is None else self.usap
            elif cmd.startswith("settings delete global hidden_api_blacklist_exemptions"):
                self.hidden = None
            elif cmd.startswith("settings put global hidden_api_blacklist_exemptions "):
                self.hidden = shlex.split(cmd)[4]
            elif cmd.startswith("device_config delete activity_manager native_usap_pool_enabled"):
                self.usap = None
            elif cmd.startswith("device_config put activity_manager native_usap_pool_enabled "):
                self.usap = shlex.split(cmd)[4]
            elif cmd == f"rm -f {cleanup.PAYLOAD_PATH}":
                self.payload = False
            elif cmd == f"test ! -e {cleanup.PAYLOAD_PATH}":
                code = int(self.payload)
            elif cmd.startswith('P="$(cat '):
                self.hidden = "*"
                self.injected = True
            elif cmd.startswith("am force-stop"):
                pass
            elif cmd.startswith("am start"):
                if self.fail_start:
                    raise RuntimeError("simulated failure between put and delete")
            else:
                raise AssertionError(f"Unexpected ADB command: {cmd}")
        else:
            raise AssertionError(f"Unexpected subprocess: {args}")
        return subprocess.CompletedProcess(args, code, output + "\n", "")


class CleanupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix=".test-unmuda-", dir=TOOLS.parent)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.state = self.root / cleanup.STATE_NAME
        self.adb = FakeADB()
        self.now = 0
        self.enter(patch.object(cleanup.subprocess, "run", self.adb.run))
        self.enter(patch.object(cleanup.time, "monotonic", lambda: self.now))
        self.enter(patch.object(cleanup.time, "sleep", self.sleep))
        self.enter(patch.dict(os.environ, {"ANDROID_SERIAL": "SERIAL"}))
        self.enter(patch.object(unlock, "ROOT", self.root))
        self.enter(patch.object(cve, "InjectionCleanup", lambda exe: cleanup.InjectionCleanup(exe, self.state)))
        self.enter(patch.object(cve, "DEFAULT_STATE", self.state))
        self.enter(contextlib.redirect_stdout(io.StringIO()))
        self.enter(contextlib.redirect_stderr(io.StringIO()))
        # Temporary payload produced by main() stays inside the test workspace.
        original_path = Path
        self.enter(patch.object(cve, "Path", lambda name: original_path(self.root / name)))

    def sleep(self, seconds):
        self.now += seconds

    def enter(self, manager):
        value = manager.__enter__()
        self.addCleanup(manager.__exit__, None, None, None)
        return value

    def pending(self, hidden=None, usap=None):
        self.state.write_text(json.dumps({"version": 1, "serial": "SERIAL",
                                          "hidden_api": hidden, "usap": usap}), encoding="utf-8")

    def test_normal_injection_restores_original_settings_and_removes_remote_payload(self):
        self.adb.hidden, self.adb.usap = "Landroid/example;", "true"
        with patch.object(sys, "argv", ["cve"]):
            self.assertEqual(cve.main(), 0)
        self.assertEqual(self.adb.hidden, "Landroid/example;")
        self.assertEqual(self.adb.usap, "true")
        self.assertFalse(self.adb.payload)
        self.assertFalse(self.state.exists())

    def test_exception_after_settings_put_still_restores_original_state(self):
        self.adb.fail_start = True
        with patch.object(sys, "argv", ["cve"]), self.assertRaises(RuntimeError):
            cve.main()
        self.assertIsNone(self.adb.hidden)
        self.assertIsNone(self.adb.usap)
        self.assertFalse(self.adb.payload)
        self.assertFalse(self.state.exists())

    def test_baseline_already_active_does_not_change_unrelated_settings(self):
        self.adb.injected = True
        self.adb.hidden, self.adb.usap = "Luser/custom;", "true"
        with patch.object(sys, "argv", ["cve"]):
            self.assertEqual(cve.main(), 0)
        self.assertEqual((self.adb.hidden, self.adb.usap), ("Luser/custom;", "true"))
        self.assertFalse(self.state.exists())

    def test_baseline_does_not_report_success_with_pending_cleanup(self):
        self.pending()
        self.adb.injected = True
        with patch.object(sys, "argv", ["cve"]):
            self.assertEqual(cve.main(), 2)
        self.assertTrue(self.state.exists())
        self.assertFalse(self.adb.calls)

    def test_cleanup_disconnect_retains_receipt_then_resumes_on_same_serial(self):
        self.pending()
        self.adb.hidden, self.adb.usap, self.adb.payload = "*", "false", True
        self.adb.online = False
        with self.assertRaises(cleanup.CleanupError):
            cleanup.cleanup_pending(self.state, timeout=2)
        self.assertTrue(self.state.exists())
        self.adb.online = True
        self.assertTrue(cleanup.cleanup_pending(self.state, expected_serial="SERIAL"))
        self.assertEqual((self.adb.hidden, self.adb.usap, self.adb.payload), (None, None, False))
        self.assertFalse(self.state.exists())

    def test_foreign_or_malformed_receipt_never_sends_adb_commands(self):
        self.pending()
        with self.assertRaises(cleanup.CleanupError):
            cleanup.cleanup_pending(self.state, expected_serial="OTHER")
        self.state.write_text('{"version":1}', encoding="utf-8")
        with self.assertRaises(cleanup.CleanupError):
            cleanup.cleanup_pending(self.state)
        self.assertFalse(self.adb.calls)

    def test_multiline_prior_value_is_not_saved_or_rearmed(self):
        self.adb.hidden = "old\ninjection"
        with self.assertRaises(cleanup.CleanupError):
            with cleanup.InjectionCleanup(path=self.state):
                self.fail("Mutation should never start")
        self.assertFalse(self.state.exists())
        self.assertEqual(self.adb.hidden, "old\ninjection")

    def test_original_setting_is_quoted_as_one_shell_argument(self):
        original = "Landroid/example; a'b $literal"
        self.pending(hidden=original, usap="false")
        self.assertTrue(cleanup.cleanup_pending(self.state))
        self.assertEqual(self.adb.hidden, original)
        self.assertEqual(self.adb.usap, "false")

    def test_existing_pending_receipt_is_never_overwritten_by_new_injection(self):
        self.pending(hidden="Loriginal;")
        original = self.state.read_bytes()
        with self.assertRaises(cleanup.CleanupError):
            with cleanup.InjectionCleanup(path=self.state):
                self.fail("Should refuse")
        self.assertEqual(self.state.read_bytes(), original)
        self.assertFalse(self.adb.calls)

    def test_parent_cleanup_runs_after_abrupt_child_failure(self):
        flow = unlock.Workflow()
        flow.serial = "SERIAL"
        def failed_child(*args, **kwargs):
            self.pending()
            self.adb.hidden, self.adb.usap, self.adb.payload = "*", "false", True
            raise unlock.Stop("Child was killed before its finally")
        with patch.object(flow, "run", failed_child), self.assertRaises(unlock.Stop):
            flow.tool("cve31317.py")
        self.assertEqual((self.adb.hidden, self.adb.usap, self.adb.payload), (None, None, False))
        self.assertFalse(self.state.exists())

    def test_parent_refuses_success_when_cleanup_cannot_be_confirmed(self):
        flow = unlock.Workflow()
        flow.serial = "SERIAL"
        def successful_child_with_pending_cleanup(*args, **kwargs):
            self.pending()
            self.adb.online = False
            return subprocess.CompletedProcess(args[0], 0, "helper says done", "")
        with patch.object(flow, "run", successful_child_with_pending_cleanup), self.assertRaises(unlock.Stop):
            flow.tool("cve31317.py")
        self.assertTrue(self.state.exists())
        self.assertFalse(flow.marker_possible)
        self.assertFalse(flow.backup.exists())

    def test_workflow_recover_handles_cleanup_only_session_without_partition_backup(self):
        self.pending()
        self.adb.hidden, self.adb.usap, self.adb.payload = "*", "false", True
        with patch.object(sys, "argv", ["unlock", "--recover", "--session", "legacy"]), patch("builtins.input", return_value="yes"):
            self.assertEqual(unlock.main(), 0)
        self.assertFalse(self.state.exists())
        self.assertFalse((self.root / "chkcode_backup.bin").exists())
        self.assertFalse(self.adb.payload)

    def test_explicit_session_state_and_payload_do_not_use_legacy_directory(self):
        directory = self.root / "selected-session"
        directory.mkdir()
        state = directory / cleanup.STATE_NAME
        self.pending()
        legacy = self.state.read_bytes()
        factory = lambda exe, path: cleanup.InjectionCleanup(exe, path)
        with patch.object(cve, "InjectionCleanup", factory), \
                patch.object(sys, "argv", ["cve", "--state", str(state)]):
            self.assertEqual(cve.main(), 0)
        self.assertFalse(state.exists())
        self.assertEqual(self.state.read_bytes(), legacy)
        self.assertTrue((directory / "p31317.txt").exists())
        self.assertFalse((self.root / "p31317.txt").exists())
