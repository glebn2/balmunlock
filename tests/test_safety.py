"""Offline regression tests. No real ADB, fastboot or USB calls are allowed."""
import contextlib
import importlib.util
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))
import backup_chkcode
import chkcode_backup as backup
import unlock
import unlock_chkcode
import restore_chkcode
from device_policy import validate_identity
from operation_signals import OperationSignals, CancelRequested

ORIGINAL = bytes(range(256)) * (backup.CHKCODE_SIZE // 256)


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix=".test-balmunlock-", dir=TOOLS.parent)
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "chkcode_backup.bin"

    def test_roundtrip_preserves_nonzero_factory_bytes(self):
        backup.save_backup(self.path, ORIGINAL)
        self.assertEqual(backup.load_backup(self.path), ORIGINAL)

    def test_reject_short_oversize_and_service_marker(self):
        for data in (b"", ORIGINAL[:-1], ORIGINAL + b"0",
                     backup.MAGIC + ORIGINAL[8:]):
            with self.subTest(size=len(data), prefix=data[:8]):
                with self.assertRaises(ValueError):
                    backup.save_backup(self.path, data)
                self.assertFalse(self.path.exists())

    def test_never_overwrite_original_or_orphan_checksum(self):
        backup.save_backup(self.path, ORIGINAL)
        with self.assertRaises(FileExistsError):
            backup.save_backup(self.path, bytes(len(ORIGINAL)))
        self.assertEqual(self.path.read_bytes(), ORIGINAL)
        self.path.unlink()
        with self.assertRaises(FileExistsError):
            backup.save_backup(self.path, ORIGINAL)

    def test_tampered_or_missing_checksum_is_rejected(self):
        backup.save_backup(self.path, ORIGINAL)
        self.path.write_bytes(ORIGINAL[:-1] + b"!")
        with self.assertRaises(ValueError):
            backup.load_backup(self.path)
        backup.checksum_path(self.path).unlink()
        with self.assertRaises(OSError):
            backup.load_backup(self.path)

    def fake_diag(self, fs):
        transport = Mock()
        return transport, patch.dict(sys.modules, {
            "diag_transport": types.SimpleNamespace(open_transport=lambda: transport),
            "fs_sys_call": types.SimpleNamespace(FSDiag=lambda tr: fs, FSError=RuntimeError,
                                                  O_RDWR=2, SEEK_SET=0),
        })

    def test_backup_requires_two_identical_full_reads(self):
        for readings in ([ORIGINAL[:-1]], [ORIGINAL, ORIGINAL[:-1]],
                         [ORIGINAL, bytes(len(ORIGINAL))],
                         [ORIGINAL, RuntimeError("USB disconnected")]):
            fs = Mock()
            fs.read_file.side_effect = readings
            tr, modules = self.fake_diag(fs)
            with modules, patch.object(sys, "argv", ["backup", "-o", str(self.path)]):
                with self.assertRaises((ValueError, RuntimeError)):
                    backup_chkcode.main()
            tr.close.assert_called_once()
            self.assertFalse(self.path.exists())

    def test_backup_success_and_rerun_before_usb(self):
        fs = Mock()
        fs.read_file.return_value = ORIGINAL
        tr, modules = self.fake_diag(fs)
        with modules, patch.object(sys, "argv", ["backup", "-o", str(self.path)]):
            self.assertEqual(backup_chkcode.main(), 0)
            fs.read_file.assert_called()
            self.assertEqual(fs.read_file.call_count, 2)
            with self.assertRaises(FileExistsError):
                backup_chkcode.main()
            self.assertEqual(fs.read_file.call_count, 2)
        self.assertEqual(backup.load_backup(self.path), ORIGINAL)

    def test_invalid_backup_never_opens_usb_for_marker(self):
        self.path.write_bytes(b"short")
        fs = Mock()
        tr, modules = self.fake_diag(fs)
        with modules, patch.object(sys, "argv", ["unlock", "--backup", str(self.path), "--yes"]):
            self.assertEqual(unlock_chkcode.main(), 2)
        fs.assert_not_called()
        tr.close.assert_not_called()

    def test_marker_requires_live_partition_match_and_exact_readback(self):
        backup.save_backup(self.path, ORIGINAL)
        for live, written, readback, expected in (
            (bytes(len(ORIGINAL)), 8, backup.MAGIC + ORIGINAL[8:16], 2),
            (ORIGINAL, 4, b"", 1),
            (ORIGINAL, 8, backup.MAGIC + b"bad-tail", 1),
            (ORIGINAL, 8, backup.MAGIC + ORIGINAL[8:16], 0),
        ):
            fs = Mock()
            fs.read_file.return_value = live
            fs.write.return_value = written
            fs.read.return_value = readback
            if readback != backup.MAGIC + ORIGINAL[8:16]:
                fs.verify_at.side_effect = RuntimeError("control read mismatch")
            else:
                fs.verify_at.return_value = readback
            tr, modules = self.fake_diag(fs)
            with modules, patch.object(sys, "argv", ["unlock", "--backup", str(self.path), "--yes"]):
                self.assertEqual(unlock_chkcode.main(), expected)
            if live != ORIGINAL:
                fs.write.assert_not_called()
            else:
                fs.write.assert_called_once_with(fs.open.return_value, backup.MAGIC)
                fs.close.assert_called_once()
            tr.close.assert_called_once()


