"""Root access, binary transport and failure recovery, without USB/device access."""
import contextlib
import io
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from test_safety import WorkflowFixture, ORIGINAL, backup, unlock
import rooted_chkcode as root


class MemoryBlock:
    def __init__(self, data=ORIGINAL):
        self.data = data
        self.reads = 0
        self.writes = []
        self.limit = 8

    def read_file(self, path, size):
        assert path == root.CHKCODE_PATH and size == backup.CHKCODE_SIZE
        self.reads += 1
        return self.data

    def open(self, path, mode):
        assert path == root.CHKCODE_PATH and mode == 2
        return path

    def lseek(self, fd, offset, whence):
        assert fd == root.CHKCODE_PATH and offset == whence == 0
        return 0

    def write(self, fd, data):
        assert fd == root.CHKCODE_PATH and len(data) == 8
        self.writes.append(data[:self.limit])
        self.data = data[:self.limit] + self.data[self.limit:]
        return min(self.limit, len(data))

    def close(self, fd):
        pass


class RootOperationsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix=".test-balmunlock-", dir=Path(__file__).resolve().parents[1])
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "original.bin"
        self.fs = MemoryBlock()

    def save(self):
        backup.save_backup(self.path, ORIGINAL)

    def test_backup_double_reads_and_refuses_existing_original_before_reading(self):
        root.operate(self.fs, "backup", self.path)
        self.assertEqual(self.fs.reads, 2)
        self.assertEqual(backup.load_backup(self.path), ORIGINAL)
        with self.assertRaises(FileExistsError):
            root.operate(self.fs, "backup", self.path)
        self.assertEqual(self.fs.reads, 2)

    def test_mismatched_or_short_double_read_never_creates_backup(self):
        for second in (ORIGINAL[:-1], ORIGINAL[:-1] + b"!"):
            with patch.object(self.fs, "read_file", side_effect=[ORIGINAL, second]):
                with self.assertRaises(ValueError):
                    root.operate(self.fs, "backup", self.path)
            self.assertFalse(self.path.exists())
            self.assertFalse(self.fs.writes)

    def test_marker_preserves_tail_and_restore_recovers_exact_original(self):
        self.save()
        root.operate(self.fs, "mark", self.path)
        self.assertEqual(self.fs.data, backup.MAGIC + ORIGINAL[8:])
        self.assertEqual(self.fs.reads, 2)
        root.operate(self.fs, "restore", self.path)
        self.assertEqual(self.fs.data, ORIGINAL)
        self.assertEqual(self.fs.writes, [backup.MAGIC, ORIGINAL[:8]])

    def test_partial_marker_can_be_restored_but_unrelated_tail_is_refused(self):
        self.save()
        self.fs.limit = 4
        with self.assertRaises(ValueError):
            root.operate(self.fs, "mark", self.path)
        self.fs.limit = 8
        root.operate(self.fs, "restore", self.path)
        self.assertEqual(self.fs.data, ORIGINAL)
        self.fs.data = backup.MAGIC + ORIGINAL[8:-1] + b"!"
        before = len(self.fs.writes)
        with self.assertRaises(ValueError):
            root.operate(self.fs, "restore", self.path)
        self.assertEqual(len(self.fs.writes), before)

    def test_live_mismatch_or_corrupt_original_never_writes(self):
        self.save()
        self.fs.data = bytes(len(ORIGINAL))
        with self.assertRaises(ValueError):
            root.operate(self.fs, "mark", self.path)
        self.path.write_bytes(b"bad")
        with self.assertRaises(ValueError):
            root.operate(self.fs, "mark", self.path)
        self.assertFalse(self.fs.writes)

    def test_marker_full_readback_detects_tail_corruption(self):
        self.save()
        with patch.object(self.fs, "read_file", side_effect=[ORIGINAL, backup.MAGIC + ORIGINAL[8:-1] + b"!"]):
            with self.assertRaises(ValueError):
                root.operate(self.fs, "mark", self.path)

    def test_binary_adb_transport_is_serial_bound_without_pty_or_write_timeout(self):
        calls = []
        def run(args, **kwargs):
            calls.append((args, kwargs))
            self.assertEqual(args[:3], ["adb", "-s", "SERIAL"])
            if args[3:] == ["shell", "id", "-u"]:
                data = b"0\n"
            elif "blockdev" in args:
                data = str(backup.CHKCODE_SIZE).encode() + b"\n"
            elif "-T" in args and any(arg.startswith("if=") for arg in args):
                data = ORIGINAL
            elif "-T" in args:
                self.assertEqual(kwargs["input"], b"\x00\n\r\xff\x1a\x80!?")
                self.assertIsNone(kwargs["timeout"])
                data = b""
            else:
                raise AssertionError(args)
            self.assertNotIn("text", kwargs)
            return subprocess.CompletedProcess(args, 0, data, b"")
        with patch.object(root.subprocess, "run", side_effect=run):
            fs = root.RootBlock("SERIAL")
            self.assertEqual(fs.read_file(root.CHKCODE_PATH, backup.CHKCODE_SIZE), ORIGINAL)
            fd = fs.open(root.CHKCODE_PATH, 2)
            fs.write(fd, b"\x00\n\r\xff\x1a\x80!?")
        self.assertFalse(any("-t" in args or "-tt" in args for args, _ in calls))

    def test_real_dd_stderr_is_not_part_of_binary_backup(self):
        bash = shutil.which("bash") if os.name != "nt" else r"C:\Program Files\Git\bin\bash.exe"
        if not bash or not Path(bash).is_file():
            self.skipTest("POSIX shell unavailable")
        sample = self.path.parent / "chkcode.bin"
        sample.write_bytes(ORIGINAL)
        real_run = subprocess.run
        raw_command = "dd if=chkcode.bin bs=524288 count=1"
        raw = real_run([bash, "-c", raw_command], cwd=sample.parent,
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=15)
        self.assertEqual(raw.returncode, 0)
        self.assertGreater(len(raw.stdout), len(ORIGINAL))  # Reproduces exec-out's merged stream.

        def adb_shell(args, **kwargs):
            self.assertEqual(args[:3], ["adb", "-s", "SERIAL"])
            if args[3:] == ["shell", "id", "-u"]:
                return subprocess.CompletedProcess(args, 0, b"0\n", b"")
            if "blockdev" in args:
                return subprocess.CompletedProcess(args, 0, b"524288\n", b"")
            self.assertEqual(args[3:6], ["shell", "-T", "dd"])
            # Real dd + raw pipes, with the streams separated as in shell v2.
            return real_run([bash, "-c", shlex.join(args[5:])], cwd=sample.parent,
                            capture_output=True, timeout=15)

        with patch.object(root, "CHKCODE_PATH", "chkcode.bin"), \
                patch.object(root.subprocess, "run", side_effect=adb_shell):
            fs = root.RootBlock("SERIAL")
            self.assertEqual(fs.read_file("chkcode.bin", backup.CHKCODE_SIZE), ORIGINAL)
            root.operate(fs, "backup", self.path)
            self.assertEqual(backup.load_backup(self.path), ORIGINAL)
            # A genuinely truncated read is still refused, without trimming/padding.
            sample.write_bytes(ORIGINAL[:-1])
            with self.assertRaisesRegex(ValueError, "524287"):
                fs.read_file("chkcode.bin", backup.CHKCODE_SIZE)
            sample.unlink()
            with self.assertRaisesRegex(ValueError, "Root ADB operation failed"):
                fs.read_file("chkcode.bin", backup.CHKCODE_SIZE)

    def test_wrong_uid_size_and_serial_stop_before_partition_write(self):
        for replies in ((b"2000\n",), (b"0\n", b"1048576\n")):
            with patch.object(root.subprocess, "run", side_effect=[subprocess.CompletedProcess([], 0, data, b"") for data in replies]):
                with self.assertRaises(ValueError):
                    root.RootBlock("SERIAL")
        with patch.object(root.subprocess, "run") as run:
            with self.assertRaises(ValueError):
                root.RootBlock(";unsafe")
            run.assert_not_called()

    def test_invalid_local_backup_is_rejected_before_any_adb(self):
        self.path.write_bytes(b"bad")
        with patch.object(sys, "argv", ["rooted_chkcode", "mark", "--backup", str(self.path)]), \
                patch.object(root.subprocess, "run") as run:
            with self.assertRaises(ValueError):
                root.main()
            run.assert_not_called()


