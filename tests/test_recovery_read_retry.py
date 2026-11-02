"""ADB restarts during read-only backup/readback must never replay a flash."""
import hashlib
from pathlib import Path
import unittest
from unittest.mock import patch

import test_recovery_both as fixture
recovery = fixture.recovery


class ReadRetryTests(unittest.TestCase):
    setUp = fixture.BothTests.setUp
    sleep = fixture.BothTests.sleep
    verify = fixture.BothTests.verify
    writes = fixture.BothTests.writes
    execute = fixture.BothTests.execute

    def fail_dd(self, *, persistent=False, code=255, after=None, partition="recovery_a"):
        original = self.host.run
        failures = []
        def interrupted(args, **kwargs):
            result = original(args, **kwargs)
            if (args[0] == "adb" and args[3:4] == ["shell"] and len(args) == 5 and
                    args[4].startswith("dd if=/dev/block/by-name/" + partition + " ") and
                    (persistent or not failures)):
                failures.append(args)
                temporary = next(word[3:] for word in args[4].split() if word.startswith("of="))
                self.host.temporary[temporary] = self.host.temporary[temporary][:-20]
                result.returncode = code
                result.stdout = result.stderr = ""
                if after:
                    after()
            return result
        return failures, patch.object(recovery.subprocess, "run", side_effect=interrupted)

    def test_second_dd_cut_short_by_adbd_restart_is_repeated_and_verified(self):
        self.host.use_slot("a")  # Real report: b backed up, then adbd dies during a.
        originals = dict(self.host.partitions)
        failures, transport = self.fail_dd()
        with transport:
            self.assertEqual(self.execute(), 0)
        self.assertEqual(len(failures), 1)
        dd_a = [args for args, _ in self.host.calls if len(args) == 5 and
                args[3] == "shell" and args[4].startswith("dd if=/dev/block/by-name/recovery_a ")]
        self.assertEqual(len(dd_a), 2)
        for name, original in originals.items():
            record = self.flow.state["partitions"][name]
            self.assertEqual((self.flow.folder / record["backup"]).read_bytes(), original)
            self.assertEqual(record["original_sha256"], hashlib.sha256(original).hexdigest())
        self.assertEqual(self.host.slot, "a")
        self.assertEqual(len(self.writes()), 2)
        self.assertEqual(self.flow.state["phase"], "completed")

    def test_persistent_disconnect_is_bounded_and_never_flashes(self):
        failures, transport = self.fail_dd(persistent=True)
        with transport, self.assertRaises(recovery.RecoveryReadInterrupted):
            self.execute()
        self.assertEqual(len(failures), 3)
        self.assertFalse(self.writes())

    def test_regular_dd_error_is_not_retried(self):
        failures, transport = self.fail_dd(code=1)
        with transport, self.assertRaises(recovery.RecoveryError):
            self.execute()
        self.assertEqual(len(failures), 1)
        self.assertFalse(self.writes())

    def test_slot_change_after_disconnect_stops_before_repeating_read(self):
        failures, transport = self.fail_dd(after=lambda: self.host.use_slot("a"))
        with transport, self.assertRaises(recovery.RecoveryError):
            self.execute()
        self.assertEqual(len(failures), 1)
        self.assertFalse(self.writes())

    def test_another_device_after_disconnect_is_not_accepted(self):
        failures, transport = self.fail_dd(after=lambda: setattr(self.host, "other_serial", True))
        with transport, self.assertRaises(recovery.RecoveryError):
            self.execute()
        self.assertEqual(len(failures), 1)
        self.assertFalse(self.writes())

    def test_partial_pull_is_replaced_before_local_backup_is_accepted(self):
        original, pulls = self.host.run, []
        def interrupted(args, **kwargs):
            result = original(args, **kwargs)
            if args[0] == "adb" and args[3:4] == ["pull"]:
                pulls.append(args)
                if len(pulls) == 1:
                    Path(args[-1]).write_bytes(b"partial")
                    result.returncode = 255
            return result
        with patch.object(recovery.subprocess, "run", side_effect=interrupted):
            self.assertEqual(self.execute(), 0)
        self.assertEqual(len(pulls), 3)
        for record in self.flow.state["partitions"].values():
            data = (self.flow.folder / record["backup"]).read_bytes()
            self.assertEqual(len(data), recovery.IMAGE_SIZE)
            self.assertEqual(hashlib.sha256(data).hexdigest(), record["original_sha256"])

    def test_lost_cleanup_reply_does_not_repeat_backup_or_flash(self):
        original, failed = self.host.run, []
        def interrupted(args, **kwargs):
            result = original(args, **kwargs)
            if args[3:6] == ["shell", "rm", "-f"] and not failed:
                failed.append(args)
                result.returncode = 255  # File was removed, reply was lost.
            return result
        with patch.object(recovery.subprocess, "run", side_effect=interrupted):
            self.assertEqual(self.execute(), 0)
        self.assertEqual(len(self.writes()), 2)
        self.assertEqual(sum(args[3:4] == ["pull"] for args, _ in self.host.calls), 2)

    def test_disconnect_during_final_hash_does_not_replay_flash(self):
        original, failed = self.host.run, []
        def interrupted(args, **kwargs):
            result = original(args, **kwargs)
            if len(self.writes()) == 2 and args[3:5] == ["shell", "sha256sum"] and not failed:
                failed.append(args)
                result.returncode = 255
            return result
        with patch.object(recovery.subprocess, "run", side_effect=interrupted):
            self.assertEqual(self.execute(), 0)
        self.assertEqual(len(self.writes()), 2)
        self.assertEqual(self.flow.state["phase"], "completed")

    def test_flash_returning_255_is_never_retried(self):
        original = self.host.run
        def interrupted(args, **kwargs):
            result = original(args, **kwargs)
            if args[0] == "fastboot" and args[3:4] == ["flash"]:
                result.returncode = 255
            return result
        with patch.object(recovery.subprocess, "run", side_effect=interrupted), self.assertRaises(recovery.RecoveryError):
            self.execute()
        self.assertEqual(len(self.writes()), 1)

    def test_restart_during_initial_twrp_properties_is_waited_out(self):
        original, failed = self.host.run, []
        def interrupted(args, **kwargs):
            result = original(args, **kwargs)
            if self.host.mode == "recovery" and args[3:] == ["shell", "getprop"] and not failed:
                failed.append(args)
                result.returncode = 255
            return result
        with patch.object(recovery.subprocess, "run", side_effect=interrupted):
            self.assertEqual(self.execute(), 0)
        self.assertEqual(len(failed), 1)
        self.assertEqual(len(self.writes()), 2)

    def test_read_retry_refuses_device_writes_or_unowned_paths(self):
        self.flow.bind_slot("_b")
        self.flow.both_slots = True
        self.flow.folder = self.root
        for args in (("reboot", "bootloader"),
                     ("shell", "dd if=/tmp/input of=/dev/block/by-name/recovery_a"),
                     ("shell", "rm", "-f", "/dev/block/by-name/recovery_a"),
                     ("pull", "/dev/block/by-name/recovery_a", str(self.root / "unexpected"))):
            with self.subTest(args=args), self.assertRaises(recovery.RecoveryError):
                self.flow.adb_read(*args)
        self.assertFalse(self.host.calls)