class ParserTests(unittest.TestCase):
    def test_unlock_reply(self):
        for value in ("(bootloader) Device unlocked: true\n", "unlocked: yes\r\n"):
            self.assertIs(unlock.parse_unlocked(value), True)
        for value in ("(bootloader) Device unlocked: false\n", "unlocked: no\n"):
            self.assertIs(unlock.parse_unlocked(value), False)
        for value in ("", "FAILED (unknown variable)", "unlocked: ",
                      "(bootloader) Device critical unlocked: true\n",
                      "unlocked: yes\nunlocked: no\n", "not unlocked: yes\n"):
            self.assertIsNone(unlock.parse_unlocked(value))


class FakeHost:
    """Subprocess boundary simulation, including unplug/replug and late consent."""
    def __init__(self):
        self.calls = []
        self.now = 0
        self.fastboot_delay = 0
        self.unlock_delay = 0
        self.unlocked_at = None
        self.fail_at = None
        self.query = "oem"
        self.adb_rows = "SERIAL device\n"
        self.tamper = False
        self.userspace = False
        self.adb_online = False
        self.flash_failures = 0
        self.reboot_failures = 0
        self.properties = {"ro.product.model": "X01A", "ro.build.version.incremental": "1.280",
                           "ro.build.version.security_patch": "2022-10-01", "ro.build.version.sdk": "31",
                           "sys.usb.config": "diag,modem,adb", "init.svc.fs_sys_call_diag": "running"}
        self.on_write = None

    def sleep(self, seconds):
        self.now += seconds

    def run(self, args, **kwargs):
        args = [str(arg) for arg in args]
        self.calls.append((args, kwargs))
        out, err, code = "", "", 0
        if args == ["adb", "devices"]:
            out = "List of devices attached\n" + self.adb_rows
        elif args in (["adb", "kill-server"], ["adb", "start-server"]):
            pass
        elif args[:4] == ["adb", "-s", "SERIAL", "shell"]:
            out = self.properties[args[-1]]
        elif args == ["adb", "-s", "SERIAL", "get-state"]:
            out, code = ("device\n", 0) if self.adb_online else ("", 1)
        elif args == ["fastboot", "devices"]:
            if self.now >= self.fastboot_delay:
                out = "SERIAL\tfastboot\n"
        elif args[0] == sys.executable:
            name = Path(args[1]).name
            if self.on_write is not None and name in ("unlock_chkcode.py", "restore_chkcode.py"):
                self.on_write(name)
            if name == "backup_chkcode.py":
                backup.save_backup(Path(args[-1]), ORIGINAL)
            if name == self.fail_at:
                code = 1
        elif args[:3] == ["fastboot", "-s", "SERIAL"]:
            command = args[3:]
            if command == ["getvar", "is-userspace"]:
                err = "is-userspace: " + ("yes" if self.userspace else "no")
            elif command == ["flashing", "unlock"]:
                self.unlocked_at = self.now + self.unlock_delay
            elif command in (["oem", "device-info"], ["getvar", "unlocked"]):
                if self.query == "unknown" or (command[0] == "oem" and self.query == "getvar"):
                    code, err = 1, "FAILED (unknown command)"
                else:
                    state = self.unlocked_at is not None and self.now >= self.unlocked_at
                    err = ("(bootloader) Device unlocked: " + str(state).lower()
                           if command[0] == "oem" else "unlocked: " + ("yes" if state else "no"))
                    if self.tamper and state:
                        path = Path(self.root) / "chkcode_backup.bin"
                        path.write_bytes(ORIGINAL[:-1] + b"!")
            elif command[:2] == ["flash", "chkcode"]:
                if self.on_write is not None:
                    self.on_write("flash")
                if self.fail_at == "flash" or self.flash_failures:
                    code, err = 1, "FAILED (write failure)"
                    self.flash_failures = max(0, self.flash_failures - 1)
                self.flashed = Path(command[2]).read_bytes()
            elif command == ["reboot"]:
                if self.reboot_failures:
                    self.reboot_failures -= 1
                    code, err = 1, "USB reply lost"
            else:
                raise AssertionError(f"Unexpected fastboot command: {args}")
        else:
            if args != ["adb", "-s", "SERIAL", "reboot", "bootloader"]:
                raise AssertionError(f"Unexpected host command: {args}")
        return subprocess.CompletedProcess(args, code, out, err)


