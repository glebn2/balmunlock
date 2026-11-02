"""Two independent partitions, failure boundaries and immutable active-slot binding."""
import contextlib
import hashlib
import io
from pathlib import Path
import signal
import unittest
from unittest.mock import patch

import test_recovery_install as fixture
recovery = fixture.recovery


class BothTests(unittest.TestCase):
    setUp = fixture.RecoveryTests.setUp
    sleep = fixture.RecoveryTests.sleep
    verify = fixture.RecoveryTests.verify
    writes = fixture.RecoveryTests.writes

    def execute(self, answers=None):
        with patch("builtins.input", side_effect=answers or ["both", "yes"]):
            return self.flow.execute()

    def assert_both(self, slot):
        self.host.use_slot(slot)
        originals = dict(self.host.partitions)
        self.assertEqual(self.execute(), 0)
        state = self.flow.state
        self.assertEqual(state["phase"], "completed")
        self.assertEqual(state["target_mode"], "both")
        self.assertNotIn("partition", state)  # No ambiguous single restore target.
        self.assertNotIn("backup", state)
        self.assertEqual(self.host.slot, slot)
        self.assertFalse(state["slot_switch_requested"])
        self.assertEqual(set(state["verified_partitions"]), set(originals))
        for partition, original in originals.items():
            record = state["partitions"][partition]
            self.assertEqual((self.flow.folder / record["backup"]).read_bytes(), original)
            self.assertEqual(record["original_sha256"], hashlib.sha256(original).hexdigest())
            self.assertEqual(record["phase"], "partition_verified")
            self.assertEqual(self.host.partitions[partition], self.data)
        other = "b" if slot == "a" else "a"
        self.assertEqual([args[4] for args, _ in self.writes()], ["recovery_" + other, "recovery_" + slot])
        self.assertTrue(all(options["timeout"] is None for _, options in self.writes()))
        self.assertFalse(any(any(arg.startswith("--set-active") for arg in args) for args, _ in self.host.calls))
        calls = [args for args, _ in self.host.calls]
        pulls = [i for i, args in enumerate(calls) if args[0] == "adb" and args[3:4] == ["pull"]]
        flashes = [i for i, args in enumerate(calls) if args[0] == "fastboot" and args[3:4] == ["flash"]]
        self.assertEqual(len(pulls), 2)
        self.assertLess(max(pulls), min(flashes))
        self.assertEqual(self.host.mode, "recovery")

    def test_both_from_b_keeps_b_and_saves_two_distinct_originals_before_writing(self):
        self.assert_both("b")

    def test_both_from_a_keeps_a_and_saves_two_distinct_originals_before_writing(self):
        self.assert_both("a")

    def test_both_already_installed_backs_up_both_without_flash(self):
        for partition in self.host.partitions:
            self.host.partitions[partition] = self.data
        self.assertEqual(self.execute(), 0)
        self.assertFalse(self.writes())
        self.assertEqual(len(self.flow.state["partitions"]), 2)
        self.assertEqual(set(self.flow.state["verified_partitions"]), set(self.host.partitions))

    def test_matching_inactive_partition_is_skipped(self):
        self.host.partitions["recovery_a"] = self.data
        self.assertEqual(self.execute(), 0)
        self.assertEqual([args[4] for args, _ in self.writes()], ["recovery_b"])

    def test_matching_active_partition_is_skipped(self):
        self.host.partitions["recovery_b"] = self.data
        self.assertEqual(self.execute(), 0)
        self.assertEqual([args[4] for args, _ in self.writes()], ["recovery_a"])

    def test_second_bad_backup_prevents_every_flash(self):
        original_run, pulls = self.host.run, []
        def bad_second(args, **kwargs):
            result = original_run(args, **kwargs)
            if args[0] == "adb" and args[3:4] == ["pull"]:
                pulls.append(args)
                if len(pulls) == 2:
                    Path(args[-1]).write_bytes(b"bad")
            return result
        with patch.object(recovery.subprocess, "run", side_effect=bad_second), self.assertRaises(recovery.RecoveryError):
            self.execute()
        self.assertFalse(self.writes())
        self.assertEqual(set(self.flow.state["partitions"]), {"recovery_a"})

    def test_wrong_second_partition_size_stops_before_temporary_boot(self):
        original_run = self.host.run
        def wrong_size(args, **kwargs):
            result = original_run(args, **kwargs)
            if args[-2:] == ["getvar", "partition-size:recovery_b"]:
                result.stderr = "partition-size:recovery_b: 0x100\n"
            return result
        with patch.object(recovery.subprocess, "run", side_effect=wrong_size), self.assertRaises(recovery.RecoveryError):
            self.execute()
        self.assertFalse(self.writes())
        self.assertFalse(any(args[0] == "fastboot" and args[3:4] == ["boot"] for args, _ in self.host.calls))

    def test_first_flash_failure_keeps_active_original_and_does_not_retry(self):
        original_b = self.host.partitions["recovery_b"]
        self.host.flash_failure = True
        with self.assertRaises(recovery.RecoveryError):
            self.execute()
        self.assertEqual(len(self.writes()), 1)
        self.assertEqual(self.host.partitions["recovery_b"], original_b)
        self.assertEqual(self.flow.state["partitions"]["recovery_a"]["phase"], "flash_started")
        self.assertEqual(self.flow.state["partitions"]["recovery_b"]["phase"], "backup_verified")
        self.assertTrue(self.flow.state["flash_may_have_changed"])

    def test_second_flash_failure_records_partial_result_without_success(self):
        original_run = self.host.run
        def fail_active(args, **kwargs):
            if args[0] == "fastboot" and args[3:5] == ["flash", "recovery_b"]:
                self.host.flash_failure = True
            return original_run(args, **kwargs)
        with patch.object(recovery.subprocess, "run", side_effect=fail_active), self.assertRaises(recovery.RecoveryError):
            self.execute()
        self.assertEqual(len(self.writes()), 2)
        self.assertEqual(self.host.partitions["recovery_a"], self.data)
        self.assertEqual(self.flow.state["partitions"]["recovery_a"]["phase"], "flash_command_completed")
        self.assertEqual(self.flow.state["partitions"]["recovery_b"]["phase"], "flash_started")
        self.assertNotEqual(self.flow.state["phase"], "completed")
        self.assertNotIn("recovery_ready", self.flow.state)

    def test_slot_change_between_writes_prevents_second_write(self):
        original_b = self.host.partitions["recovery_b"]
        original_run = self.host.run
        def changed_slot(args, **kwargs):
            result = original_run(args, **kwargs)
            if args[0] == "fastboot" and args[3:5] == ["flash", "recovery_a"]:
                self.host.use_slot("a")
            return result
        with patch.object(recovery.subprocess, "run", side_effect=changed_slot), self.assertRaises(recovery.RecoveryError):
            self.execute()
        self.assertEqual(len(self.writes()), 1)
        self.assertEqual(self.host.partitions["recovery_b"], original_b)

    def test_image_change_between_writes_prevents_second_write(self):
        original_run = self.host.run
        def changed_image(args, **kwargs):
            result = original_run(args, **kwargs)
            if args[0] == "fastboot" and args[3:5] == ["flash", "recovery_a"]:
                (self.flow.folder / "recovery.img").write_bytes(b"changed")
            return result
        with patch.object(recovery.subprocess, "run", side_effect=changed_image), self.assertRaises(ValueError):
            self.execute()
        self.assertEqual(len(self.writes()), 1)

    def test_backup_change_between_writes_prevents_second_write(self):
        original_run = self.host.run
        def changed_backup(args, **kwargs):
            result = original_run(args, **kwargs)
            if args[0] == "fastboot" and args[3:5] == ["flash", "recovery_a"]:
                (self.flow.folder / "recovery_b.before-install.img").write_bytes(b"changed")
            return result
        with patch.object(recovery.subprocess, "run", side_effect=changed_backup), self.assertRaises(recovery.RecoveryError):
            self.execute()
        self.assertEqual(len(self.writes()), 1)

    def test_second_readback_wrong_path_cannot_report_completed(self):
        original_run = self.host.run
        def wrong_path(args, **kwargs):
            result = original_run(args, **kwargs)
            if len(self.writes()) == 2 and args[3:] == ["shell", "sha256sum", "/dev/block/by-name/recovery_b"]:
                result.stdout = hashlib.sha256(self.data).hexdigest() + "  /dev/block/by-name/recovery_a\n"
            return result
        with patch.object(recovery.subprocess, "run", side_effect=wrong_path), self.assertRaises(recovery.RecoveryError):
            self.execute()
        self.assertEqual(self.flow.state["partitions"]["recovery_a"]["phase"], "partition_verified")
        self.assertNotEqual(self.flow.state["phase"], "completed")

    def test_cancellation_after_first_write_does_not_start_second(self):
        self.host.cancel_flash = signal.SIGINT
        with self.assertRaises(KeyboardInterrupt):
            self.execute()
        self.assertEqual(len(self.writes()), 1)
        self.assertEqual(self.flow.state["partitions"]["recovery_a"]["phase"], "flash_command_completed")
        self.assertEqual(self.flow.state["partitions"]["recovery_b"]["phase"], "backup_verified")

    def test_both_overrides_prior_single_slot_choice_without_switching(self):
        self.assertEqual(self.execute(["a", "both", "yes"]), 0)
        self.assertEqual(self.host.slot, "b")
        self.assertFalse(any(any(arg.startswith("--set-active") for arg in args) for args, _ in self.host.calls))

    def test_single_slot_can_be_selected_after_both(self):
        self.assertEqual(self.execute(["both", "a", "yes"]), 0)
        self.assertEqual(self.host.slot, "a")
        self.assertEqual(self.flow.state["target_mode"], "single")
        self.assertEqual([args[4] for args, _ in self.writes()], ["recovery_a"])

    def test_declining_both_does_not_reboot_or_write(self):
        self.assertEqual(self.execute(["both", "no"]), 0)
        self.assertIsNone(self.flow.folder)
        self.assertFalse(any("reboot" in args or "flash" in args for args, _ in self.host.calls))

    def test_english_both_prompt_and_completion_name_both_partitions(self):
        self.flow.lang = "en"
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(self.execute(), 0)
        self.assertIn("both recovery_a and recovery_b", out.getvalue())
        self.assertIn("Both recovery partitions verified", out.getvalue())
