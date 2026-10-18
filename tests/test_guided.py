"""Offline user flows: setup retries, saved sessions and refusal to guess unsafe state."""
import contextlib
import io
import json
import subprocess
import types
from unittest.mock import patch

from test_safety import WorkflowFixture, ORIGINAL, backup, unlock


class GuidedTests(WorkflowFixture):
    def setUp(self):
        super().setUp()
        self.flow.interactive = True
        self.args = types.SimpleNamespace(prepare=False, recover=False, unlock_prepared=False)
        self.enter(patch.object(unlock, "preflight", return_value=True))

    def prepared(self):
        self.flow.interactive = False
        self.flow.execute(prepare_only=True)
        self.flow.interactive = True
        self.host.calls.clear()

    def guide(self, answers):
        with patch("builtins.input", side_effect=answers):
            return self.flow.guided(self.args)

    def test_unauthorized_adb_can_be_authorized_without_restarting(self):
        self.host.adb_rows = "SERIAL unauthorized\n"
        def authorize():
            self.host.adb_rows = "SERIAL device\n"
            return "yes"
        with patch("builtins.input", side_effect=authorize):
            self.flow.select_adb()
        self.assertEqual(self.flow.serial, "SERIAL")
        self.assertFalse(self.flashed())

    def test_multiple_devices_require_one_device_after_confirmation(self):
        self.host.adb_rows = "SERIAL device\nOTHER device\n"
        def disconnect_other():
            self.host.adb_rows = "SERIAL device\n"
            return "yes"
        with patch("builtins.input", side_effect=disconnect_other):
            self.flow.select_adb()
        self.assertEqual(self.flow.serial, "SERIAL")

    def test_wrong_device_does_not_replace_saved_serial(self):
        self.prepared()
        self.host.adb_rows = "OTHER device\n"
        with patch("builtins.input", return_value="no"), self.assertRaises(unlock.Stop):
            self.flow.select_adb()
        self.assertEqual(self.flow.serial, "SERIAL")
        self.assertEqual(self.flow.env["ANDROID_SERIAL"], "SERIAL")

    def test_diag_driver_retry_runs_only_read_only_probes(self):
        self.flow.serial = "SERIAL"
        replies = iter([2, 0])
        original = self.host.run
        def probe(args, **kwargs):
            result = original(args, **kwargs)
            if args[0] == unlock.sys.executable and str(args[1]).endswith("diag_probe.py"):
                result.returncode = next(replies)
            return result
        with patch.object(unlock.subprocess, "run", probe), patch("builtins.input", return_value="yes"):
            self.flow.ensure_diag()
        tools = [str(args[1]) for args, _ in self.host.calls if args[0] == unlock.sys.executable]
        self.assertEqual(len(tools), 2)
        self.assertTrue(all(name.endswith("diag_probe.py") for name in tools))
        self.assertFalse(self.flashed())
        self.assertFalse(self.flow.backup.exists())

    def test_diag_identity_error_cannot_be_overridden_by_yes(self):
        result = subprocess.CompletedProcess([], 3, "", "wrong device")
        with patch.object(self.flow, "tool", return_value=result), patch("builtins.input") as question:
            with self.assertRaises(unlock.Stop):
                self.flow.ensure_diag()
        question.assert_not_called()

    def test_declining_driver_setup_stops_without_marker(self):
        result = subprocess.CompletedProcess([], 2, "", "driver missing")
        with patch.object(self.flow, "tool", return_value=result), patch("builtins.input", return_value="no"):
            with self.assertRaises(unlock.Declined):
                self.flow.ensure_diag()
        self.assertFalse(self.flow.marker_possible)

    def test_prepared_session_offers_real_resume_and_keeps_originals(self):
        self.prepared()
        self.assertEqual(self.guide(["yes"]), 0)
        self.assertEqual(self.host.flashed, ORIGINAL)
        self.assertEqual(backup.load_backup(self.flow.backup), ORIGINAL)
        self.assertEqual(backup.load_backup(self.flow.recovery_backup), ORIGINAL)
        tools = [str(args[1]) for args, _ in self.host.calls if args[0] == unlock.sys.executable]
        self.assertTrue(any(name.endswith("verify_chkcode.py") for name in tools))
        self.assertFalse(any(name.endswith("backup_chkcode.py") for name in tools))

    def test_prepared_session_can_be_declined_before_device_access(self):
        self.prepared()
        self.assertEqual(self.guide(["no"]), 0)
        self.assertFalse(self.host.calls)

    def test_backup_ready_receipt_is_resumable_without_overwriting_backups(self):
        self.prepared()
        self.flow.save_state("backup_ready")
        self.assertEqual(self.guide(["yes"]), 0)
        self.assertEqual(self.host.flashed, ORIGINAL)
        self.assertEqual(backup.load_backup(self.flow.backup), ORIGINAL)

    def test_pending_cleanup_is_completed_before_offering_a_new_unlock(self):
        path = self.root / unlock.STATE_NAME
        path.write_text(json.dumps({"version": 1, "serial": "SERIAL", "hidden_api": None, "usap": None}))
        with patch.object(self.flow, "cleanup_cve", side_effect=lambda: path.unlink()):
            self.assertEqual(self.guide(["yes", "no"]), 0)
        self.assertFalse(path.exists())
        self.assertFalse(self.host.calls)

    def test_corrupt_cleanup_receipt_cannot_be_confirmed_into_device_commands(self):
        path = self.root / unlock.STATE_NAME
        path.write_text("corrupted")
        self.assertEqual(self.guide(["no"]), 1)
        self.assertEqual(path.read_text(), "corrupted")
        self.assertFalse(self.host.calls)

    def test_invalid_cleanup_schema_is_explained_without_crashing_or_device_access(self):
        path = self.root / unlock.STATE_NAME
        path.write_text("{}")
        self.assertEqual(self.guide(["no"]), 1)
        self.assertFalse(self.host.calls)
        self.assertEqual(path.read_text(), "{}")

    def test_invalid_confirmation_is_explained_instead_of_silently_exiting(self):
        with patch("builtins.input", side_effect=["typo", "yes"]):
            self.assertTrue(self.flow.ask("Продолжить?", "Continue?"))

    def test_completed_session_is_success_without_device_access_or_question(self):
        self.prepared()
        self.flow.save_state("reboot_submitted", reboot_pending=False)
        with patch("builtins.input", side_effect=AssertionError("No prompt expected")):
            self.assertEqual(self.flow.guided(self.args), 0)
        self.assertFalse(self.host.calls)
    def test_marker_session_offers_recovery_without_a_new_unlock(self):
        self.prepared()
        self.flow.marker_possible = self.flow.marker_verified = True
        self.flow.save_state("marker_verified")
        self.assertEqual(self.guide(["yes"]), 0)
        self.assertEqual(len(self.flashed()), 1)
        self.assertFalse(any("unlock" in args for args, _ in self.host.calls))

    def test_recovery_after_completed_restore_only_reboots(self):
        self.prepared()
        self.flow.save_state("restored", reboot_pending=True)
        self.assertEqual(self.guide(["yes"]), 0)
        self.assertFalse(self.flashed())
        self.assertEqual(len(self.rebooted()), 1)

    def test_failed_recovery_is_retried_only_after_another_confirmation(self):
        self.prepared()
        self.flow.marker_possible = self.flow.marker_verified = True
        self.flow.save_state("marker_verified")
        self.host.flash_failures = 1
        self.assertEqual(self.guide(["yes", "yes"]), 0)
        self.assertEqual(len(self.flashed()), 2)
        self.assertFalse(any("unlock" in args for args, _ in self.host.calls))

    def test_declined_recovery_preserves_pending_session(self):
        self.prepared()
        self.flow.marker_possible = self.flow.marker_verified = True
        self.flow.save_state("marker_verified")
        self.assertEqual(self.guide(["no"]), 0)
        self.assertFalse(self.host.calls)
        self.assertTrue(json.loads(self.flow.receipt.read_text())["marker_possible"])

    def test_corrupt_receipt_reports_error_without_any_device_command(self):
        self.prepared()
        self.flow.receipt.write_text("broken JSON")
        self.assertEqual(self.guide(["no"]), 1)
        self.assertFalse(self.host.calls)
        self.assertEqual(self.flow.backup.read_bytes(), ORIGINAL)
        self.assertTrue((self.root / "balmunlock-last-error.txt").exists())

    def test_unknown_saved_phase_cannot_be_resumed_as_preparation(self):
        self.prepared()
        self.flow.save_state("unknown")
        self.assertEqual(self.guide(["no"]), 1)
        self.assertFalse(self.host.calls)

    def test_missing_originals_refuse_without_device_access(self):
        self.prepared()
        self.flow.backup.unlink()
        self.flow.recovery_backup.unlink()
        self.assertEqual(self.guide(["no"]), 1)
        self.assertFalse(self.host.calls)

    def test_damaged_primary_uses_receipt_verified_recovery_copy(self):
        self.prepared()
        self.flow.backup.write_bytes(b"damaged")
        self.assertEqual(self.guide(["yes"]), 0)
        self.assertEqual(self.flow.backup.read_bytes(), b"damaged")
        self.assertEqual(self.flashed()[0][-1], str(self.flow.recovery_backup))

    def test_orphan_backups_rebuild_receipt_only_after_live_comparison(self):
        self.prepared()
        self.flow.receipt.unlink()
        self.assertEqual(self.guide(["yes", "no"]), 0)
        state = json.loads(self.flow.receipt.read_text())
        self.assertEqual(state["phase"], "prepared")
        self.assertFalse(state["marker_possible"])
        self.assertFalse(self.flashed())
        tools = [str(args[1]) for args, _ in self.host.calls if args[0] == unlock.sys.executable]
        self.assertTrue(any(name.endswith("verify_chkcode.py") for name in tools))
        self.assertFalse(any(name.endswith("unlock_chkcode.py") for name in tools))

    def test_orphan_mismatching_live_data_never_creates_receipt_or_writes(self):
        self.prepared()
        self.flow.receipt.unlink()
        self.host.fail_at = "verify_chkcode.py"
        self.assertEqual(self.guide(["yes", "no"]), 1)
        self.assertFalse(self.flow.receipt.exists())
        self.assertFalse(self.flashed())
        self.assertEqual(self.flow.backup.read_bytes(), ORIGINAL)

    def test_orphan_single_original_can_create_missing_second_copy_without_overwrite(self):
        self.prepared()
        self.flow.receipt.unlink()
        self.flow.recovery_backup.unlink()
        backup.checksum_path(self.flow.recovery_backup).unlink()
        self.assertEqual(self.guide(["yes", "no"]), 0)
        self.assertEqual(backup.load_backup(self.flow.backup), ORIGINAL)
        self.assertEqual(backup.load_backup(self.flow.recovery_backup), ORIGINAL)

    def test_truncated_orphan_cannot_be_accepted_by_confirmation(self):
        self.prepared()
        self.flow.receipt.unlink()
        self.flow.backup.write_bytes(b"truncated")
        self.assertEqual(self.guide(["no"]), 1)
        self.assertFalse(self.host.calls)
        self.assertFalse(self.flow.receipt.exists())

    def test_explicit_recovery_without_session_explains_nothing_to_restore(self):
        self.args.recover = True
        self.assertEqual(self.guide([]), 0)
        self.assertFalse(self.host.calls)

    def test_cli_guided_completed_session_never_prints_erase_confirmation(self):
        self.prepared()
        self.flow.save_state("reboot_submitted", reboot_pending=False)
        output = io.StringIO()
        with patch.object(unlock.sys, "argv", ["unlock", "--guided"]), \
                patch("builtins.input", side_effect=AssertionError("No question expected")), contextlib.redirect_stdout(output):
            self.assertEqual(unlock.main(), 0)
        self.assertNotIn("ERASE", output.getvalue())
        self.assertFalse(self.host.calls)

    def test_recovered_session_can_retry_unlock_only_after_live_original_validation(self):
        self.prepared()
        self.flow.marker_verified = True
        self.flow.save_state("rollback_diag_verified", reboot_pending=False)
        self.assertEqual(self.guide(["yes"]), 0)
        self.assertEqual(self.host.flashed, ORIGINAL)
        tools = [str(args[1]) for args, _ in self.host.calls if args[0] == unlock.sys.executable]
        verify = next(i for i, name in enumerate(tools) if name.endswith("verify_chkcode.py"))
        write = next(i for i, name in enumerate(tools) if name.endswith("unlock_chkcode.py"))
        self.assertLess(verify, write)

    def test_mismatch_before_retried_unlock_keeps_recovery_receipt_and_originals(self):
        self.prepared()
        self.flow.marker_verified = True
        self.flow.save_state("rollback_diag_verified", reboot_pending=False)
        self.host.fail_at = "verify_chkcode.py"
        self.assertEqual(self.guide(["yes", "no"]), 1)
        state = json.loads(self.flow.receipt.read_text())
        self.assertEqual(state["phase"], "rollback_diag_verified")
        self.assertTrue(state["marker_verified"])
        self.assertFalse(self.flashed())
        self.assertEqual(backup.load_backup(self.flow.backup), ORIGINAL)

    def test_unknown_bootloader_mode_in_recovery_never_flashes(self):
        self.prepared()
        self.flow.marker_possible = self.flow.marker_verified = True
        self.flow.save_state("marker_verified")
        original = self.host.run
        def unknown_mode(args, **kwargs):
            result = original(args, **kwargs)
            if args[-2:] == ["getvar", "is-userspace"]:
                result.returncode, result.stderr = 1, "unknown command"
            return result
        with patch.object(unlock.subprocess, "run", unknown_mode):
            self.assertFalse(self.flow.recover(timeout=3))
        self.assertFalse(self.flashed())
        self.assertFalse(self.rebooted())


class FastbootSetupRetryTests(WorkflowFixture):
    isolate_bootloader_check = False

    def test_driver_can_be_installed_during_extended_wait_without_a_new_reboot(self):
        self.flow.interactive = True
        self.flow.serial = "SERIAL"
        self.host.properties["sys.boot_completed"] = "1"
        self.host.fastboot_delay = 1000
        identity = {key: value for key, value in self.host.properties.items() if key.startswith("ro.")}
        def installed():
            self.host.fastboot_delay = self.host.now
            return "yes"
        with patch("builtins.input", side_effect=installed):
            self.flow.check_bootloader(identity)
        self.assertEqual(self.host.now, 300)
        self.assertEqual(sum(args == ["adb", "-s", "SERIAL", "reboot", "bootloader"]
                             for args, _ in self.host.calls), 1)
        self.assertFalse(self.flashed())