class WorkflowFixture(unittest.TestCase):
    isolate_bootloader_check = True
    isolate_session_routing = True

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix=".test-balmunlock-", dir=TOOLS.parent)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.host = FakeHost()
        self.host.root = self.root
        self.enter(patch.object(unlock, "ROOT", self.root))
        self.enter(patch.object(unlock.subprocess, "run", self.host.run))
        self.enter(patch.object(unlock.time, "sleep", self.host.sleep))
        self.enter(patch.object(unlock.time, "monotonic", lambda: self.host.now))
        self.enter(patch.dict(os.environ, {}, clear=True))
        # These regression cases exercise the strict CLI; guided flows have separate tests.
        self.enter(patch.object(unlock.sys.stdin, "isatty", return_value=False))
        if self.isolate_bootloader_check:
            # Existing write/recovery scenarios begin after driver setup.
            self.enter(patch.object(unlock.Workflow, "check_bootloader", autospec=True))
        if self.isolate_session_routing:
            # Low-level regression scenarios supply their existing session directly.
            self.enter(patch.object(unlock.Workflow, "route_session", return_value=True))
        self.flow = unlock.Workflow()

    def enter(self, manager):
        value = manager.start()
        self.addCleanup(manager.stop)
        return value

    def flashed(self):
        return [args for args, _ in self.host.calls if "flash" in args]

    def rebooted(self):
        return [args for args, _ in self.host.calls if args == ["fastboot", "-s", "SERIAL", "reboot"]]