class RootWorkflowTests(WorkflowFixture):
    isolate_bootloader_check = False

    def setUp(self):
        super().setUp()
        self.fs = MemoryBlock()
        self.uid = "0"
        self.root_failure = None
        self.host.properties.update({"ro.build.version.sdk": "37", "ro.build.version.incremental": "1793042296",
                                     "ro.build.version.security_patch": "2026-10-01", "ro.product.vendor.device": "A101BM",
                                     "ro.board.platform": "lito", "ro.boot.serialno": "SERIAL", "sys.boot_completed": "1"})
        self.enter(patch.object(unlock.subprocess, "run", side_effect=self.transport_run))

    def transport_run(self, args, **kwargs):
        args = list(map(str, args))
        if args == ["adb", "-s", "SERIAL", "root"]:
            self.host.calls.append((args, kwargs))
            return subprocess.CompletedProcess(args, 0, "restarting adbd as root", "")
        if args == ["adb", "-s", "SERIAL", "shell", "id", "-u"]:
            self.host.calls.append((args, kwargs))
            return subprocess.CompletedProcess(args, 0, self.uid, "")
        if args == ["fastboot", "-s", "SERIAL", "getvar", "product"]:
            self.host.calls.append((args, kwargs))
            return subprocess.CompletedProcess(args, 0, "", "(bootloader) product: lito\n")
        if args[0] == sys.executable and Path(args[1]).name == "rooted_chkcode.py":
            self.host.calls.append((args, kwargs))
            if args[2] == self.root_failure:
                return subprocess.CompletedProcess(args, 1, "", "simulated failure")
            try:
                root.operate(self.fs, args[2], Path(args[4]))
            except ValueError as error:
                return subprocess.CompletedProcess(args, 1, "", str(error))
            return subprocess.CompletedProcess(args, 0, "verified", "")
        result = self.host.run(args, **kwargs)
        if args[:5] == ["fastboot", "-s", "SERIAL", "flash", "chkcode"] and result.returncode == 0:
            self.fs.data = Path(args[5]).read_bytes()
        return result

    def test_custom_android_unlock_uses_root_backups_and_existing_fastboot_restore(self):
        self.flow.execute()
        self.assertEqual(self.flow.state["phase"], "reboot_submitted")
        self.assertEqual(self.flow.state["access_method"], "adb_root")
        self.assertEqual(self.fs.data, ORIGINAL)
        self.assertEqual(backup.load_backup(self.flow.backup), ORIGINAL)
        self.assertEqual(backup.load_backup(self.flow.recovery_backup), ORIGINAL)
        helpers = [Path(args[1]).name for args, _ in self.host.calls if args[0] == sys.executable]
        self.assertEqual(helpers, ["rooted_chkcode.py", "rooted_chkcode.py"])
        self.assertEqual(len(self.flashed()), 1)
        mark_options = next(options for args, options in self.host.calls if
                            args[0] == sys.executable and Path(args[1]).name == "rooted_chkcode.py" and args[2] == "mark")
        self.assertIsNone(mark_options["timeout"])

    def test_root_unavailable_and_wrong_serial_never_create_backup_or_marker(self):
        self.uid = "2000"
        with self.assertRaisesRegex(unlock.Stop, "ADB root is unavailable"):
            self.flow.execute()
        self.assertFalse(self.flow.backup.exists())
        self.assertFalse(self.fs.writes)
        self.host.properties["ro.boot.serialno"] = "OTHER"
        with self.assertRaises(ValueError):
            self.flow.execute()
        self.assertFalse(self.fs.writes)

    def test_root_backup_failure_does_not_try_stock_or_write_marker(self):
        self.root_failure = "backup"
        with self.assertRaises(unlock.Stop):
            self.flow.execute()
        self.assertFalse(self.fs.writes)
        self.assertFalse(self.flow.marker_possible)
        self.assertFalse(any("flashing" in args for args, _ in self.host.calls))

    def test_partial_root_marker_recovers_through_same_backend(self):
        self.fs.limit = 4
        with self.assertRaises(unlock.Stop):
            self.flow.execute()
        self.assertTrue(self.flow.marker_possible)
        self.assertFalse(self.flow.marker_verified)
        self.assertFalse(any("flashing" in args for args, _ in self.host.calls))
        self.fs.limit = 8
        self.host.adb_online = True
        resumed = unlock.Workflow()
        resumed.root = self.root
        resumed.load_session()
        self.assertTrue(resumed.recover())
        self.assertEqual(resumed.access_method, "adb_root")
        self.assertEqual(self.fs.data, ORIGINAL)
        self.assertFalse(resumed.marker_possible)

    def test_root_prepared_session_resumes_without_stock_validation(self):
        self.flow.execute(prepare_only=True)
        self.assertEqual(self.flow.state["phase"], "prepared")
        self.flow.resume_prepared()
        self.assertEqual(self.flow.state["phase"], "reboot_submitted")
        self.assertEqual(self.fs.data, ORIGINAL)

    def test_supported_stock_is_tried_before_root_and_cleanup_precedes_fallback(self):
        self.host.properties.update({"ro.build.version.sdk": "31", "ro.build.version.incremental": "1.280",
                                     "ro.build.version.security_patch": "2023-09-01"})
        events = []
        def stock_failure():
            events.append("stock")
            raise unlock.Stop("failed")
        with patch.object(self.flow, "activate_diag", side_effect=stock_failure), \
                patch.object(self.flow, "cleanup_cve", side_effect=lambda: events.append("cleanup")), \
                patch.object(self.flow, "ensure_root", wraps=self.flow.ensure_root) as probe:
            self.flow.execute()
        self.assertEqual(events, ["stock", "cleanup"])
        probe.assert_called_once()
        self.assertEqual(self.flow.state["access_method"], "adb_root")

    def test_already_unlocked_custom_phone_never_requests_root_or_marker(self):
        self.host.unlocked_at = 0
        with self.assertRaises(unlock.AlreadyUnlocked):
            self.flow.execute()
        self.assertFalse(self.fs.writes)
        self.assertFalse(any("root" in args for args, _ in self.host.calls))

    def recovery_after_unlock(self, *, unknown_fastboot_state=False):
        original = self.transport_run
        pending = {"recovery": False}
        self.host.properties.update({"ro.boot.vbmeta.device_state": "unlocked",
                                     "ro.boot.verifiedbootstate": "orange", "ro.twrp.version": "3.7.1_12"})
        def transport(args, **kwargs):
            if pending["recovery"] and args == ["fastboot", "devices"]:
                self.host.calls.append((args, kwargs))
                return subprocess.CompletedProcess(args, 0, "", "")
            if pending["recovery"] and args == ["adb", "devices"]:
                self.host.calls.append((args, kwargs))
                return subprocess.CompletedProcess(args, 0, "List of devices attached\nSERIAL\trecovery\n", "")
            result = original(args, **kwargs)
            if args == ["fastboot", "-s", "SERIAL", "flashing", "unlock"]:
                pending["recovery"] = True
            elif args == ["adb", "-s", "SERIAL", "reboot", "bootloader"] and pending["recovery"]:
                pending["recovery"] = False
                if unknown_fastboot_state:
                    self.host.query = "unknown"
            return result
        return patch.object(unlock.subprocess, "run", side_effect=transport)

    def test_recovery_after_unlock_returns_to_fastboot_and_restores_original(self):
        with self.recovery_after_unlock():
            self.flow.execute()
        self.assertEqual(self.flow.state["phase"], "reboot_submitted")
        self.assertEqual(self.fs.data, ORIGINAL)
        reboots = [args for args, _ in self.host.calls if args == ["adb", "-s", "SERIAL", "reboot", "bootloader"]]
        self.assertEqual(len(reboots), 3)  # Driver check, marker boot, recovery handoff.
        self.assertEqual(len(self.fs.writes), 1)

    def test_recovery_claim_alone_does_not_prove_fastboot_unlock(self):
        with self.recovery_after_unlock(unknown_fastboot_state=True), \
                self.assertRaisesRegex(unlock.Stop, "Unlocked bootloader not confirmed"):
            self.flow.execute()
        self.assertTrue(self.flow.marker_possible)
        self.assertFalse(self.flashed())

    def test_locked_recovery_is_not_rebooted_as_unlock_success(self):
        self.flow.serial = "SERIAL"
        self.flow.access_method = "adb_root"
        self.flow.marker_verified = True
        self.host.fastboot_delay = 999
        self.host.adb_rows = "SERIAL recovery\n"
        self.host.properties.update({"ro.boot.vbmeta.device_state": "locked",
                                     "ro.boot.verifiedbootstate": "yellow", "ro.twrp.version": "3.7.1_12"})
        with self.assertRaisesRegex(unlock.Stop, "Unlock not confirmed"):
            self.flow.wait_unlocked(timeout=3)
        self.assertFalse(any(args[3:5] == ["reboot", "bootloader"] for args, _ in self.host.calls))

    def test_unknown_saved_backend_is_refused_before_device_access(self):
        import json
        self.flow.execute(prepare_only=True)
        state = json.loads(self.flow.receipt.read_text())
        state["access_method"] = "unknown"
        self.flow.receipt.write_text(json.dumps(state))
        self.host.calls.clear()
        with self.assertRaises(unlock.Stop):
            self.flow.load_session()
        self.assertFalse(self.host.calls)

    def test_adbd_restart_is_waited_out_before_root_is_accepted(self):
        original = self.transport_run
        uid_calls = []
        def restarting(args, **kwargs):
            if args == ["adb", "-s", "SERIAL", "shell", "id", "-u"]:
                uid_calls.append(args)
                if len(uid_calls) == 1:
                    return subprocess.CompletedProcess(args, 1, "", "device offline")
            return original(args, **kwargs)
        with patch.object(unlock.subprocess, "run", side_effect=restarting):
            self.flow.execute(prepare_only=True)
        self.assertEqual(len(uid_calls), 2)
        self.assertFalse(self.fs.writes)

    def test_failed_stock_cleanup_prevents_root_fallback(self):
        self.host.properties.update({"ro.build.version.sdk": "31", "ro.build.version.incremental": "1.280",
                                     "ro.build.version.security_patch": "2023-09-01"})
        with patch.object(self.flow, "activate_diag", side_effect=unlock.Stop("stock failed")), \
                patch.object(self.flow, "cleanup_cve", side_effect=unlock.Stop("cleanup pending")):
            with self.assertRaisesRegex(unlock.Stop, "cleanup pending"):
                self.flow.execute()
        self.assertFalse(self.fs.writes)
        self.assertFalse(self.flow.backup.exists())
        self.assertFalse(any("root" in args for args, _ in self.host.calls))
