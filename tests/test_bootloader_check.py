"""Offline coverage of the automatic marker-free bootloader/driver check."""
import subprocess
from unittest.mock import patch

from test_safety import WorkflowFixture, unlock


class BootloaderCheckTests(WorkflowFixture):
    isolate_bootloader_check = False

    def setUp(self):
        super().setUp()
        self.host.properties["sys.boot_completed"] = "1"

    def assert_no_preparation_or_write(self):
        self.assertFalse(self.flow.marker_possible)
        self.assertFalse(self.flow.backup.exists())
        self.assertFalse(self.flow.receipt.exists())
        self.assertFalse(self.flashed())
        self.assertFalse(any(args[0] == unlock.sys.executable or "unlock" in args
                             for args, _ in self.host.calls))

    def test_normal_entry_checks_driver_and_returns_to_android_before_first_helper(self):
        self.flow.execute()
        calls = [args for args, _ in self.host.calls]
        first_helper = next(i for i, args in enumerate(calls) if args[0] == unlock.sys.executable)
        prefix = calls[:first_helper]
        self.assertIn(["fastboot", "-s", "SERIAL", "getvar", "is-userspace"], prefix)
        self.assertIn(["fastboot", "-s", "SERIAL", "oem", "device-info"], prefix)
        self.assertIn(["fastboot", "-s", "SERIAL", "reboot"], prefix)
        self.assertIn(["adb", "-s", "SERIAL", "shell", "getprop", "sys.boot_completed"], prefix)
        self.assertEqual(len(self.flashed()), 1)
        self.assertEqual(len(self.rebooted()), 2)

    def test_missing_fastboot_driver_stops_before_cve_backup_or_marker(self):
        self.host.fastboot_delay = 1000
        with self.assertRaisesRegex(unlock.Stop, "driver check failed"):
            self.flow.execute()
        self.assertEqual(self.host.now, 300)
        self.assert_no_preparation_or_write()

    def test_another_fastboot_serial_never_passes_driver_check(self):
        original = self.host.run

        def other_serial(args, **kwargs):
            if args == ["fastboot", "devices"]:
                return subprocess.CompletedProcess(args, 0, "OTHER\tfastboot\n", "")
            return original(args, **kwargs)

        with patch.object(unlock.subprocess, "run", other_serial), self.assertRaises(unlock.Stop):
            self.flow.execute()
        self.assert_no_preparation_or_write()

    def test_fastbootd_returns_to_android_without_preparation(self):
        self.host.userspace = True
        with self.assertRaisesRegex(unlock.Stop, "Bootloader mode not confirmed"):
            self.flow.execute()
        self.assertEqual(len(self.rebooted()), 1)
        self.assert_no_preparation_or_write()

    def test_unknown_or_conflicting_bootloader_mode_refuses_before_preparation(self):
        original = self.host.run
        for reply in ("", "is-userspace: no\nis-userspace: yes"):
            with self.subTest(reply=reply):
                def unknown_mode(args, **kwargs):
                    if args[-2:] == ["getvar", "is-userspace"]:
                        return subprocess.CompletedProcess(args, 0, "", reply)
                    return original(args, **kwargs)

                with patch.object(unlock.subprocess, "run", unknown_mode), self.assertRaises(unlock.Stop):
                    self.flow.execute()
                self.assert_no_preparation_or_write()

    def test_lock_query_fallback_is_checked_before_preparation(self):
        self.host.query = "getvar"
        self.flow.execute()
        calls = [args for args, _ in self.host.calls]
        query = calls.index(["fastboot", "-s", "SERIAL", "getvar", "unlocked"])
        helper = next(i for i, args in enumerate(calls) if args[0] == unlock.sys.executable)
        self.assertLess(query, helper)

    def test_unknown_lock_state_refuses_before_preparation(self):
        self.host.query = "unknown"
        with self.assertRaisesRegex(unlock.Stop, "lock state unknown"):
            self.flow.execute()
        self.assertEqual(len(self.rebooted()), 1)
        self.assert_no_preparation_or_write()

    def test_already_unlocked_phone_is_not_wiped_again(self):
        self.host.unlocked_at = 0
        with self.assertRaisesRegex(unlock.Stop, "already unlocked"):
            self.flow.execute()
        self.assert_no_preparation_or_write()

    def test_android_not_returning_prevents_cve_or_marker(self):
        self.host.properties["sys.boot_completed"] = ""
        with self.assertRaisesRegex(unlock.Stop, "sys.boot_completed"):
            self.flow.execute()
        self.assertEqual(self.host.now, 180)
        self.assert_no_preparation_or_write()

    def test_lost_return_reboot_reply_prevents_preparation(self):
        self.host.reboot_failures = 1
        with self.assertRaises(unlock.Stop):
            self.flow.execute()
        self.assert_no_preparation_or_write()

    def test_identity_changed_after_roundtrip_prevents_preparation(self):
        original = self.host.run

        def changed_identity(args, **kwargs):
            result = original(args, **kwargs)
            if args == ["fastboot", "-s", "SERIAL", "reboot"]:
                self.host.properties["ro.product.model"] = "OTHER"
            return result

        with patch.object(unlock.subprocess, "run", changed_identity), self.assertRaisesRegex(
                unlock.Stop, "identity changed"):
            self.flow.execute()
        self.assert_no_preparation_or_write()

    def test_pending_cve_cleanup_refuses_before_reboot_or_any_device_command(self):
        (self.root / unlock.STATE_NAME).write_text("{}")
        with self.assertRaisesRegex(unlock.Stop, "cleanup is pending"):
            self.flow.execute()
        self.assertFalse(self.host.calls)
        self.assert_no_preparation_or_write()