class WorkflowTests(WorkflowFixture):
    def test_windows_diag_releases_adb_and_restarts_it_after_helper_failure(self):
        self.host.fail_at = 'kdiag_shell.py'
        with patch.object(unlock.os, 'name', 'nt'):
            with self.assertRaises(unlock.Stop):
                self.flow.tool('kdiag_shell.py', 'id')
        self.assertEqual([args for args, _ in self.host.calls], [
            ['adb', 'kill-server'],
            [sys.executable, str(self.root / 'tools' / 'kdiag_shell.py'), 'id'],
            ['adb', 'start-server'],
        ])

    def test_windows_adb_restart_error_does_not_hide_verified_helper_result(self):
        original_run = self.host.run
        def fail_restart(args, **kwargs):
            if args == ['adb', 'start-server']:
                return subprocess.CompletedProcess(args, 1, '', 'restart failed')
            return original_run(args, **kwargs)
        with patch.object(unlock.os, 'name', 'nt'), \
                patch.object(unlock.subprocess, 'run', fail_restart):
            self.flow.tool('unlock_chkcode.py')
        self.assertTrue(any(args[0] == sys.executable for args, _ in self.host.calls))

    def test_prepare_saves_two_originals_and_stops_before_every_partition_write(self):
        self.flow.execute(prepare_only=True)
        self.assertEqual(backup.load_backup(self.flow.backup), ORIGINAL)
        self.assertEqual(backup.load_backup(self.flow.recovery_backup), ORIGINAL)
        state = __import__('json').loads(self.flow.receipt.read_text())
        self.assertEqual(state['phase'], 'prepared')
        self.assertFalse(state['marker_possible'])
        self.assertFalse(state['marker_verified'])
        tools = [Path(args[1]).name for args, _ in self.host.calls if args[0] == sys.executable]
        self.assertEqual(tools, ['cve31317.py', 'kdiag_shell.py', 'backup_chkcode.py'])
        self.assertFalse(any(args[0] == 'fastboot' or 'reboot' in args for args, _ in self.host.calls))
        self.host.calls.clear()
        with self.assertRaises(FileExistsError):
            self.flow.execute(prepare_only=True)
        self.assertFalse(self.host.calls)

    def test_prepare_cli_never_falls_through_into_unlock(self):
        with patch.object(sys, 'argv', ['unlock', '--prepare']), \
                patch('builtins.input', return_value='yes'), \
                patch.object(unlock, 'preflight', return_value=True):
            self.assertEqual(unlock.main(), 0)
        self.assertEqual(__import__('json').loads(self.flow.receipt.read_text())['phase'], 'prepared')
        self.assertFalse(any(args[0] == 'fastboot' or 'reboot' in args or
                             (args[0] == sys.executable and Path(args[1]).name == 'unlock_chkcode.py')
                             for args, _ in self.host.calls))

    def test_explicit_prepared_unlock_reuses_originals_without_new_exploit_or_backup(self):
        self.flow.execute(prepare_only=True)
        self.host.calls.clear()
        with patch.object(sys, 'argv', ['unlock', '--unlock-prepared']), \
                patch('builtins.input', return_value='yes'), \
                patch.object(unlock, 'preflight', return_value=True):
            self.assertEqual(unlock.main(), 0)
        tools = [Path(args[1]).name for args, _ in self.host.calls if args[0] == sys.executable]
        self.assertEqual(tools, ['unlock_chkcode.py'])
        self.assertEqual(self.host.flashed, ORIGINAL)
        self.assertEqual(backup.load_backup(self.flow.backup), ORIGINAL)
        self.assertEqual(backup.load_backup(self.flow.recovery_backup), ORIGINAL)

    def test_prepared_identity_change_refuses_marker(self):
        self.flow.execute(prepare_only=True)
        self.host.calls.clear()
        self.host.properties['ro.build.version.incremental'] = '1.270'
        with self.assertRaises(unlock.Stop):
            unlock.Workflow().resume_prepared()
        self.assertFalse(any(args[0] in (sys.executable, 'fastboot') for args, _ in self.host.calls))

    def test_prepared_other_serial_refuses_marker(self):
        self.flow.execute(prepare_only=True)
        self.host.calls.clear()
        self.host.adb_rows = 'OTHER device\n'
        with self.assertRaises(unlock.Stop):
            unlock.Workflow().resume_prepared()
        self.assertEqual([args for args, _ in self.host.calls], [['adb', 'devices']])

    def test_prepared_started_marker_session_refuses_before_device_commands(self):
        self.flow.execute(prepare_only=True)
        self.flow.marker_possible = True
        self.flow.save_state('marker_write_started')
        self.host.calls.clear()
        with self.assertRaises(unlock.Stop):
            unlock.Workflow().resume_prepared()
        self.assertFalse(self.host.calls)

    def test_prepared_pending_cleanup_refuses_before_device_commands(self):
        self.flow.execute(prepare_only=True)
        (self.root / unlock.STATE_NAME).write_text('{}')
        self.host.calls.clear()
        with self.assertRaises(unlock.Stop):
            unlock.Workflow().resume_prepared()
        self.assertFalse(self.host.calls)

    def test_prepared_no_diag_refuses_marker_without_reactivating_exploit(self):
        self.flow.execute(prepare_only=True)
        self.host.calls.clear()
        self.host.properties['sys.usb.config'] = 'mtp,adb'
        with self.assertRaises(unlock.Stop):
            unlock.Workflow().resume_prepared()
        self.assertFalse(any(args[0] in (sys.executable, 'fastboot') for args, _ in self.host.calls))

    def test_wrong_model_build_sdk_or_spl_refuses_before_any_tool(self):
        for key, value in (("ro.product.model", "OTHER"),
                           ("ro.build.version.incremental", "1.281PO.0686.a"),
                           ("ro.build.version.sdk", "36"),
                           ("ro.build.version.security_patch", "2024-06-01")):
            old = self.host.properties[key]
            self.host.properties[key] = value
            with self.assertRaises(ValueError):
                self.flow.execute()
            self.host.properties[key] = old
        self.assertFalse(any(args[0] == sys.executable for args, _ in self.host.calls))
        self.assertFalse(self.flow.backup.exists())

    def test_zero_exit_without_diag_composition_stops_before_diag_and_backup(self):
        self.host.properties["sys.usb.config"] = "mtp,adb"
        with self.assertRaises(unlock.Stop):
            self.flow.execute()
        names = [Path(args[1]).name for args, _ in self.host.calls if args[0] == sys.executable]
        self.assertEqual(names, ["cve31317.py"])
        self.assertFalse(self.flow.backup.exists())

    def test_zero_exit_without_fs_daemon_stops_before_backup_and_marker(self):
        self.host.properties["init.svc.fs_sys_call_diag"] = "stopped"
        with self.assertRaises(unlock.Stop):
            self.flow.execute()
        names = [Path(args[1]).name for args, _ in self.host.calls if args[0] == sys.executable]
        self.assertEqual(names, ["cve31317.py", "kdiag_shell.py"])
        self.assertFalse(self.flow.backup.exists())

    def test_late_confirmation_restores_exact_backup(self):
        self.host.fastboot_delay = 20
        self.host.unlock_delay = 25
        self.flow.execute()
        self.assertGreaterEqual(self.host.now, 45)
        self.assertEqual(self.host.flashed, ORIGINAL)
        self.assertEqual(len(self.rebooted()), 1)
        self.assertFalse(self.flow.marker_possible)
        for args, kwargs in self.host.calls:
            if args[:2] == ["fastboot", "-s"]:
                self.assertEqual(args[2], "SERIAL")
            if "flash" in args:
                self.assertIsNone(kwargs["timeout"], "Never terminate an active flash on a timer")
            if args[0] == sys.executable:
                self.assertEqual(kwargs["env"]["ANDROID_SERIAL"], "SERIAL")

    def test_getvar_fallback(self):
        self.host.query = "getvar"
        self.flow.execute()
        self.assertEqual(len(self.flashed()), 1)

    def test_unknown_or_declined_unlock_never_restores_or_reboots(self):
        for query, delay in (("unknown", 0), ("oem", 1000)):
            with self.subTest(query=query):
                self.host.query = query
                self.host.unlock_delay = delay
                with self.assertRaises(unlock.Stop):
                    self.flow.execute()
                self.assertFalse(self.flashed())
                self.assertFalse(self.rebooted())
                self.assertTrue(self.flow.marker_possible)
            # Separate isolated attempt, preserving refusal within each run.
            for path in self.root.iterdir():
                path.unlink()

    def test_wrong_fastboot_serial_never_unlocks(self):
        self.flow.serial = "OTHER"
        with self.assertRaises(unlock.Stop):
            self.flow.wait_fastboot(timeout=3)
        self.assertFalse(self.flashed())

    def test_failed_flash_never_reboots(self):
        self.host.fail_at = "flash"
        with self.assertRaises(unlock.Stop):
            self.flow.execute()
        self.assertFalse(self.rebooted())
        self.assertTrue(self.flow.marker_possible)

    def test_marker_helper_failure_never_reboots(self):
        self.host.fail_at = "unlock_chkcode.py"
        with self.assertRaises(unlock.Stop):
            self.flow.execute()
        self.assertFalse(self.flashed())
        self.assertFalse(any("reboot" in args for args, _ in self.host.calls))
        self.assertTrue(self.flow.marker_possible)

    def test_damaged_primary_uses_verified_second_copy(self):
        self.host.tamper = True
        self.flow.execute()
        self.assertEqual(self.host.flashed, ORIGINAL)
        self.assertEqual(Path(self.flashed()[0][-1]), self.flow.recovery_backup)

    def test_existing_backup_refuses_before_any_host_command(self):
        backup.save_backup(self.flow.backup, ORIGINAL)
        with self.assertRaises(FileExistsError):
            self.flow.execute()
        self.assertFalse(self.host.calls)
        self.assertEqual(self.flow.backup.read_bytes(), ORIGINAL)

    def test_multiple_or_unauthorized_devices_refuse(self):
        for rows in ("ONE device\nTWO device\n", "ONE unauthorized\n", "ONE offline\n"):
            self.host.adb_rows = rows
            with self.assertRaises(unlock.Stop):
                self.flow.execute()
            self.assertFalse(self.flow.backup.exists())
        self.assertFalse(self.flashed())

    def test_fastbootd_refuses(self):
        self.host.userspace = True
        with self.assertRaises(unlock.Stop):
            self.flow.execute()
        self.assertFalse(self.flashed())
        self.assertFalse(any("unlock" in args for args, _ in self.host.calls))

    def test_preflight_has_no_subprocess_or_usb_discovery(self):
        backend = types.SimpleNamespace(get_backend=lambda: object())
        usb = types.SimpleNamespace(__version__="test")
        with patch.object(unlock.shutil, "which", return_value="fake"), \
                patch.object(unlock.importlib, "import_module", side_effect=[usb, backend]):
            self.assertTrue(unlock.preflight("ru"))
        self.assertFalse(self.host.calls)

    def test_command_timeout_stops(self):
        with patch.object(unlock.subprocess, "run", side_effect=subprocess.TimeoutExpired("fastboot", 5)):
            with self.assertRaises(unlock.Stop):
                self.flow.run(["fastboot", "devices"], timeout=5)


class RecoveryTests(WorkflowFixture):
    def prepare(self, verified=True):
        backup.save_backup(self.flow.backup, ORIGINAL)
        self.flow.serial = "SERIAL"
        self.flow.env["ANDROID_SERIAL"] = "SERIAL"
        self.flow.original = ORIGINAL
        self.flow.marker_possible = True
        self.flow.marker_verified = verified
        import hashlib
        self.flow.state = {"version": 1, "serial": "SERIAL",
                           "sha256": hashlib.sha256(ORIGINAL).hexdigest()}
        self.flow.save_state("marker_verified" if verified else "marker_write_started")

    def test_recovery_locked_service_fastboot_restores_without_unlocking(self):
        self.prepare()
        self.assertTrue(self.flow.recover())
        self.assertEqual(self.host.flashed, ORIGINAL)
        self.assertEqual(len(self.rebooted()), 1)
        self.assertFalse(any("unlock" in args for args, _ in self.host.calls))
        self.assertFalse(self.flow.marker_possible)

    def test_recovery_waits_for_same_device_after_usb_disconnect(self):
        self.prepare()
        self.host.fastboot_delay = 30
        self.assertTrue(self.flow.recover())
        self.assertGreaterEqual(self.host.now, 30)
        self.assertEqual(self.host.flashed, ORIGINAL)

    def test_recovery_unknown_lock_state_preserves_marker_and_pending_state(self):
        self.prepare()
        self.host.query = "unknown"
        self.assertFalse(self.flow.recover(timeout=3))
        self.assertFalse(self.flashed())
        self.assertFalse(self.rebooted())
        import json
        state = json.loads(self.flow.receipt.read_text())
        self.assertEqual(state["phase"], "recovery_pending")
        self.assertTrue(state["marker_possible"])

    def test_recovery_locked_without_verified_marker_refuses_flash(self):
        self.prepare(verified=False)
        self.assertFalse(self.flow.recover(timeout=3))
        self.assertFalse(self.flashed())

    def test_recovery_uses_existing_diag_without_reboot_or_exploit(self):
        self.prepare(verified=False)
        self.host.adb_online = True
        self.assertTrue(self.flow.recover())
        self.assertFalse(self.flashed())
        self.assertFalse(any("reboot" in args for args, _ in self.host.calls))
        tools = [Path(args[1]).name for args, _ in self.host.calls if args[0] == sys.executable]
        self.assertEqual(tools, ["restore_chkcode.py"])

    def test_recovery_failed_diag_does_not_reboot_into_another_mode(self):
        self.prepare(verified=False)
        self.host.adb_online = True
        self.host.fail_at = "restore_chkcode.py"
        self.assertFalse(self.flow.recover())
        self.assertFalse(any("reboot" in args for args, _ in self.host.calls))
        self.assertFalse(self.flashed())

    def test_recovery_after_flash_failure_attempts_original_once(self):
        self.host.flash_failures = 1
        with self.assertRaises(unlock.Stop):
            self.flow.execute()
        self.assertTrue(self.flow.recover())
        self.assertEqual(len(self.flashed()), 2)  # Normal restore, then one recovery.
        self.assertEqual(len(self.rebooted()), 1)

    def test_recovery_write_failure_stops_without_endless_retry_or_reboot(self):
        self.prepare()
        self.host.fail_at = "flash"
        self.assertFalse(self.flow.recover())
        self.assertEqual(len(self.flashed()), 1)
        self.assertFalse(self.rebooted())

    def test_recovery_resumes_saved_session_after_process_restart(self):
        self.prepare()
        self.host.fastboot_delay = 10
        self.assertFalse(self.flow.recover(timeout=2))
        resumed = unlock.Workflow()
        resumed.load_session()
        self.assertTrue(resumed.recover())
        self.assertEqual(self.host.flashed, ORIGINAL)

    def test_recovery_retries_only_reboot_after_a_successful_flash(self):
        self.host.reboot_failures = 1
        with self.assertRaises(unlock.Stop):
            self.flow.execute()
        resumed = unlock.Workflow()
        resumed.load_session()
        self.assertTrue(resumed.recover())
        self.assertEqual(len(self.flashed()), 1)
        self.assertEqual(len(self.rebooted()), 2)

    def test_recovery_changed_backup_or_receipt_never_writes(self):
        self.prepare()
        self.flow.backup.write_bytes(ORIGINAL[:-1] + b"!")
        with self.assertRaises(unlock.Stop):
            unlock.Workflow().load_session()
        self.assertFalse(self.host.calls)

    def test_recovery_uses_second_copy_after_restart_if_primary_is_lost(self):
        self.prepare()
        backup.save_backup(self.flow.recovery_backup, ORIGINAL)
        self.flow.backup.unlink()
        resumed = unlock.Workflow()
        resumed.load_session()
        self.assertTrue(resumed.recover())
        self.assertEqual(Path(self.flashed()[0][-1]), self.flow.recovery_backup)
        self.assertEqual(self.host.flashed, ORIGINAL)

    def test_no_valid_backup_copy_refuses_recovery_without_device_commands(self):
        self.prepare()
        backup.save_backup(self.flow.recovery_backup, ORIGINAL)
        self.flow.backup.write_bytes(b"broken primary")
        self.flow.recovery_backup.write_bytes(b"broken second copy")
        with self.assertRaises(unlock.Stop):
            self.flow.recover()
        self.assertFalse(self.host.calls)

    def test_cli_fastboot_recovery_works_without_adb_or_pyusb(self):
        self.prepare()
        original_run = self.host.run
        def missing_adb(args, **kwargs):
            if args[0] == "adb":
                raise FileNotFoundError("ADB absent")
            return original_run(args, **kwargs)
        with patch.object(sys, "argv", ["unlock", "--recover"]), \
                patch("builtins.input", return_value="yes"), \
                patch.object(unlock, "preflight", side_effect=AssertionError("Not a recovery requirement")), \
                patch.object(unlock.subprocess, "run", missing_adb):
            self.assertEqual(unlock.main(), 0)
        self.assertEqual(self.host.flashed, ORIGINAL)

    def test_session_lock_prevents_concurrent_workflow_and_releases_after_error(self):
        lock_path = self.root / ".unlock.lock"
        with self.assertRaises(RuntimeError):
            with unlock.session_lock(lock_path):
                with self.assertRaises(OSError):
                    with unlock.session_lock(lock_path):
                        self.fail("Second workflow entered")
                raise RuntimeError("simulated process error")
        with unlock.session_lock(lock_path):
            pass

    def test_cli_error_automatically_invokes_recovery_and_reports_failure(self):
        self.host.fail_at = "unlock_chkcode.py"
        self.host.adb_online = True
        with patch.object(sys, "argv", ["unlock", "--unlock"]), patch("builtins.input", return_value="yes"), \
                patch.object(unlock, "preflight", return_value=True):
            self.assertEqual(unlock.main(), 1)
        self.assertTrue(any(Path(args[1]).name == "restore_chkcode.py"
                            for args, _ in self.host.calls if args[0] == sys.executable))
        self.assertFalse(self.flashed())

    def test_cli_error_before_marker_does_not_start_recovery(self):
        self.host.fail_at = "cve31317.py"
        with patch.object(sys, "argv", ["unlock", "--unlock"]), patch("builtins.input", return_value="yes"), \
                patch.object(unlock, "preflight", return_value=True):
            self.assertEqual(unlock.main(), 1)
        self.assertFalse(any(args[-1] == "get-state" for args, _ in self.host.calls))

    def test_sigint_during_marker_write_is_deferred_until_verified_then_rolls_back(self):
        import json
        import signal
        self.host.adb_online = True
        def cancel(name):
            if name == "unlock_chkcode.py":
                signal.raise_signal(signal.SIGINT)
        self.host.on_write = cancel
        with patch.object(sys, "argv", ["unlock", "--unlock"]), patch("builtins.input", return_value="yes"), \
                patch.object(unlock, "preflight", return_value=True):
            self.assertEqual(unlock.main(), 1)
        state = json.loads(self.flow.receipt.read_text())
        self.assertEqual(state["phase"], "rollback_diag_verified")
        self.assertTrue(state["marker_verified"])
        self.assertFalse(state["marker_possible"])
        self.assertFalse(any("reboot" in args for args, _ in self.host.calls))

    def test_sigterm_during_successful_flash_preserves_result_and_only_reboots(self):
        import json
        import signal
        def cancel(name):
            if name == "flash":
                signal.raise_signal(signal.SIGTERM)
        self.host.on_write = cancel
        previous = signal.getsignal(signal.SIGTERM)
        with patch.object(sys, "argv", ["unlock", "--unlock"]), patch("builtins.input", return_value="yes"), \
                patch.object(unlock, "preflight", return_value=True):
            self.assertEqual(unlock.main(), 1)
        self.assertEqual(signal.getsignal(signal.SIGTERM), previous)
        self.assertEqual(len(self.flashed()), 1)
        self.assertEqual(len(self.rebooted()), 1)
        state = json.loads(self.flow.receipt.read_text())
        self.assertFalse(state["marker_possible"])
        self.assertFalse(state["reboot_pending"])
        for args, kwargs in self.host.calls:
            if "flash" in args:
                if os.name == "nt":
                    self.assertEqual(kwargs["creationflags"], subprocess.CREATE_NEW_PROCESS_GROUP)
                    self.assertFalse(kwargs["creationflags"] & subprocess.CREATE_NEW_CONSOLE)
                else:
                    self.assertTrue(kwargs["start_new_session"])


class DevicePolicyTests(unittest.TestCase):
    def test_known_stock_formats_are_admitted_by_policy(self):
        for model in ("A101BM", "X01A"):
            for build in ("1.220PO.0686.a", "1.280MI.0686.a", "1.280"):
                validate_identity({"ro.product.model": model, "ro.build.version.sdk": "31",
                                   "ro.build.version.incremental": build,
                                   "ro.build.version.security_patch": "2022-10-01"})

    def test_unknown_patched_or_malformed_build_and_spl_are_rejected(self):
        original = {"ro.product.model": "X01A", "ro.build.version.sdk": "31",
                    "ro.build.version.incremental": "1.280PO.0686.a",
                    "ro.build.version.security_patch": "2022-10-01"}
        for key, values in {
            "ro.product.model": ("", "KYOCERA", "A101BM-other"),
            "ro.build.version.sdk": ("", "32", "36", "37"),
            "ro.build.version.incremental": ("", "1.219", "1.281", "2.280", "1.280-custom", "1.2800"),
            "ro.build.version.security_patch": ("", "unknown", "20221001", "2022-02-30", "2024-06-01", "2026-10-01"),
        }.items():
            for value in values:
                with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                    validate_identity({**original, key: value})


class SignalGuardTests(unittest.TestCase):
    def test_signal_outside_write_requests_cancellation(self):
        import signal
        guard = OperationSignals()
        with self.assertRaises(CancelRequested):
            guard.handle(signal.SIGTERM, None)

    def test_repeated_signals_wait_for_outermost_write_and_journal(self):
        import signal
        guard = OperationSignals()
        with guard.defer():
            with guard.defer():
                guard.handle(signal.SIGINT, None)
                guard.handle(signal.SIGTERM, None)
                guard.checkpoint()  # Still deferred.
            guard.checkpoint()      # Outer write/journal not finished.
        with self.assertRaises(CancelRequested):
            guard.checkpoint()
        self.assertIsNone(guard.pending)


class DiagRollbackTests(unittest.TestCase):
    def test_full_and_every_partial_marker_restore_exact_original_prefix(self):
        for count in range(1, 9):
            fs = Mock()
            fs.read_file.side_effect = [backup.MAGIC[:count] + ORIGINAL[count:], ORIGINAL]
            fs.lseek.return_value = 0
            fs.write.return_value = 8
            self.assertTrue(restore_chkcode.restore_prefix(fs, ORIGINAL))
            fs.write.assert_called_once_with(fs.open.return_value, ORIGINAL[:8])
            fs.close.assert_called_once()

    def test_already_original_needs_no_write(self):
        fs = Mock()
        fs.read_file.return_value = ORIGINAL
        self.assertFalse(restore_chkcode.restore_prefix(fs, ORIGINAL))
        fs.write.assert_not_called()

    def test_unknown_corruption_short_read_or_changed_tail_refuses_write(self):
        for data in (ORIGINAL[:-1], b"UNKNOWN!" + ORIGINAL[8:],
                     backup.MAGIC + ORIGINAL[8:-1] + b"!"):
            fs = Mock()
            fs.read_file.return_value = data
            with self.assertRaises(ValueError):
                restore_chkcode.restore_prefix(fs, ORIGINAL)
            fs.write.assert_not_called()

    def test_short_rollback_write_or_failed_readback_is_not_success(self):
        for count, readback in ((4, ORIGINAL), (8, backup.MAGIC + ORIGINAL[8:])):
            fs = Mock()
            fs.read_file.side_effect = [backup.MAGIC + ORIGINAL[8:], readback]
            fs.lseek.return_value = 0
            fs.write.return_value = count
            with self.assertRaises(ValueError):
                restore_chkcode.restore_prefix(fs, ORIGINAL)
            self.assertEqual(fs.write.call_count, 1)
            fs.close.assert_called_once()


class WriteRetryTests(unittest.TestCase):
    def test_protocol_writes_disable_transport_retries(self):
        import struct
        with patch.dict(sys.modules, {"diag_transport": types.SimpleNamespace(open_transport=Mock())}):
            spec = importlib.util.spec_from_file_location("fs_protocol_test", TOOLS / "fs_sys_call.py")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        tr = Mock()
        tr.xfer.return_value = b"\x4b\xfc\x00\x20" + struct.pack("<HHII", 3, 0, 8, 0)
        self.assertEqual(module.FSDiag(tr).write(0, backup.MAGIC), 8)
        self.assertEqual(tr.xfer.call_args.kwargs, {"tries": 1})


class TransportTests(unittest.TestCase):
    def test_multiple_diag_devices_or_serial_mismatch_refuse_before_claim(self):
        for count, serial in ((2, "SERIAL"), (1, "OTHER")):
            core = types.ModuleType("usb.core")
            util = types.ModuleType("usb.util")
            core.find = Mock(return_value=[types.SimpleNamespace(serial_number=serial)] * count)
            util.claim_interface = Mock()
            usb = types.ModuleType("usb")
            usb.core, usb.util = core, util
            with patch.dict(sys.modules, {"usb": usb, "usb.core": core, "usb.util": util}), \
                    patch.dict(os.environ, {"ANDROID_SERIAL": "SERIAL"}):
                spec = importlib.util.spec_from_file_location("transport_test", TOOLS / "diag_transport.py")
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                with self.assertRaises(RuntimeError):
                    module.open_transport()
                util.claim_interface.assert_not_called()


if __name__ == "__main__":
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        unittest.main()
