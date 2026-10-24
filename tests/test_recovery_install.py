"""Host-only recovery regression tests. All device subprocesses are simulated."""
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import signal
import struct
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import recovery_image as artifact
import recovery_install as recovery
import unlock
import combined_workflow


class ImageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix=".test-balmunlock-", dir=ROOT)
        self.addCleanup(self.tmp.cleanup)
        self.image = Path(self.tmp.name) / "image.img"
        data = bytearray(8192)
        data[:8] = b"ANDROID!"
        struct.pack_into("<I", data, 36, 4096)
        struct.pack_into("<I", data, 40, 2)
        data[-64:-60] = b"AVBf"
        self.data = bytes(data)
        self.image.write_bytes(self.data)
        self.addCleanup(patch.stopall)
        patch.object(artifact, "IMAGE_SIZE", len(self.data)).start()
        patch.object(artifact, "IMAGE_SHA256", hashlib.sha256(self.data).hexdigest()).start()

    def test_valid_hash_and_structure(self):
        self.assertEqual(artifact.verify_image(self.image), self.image.resolve())

    def test_truncated_and_altered_images_are_rejected(self):
        for data in (self.data[:-1], self.data[:-1] + b"!"):
            self.image.write_bytes(data)
            with self.assertRaises(ValueError):
                artifact.verify_image(self.image)

    def test_structure_is_checked_even_with_a_matching_digest(self):
        for offset in (0, 36, 40, len(self.data) - 64):
            data = bytearray(self.data)
            data[offset] ^= 1
            self.image.write_bytes(data)
            with patch.object(artifact, "IMAGE_SHA256", hashlib.sha256(data).hexdigest()), self.assertRaises(ValueError):
                artifact.verify_image(self.image)


class FakeDevice:
    def __init__(self, image):
        self.image = image
        self.mode = "device"
        self.calls = []
        self.partition = b"original" * 512
        self.temporary = {}
        self.unlocked = "yes"
        self.userspace = "no"
        self.slot = "b"
        self.snapshot = "none"
        self.partition_size = len(self.partition)
        self.extra = False
        self.other_serial = False
        self.pull_bad = False
        self.flash_failure = False
        self.readback_bad = False
        self.fail_ram_boot = False
        self.cancel_flash = None
        self.flow = None
        self.now = 0
        self.fallback = False
        self.unlocked_conflict = False
        self.props = {"ro.boot.serialno": "SERIAL", "ro.product.model": "A101BM",
                      "ro.product.vendor.model": "A101BM", "ro.boot.slot_suffix": "_b",
                      "ro.build.version.sdk": "31", "ro.build.version.incremental": artifact.STOCK_BUILD,
                      "ro.build.fingerprint": artifact.STOCK_FINGERPRINT,
                      "ro.vendor.build.fingerprint": artifact.VENDOR_FINGERPRINT, "sys.boot_completed": "1",
                      "ro.build.version.security_patch": "2023-09-01"}
        self.recovery_props = {"ro.boot.serialno": "SERIAL", "ro.product.vendor.model": "Phone",
                               "ro.boot.slot_suffix": "_b", "ro.twrp.version": artifact.TWRP_VERSION,
                               "ro.vendor.build.fingerprint": artifact.TWRP_FINGERPRINT}

    def run(self, args, **kwargs):
        args = list(map(str, args))
        self.calls.append((args, kwargs))
        out, err, rc = "", "", 0
        if args == ["adb", "devices"]:
            out = "List of devices attached\n"
            if self.mode != "fastboot":
                out += f"{'OTHER' if self.other_serial else 'SERIAL'}\t{self.mode}\n"
            if self.extra:
                out += "SECOND\tunauthorized\n"
        elif args == ["fastboot", "devices"]:
            if self.mode == "fastboot":
                out = f"{'OTHER' if self.other_serial else 'SERIAL'}\tfastboot\n"
        else:
            assert args[1:3] == ["-s", "SERIAL"], args
            command = args[3:]
            if args[0] == "fastboot":
                assert self.mode == "fastboot", args
                if command[0] == "getvar":
                    name = command[1]
                    values = {"is-userspace": self.userspace, "unlocked": self.unlocked,
                              "current-slot": self.slot, "product": "lito", "slot-count": "2",
                              "snapshot-update-status": self.snapshot,
                              "partition-size:recovery_b": hex(self.partition_size)}
                    err = f"(bootloader) {name}: {values[name]}\nFinished. Total time: 0.001s\n"
                    if name == "unlocked" and self.fallback:
                        rc, err = 1, "FAILED (unknown variable)"
                    elif name == "unlocked" and self.unlocked_conflict:
                        err += "unlocked: no\n"
                elif command == ["oem", "device-info"]:
                    err = "(bootloader) Device unlocked: true\n"
                elif command[0] == "boot":
                    if self.fail_ram_boot:
                        rc = 1
                    else:
                        self.mode = "recovery"
                elif command[0] == "flash":
                    assert command[1] == "recovery_b", args
                    if self.cancel_flash:
                        self.flow.signals.handle(self.cancel_flash, None)
                    self.partition = Path(command[2]).read_bytes()
                    if self.flash_failure or self.readback_bad:
                        self.partition = b"?" * len(self.partition)
                    rc = 1 if self.flash_failure else 0
                elif command == ["reboot", "recovery"]:
                    self.mode = "recovery"
                elif command == ["reboot"]:
                    self.mode = "device"
                else:
                    raise AssertionError(args)
            elif command == ["shell", "getprop"]:
                props = self.recovery_props if self.mode == "recovery" else self.props
                out = "".join(f"[{k}]: [{v}]\n" for k, v in props.items())
            elif command[:2] == ["shell", "getprop"] and len(command) == 3:
                props = self.recovery_props if self.mode == "recovery" else self.props
                out = props.get(command[2], "") + "\n"
            elif command == ["reboot", "bootloader"]:
                self.mode = "fastboot"
            elif command == ["reboot"]:
                self.mode = "device"
            elif command == ["shell", "id", "-u"]:
                out = "0\n"
            elif command == ["shell", "blockdev", "--getsize64", recovery.BLOCK]:
                out = str(len(self.partition)) + "\n"
            elif command[:2] == ["shell", "sha256sum"]:
                path = command[2]
                data = self.partition if path == recovery.BLOCK else self.temporary[path]
                out = f"{hashlib.sha256(data).hexdigest()}  {path}\n"
            elif command[0] == "shell" and command[1].startswith("dd "):
                assert f"if={recovery.BLOCK} " in command[1]
                name = next(item[3:] for item in command[1].split() if item.startswith("of="))
                self.temporary[name] = self.partition
            elif command[0] == "pull":
                Path(command[2]).write_bytes(b"bad" if self.pull_bad else self.temporary[command[1]])
            elif command[:2] == ["shell", "rm"]:
                del self.temporary[command[2]]
            else:
                raise AssertionError(args)
        return subprocess.CompletedProcess(args, rc, out, err)


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix=".test-balmunlock-", dir=ROOT)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "recovery").mkdir()
        self.image = self.root / "recovery" / artifact.IMAGE_NAME
        self.data = b"TWRP!!!!" * 512
        self.image.write_bytes(self.data)
        self.host = FakeDevice(self.image)
        self.flow = recovery.RecoveryInstaller(self.root)
        self.host.flow = self.flow
        self.addCleanup(patch.stopall)
        patch.object(recovery, "IMAGE_SIZE", len(self.data)).start()
        patch.object(recovery, "IMAGE_SHA256", hashlib.sha256(self.data).hexdigest()).start()
        patch.object(recovery, "verify_image", side_effect=self.verify).start()
        patch.object(recovery.shutil, "which", return_value="tool").start()
        patch.object(recovery.subprocess, "run", side_effect=self.host.run).start()
        patch.object(recovery.time, "monotonic", side_effect=lambda: self.host.now).start()
        patch.object(recovery.time, "sleep", side_effect=self.sleep).start()
        patch("builtins.input", return_value="yes").start()
        self.flow.env.pop("ANDROID_SERIAL", None)
        patch("sys.stdout", new=io.StringIO()).start()

    def sleep(self, seconds):
        self.host.now += seconds

    def verify(self, path):
        path = Path(path)
        if path.read_bytes() != self.data:
            raise ValueError("bad image")
        return path

    def writes(self):
        return [(args, kwargs) for args, kwargs in self.host.calls if "flash" in args]

    def reboots(self):
        return [args for args, _ in self.host.calls if "reboot" in args]

    def test_full_install_backs_up_original_and_verifies_android_without_unlock_helpers(self):
        original = self.host.partition
        self.assertEqual(self.flow.execute(), 0)
        state = json.loads((self.flow.folder / "state.json").read_text())
        self.assertEqual(state["phase"], "completed")
        self.assertEqual((self.flow.folder / state["backup"]).read_bytes(), original)
        self.assertEqual(state["original_sha256"], hashlib.sha256(original).hexdigest())
        self.assertEqual(self.host.mode, "device")
        self.assertEqual(self.host.partition, self.data)
        self.assertEqual(len(self.writes()), 1)
        args, options = self.writes()[0]
        self.assertEqual(args[3:5], ["flash", "recovery_b"])
        self.assertIsNone(options["timeout"])
        self.assertFalse(any("su" in args or "root" in args or "set_active" in args or "flashing" in args
                             or "--disable-verity" in args or "--disable-verification" in args for args, _ in self.host.calls))
        self.assertFalse((self.root / "sessions").exists())

    def test_bad_artifact_refuses_before_any_device_discovery(self):
        self.image.write_bytes(b"bad")
        with self.assertRaises(ValueError):
            self.flow.execute()
        self.assertFalse(self.host.calls)

    def test_missing_artifact_refuses_before_device_access(self):
        self.image.unlink()
        with self.assertRaises(recovery.RecoveryError):
            self.flow.execute()
        self.assertFalse(self.host.calls)

    def test_x01a_other_build_vendor_and_slot_refuse_before_reboot(self):
        for key, value in (("ro.product.model", "X01A"), ("ro.boot.slot_suffix", "_a"),
                           ("ro.build.version.incremental", "1.270PO.0682.a"),
                           ("ro.vendor.build.fingerprint", "other"), ("sys.boot_completed", "0")):
            before = self.host.props[key]
            self.host.props[key] = value
            with self.assertRaises(recovery.RecoveryError):
                self.flow.execute()
            self.host.props[key] = before
        self.assertFalse(self.reboots())
        self.assertFalse(self.writes())

    def test_other_selected_serial_multiple_and_unauthorized_devices_refuse(self):
        self.host.extra = True
        with self.assertRaises(recovery.RecoveryError):
            self.flow.execute()
        self.host.extra = False
        self.flow.env["ANDROID_SERIAL"] = "OTHER"
        with self.assertRaises(recovery.RecoveryError):
            self.flow.execute()
        self.flow.env.pop("ANDROID_SERIAL")
        self.host.mode = "unauthorized"
        with self.assertRaises(recovery.RecoveryError):
            self.flow.execute()
        self.assertFalse(self.reboots())

    def test_decline_does_not_reboot_or_create_a_backup_directory(self):
        with patch("builtins.input", return_value="no"):
            self.assertEqual(self.flow.execute(), 0)
        self.assertFalse(self.reboots())
        self.assertFalse((self.root / "recovery-sessions").exists())

    def test_locked_unknown_conflicting_and_fastbootd_never_ram_boot_or_flash(self):
        for key, value in (("unlocked", "no"), ("unlocked", "unknown"),
                           ("unlocked_conflict", True), ("userspace", "yes"),
                           ("slot", "a"), ("snapshot", "merging"), ("partition_size", 1)):
            old = getattr(self.host, key)
            setattr(self.host, key, value)
            self.host.mode = "device"
            with self.assertRaises(recovery.RecoveryError):
                self.flow.execute()
            setattr(self.host, key, old)
        self.assertFalse(any(args[3:4] == ["boot"] for args, _ in self.host.calls))
        self.assertFalse(self.writes())

    def test_supported_lock_state_fallback(self):
        self.host.fallback = True
        self.assertEqual(self.flow.execute(), 0)
        self.assertTrue(any(args[-2:] == ["oem", "device-info"] for args, _ in self.host.calls))

    def test_pending_unlock_refuses_before_reboot_and_leaves_its_files_untouched(self):
        session = recovery.SessionStore(self.root).create("SERIAL")
        path = session.root / "chkcode_backup.device.json"
        data = json.dumps({"version": 1, "serial": "SERIAL", "phase": "flash_started", "marker_possible": True})
        path.write_text(data)
        with self.assertRaises(recovery.RecoveryError):
            self.flow.execute()
        self.assertEqual(path.read_text(), data)
        self.assertFalse(self.reboots())

    def test_completed_unlock_session_is_not_overwritten(self):
        session = recovery.SessionStore(self.root).create("SERIAL")
        path = session.root / "chkcode_backup.device.json"
        data = json.dumps({"version": 1, "serial": "SERIAL", "phase": "reboot_submitted", "marker_possible": False})
        path.write_text(data)
        self.assertEqual(self.flow.execute(), 0)
        self.assertEqual(path.read_text(), data)

    def test_recovery_fingerprint_is_the_recorded_twrp_fingerprint_not_stock_vendor(self):
        self.host.recovery_props["ro.vendor.build.fingerprint"] = artifact.VENDOR_FINGERPRINT
        with self.assertRaises(recovery.RecoveryError):
            self.flow.execute()
        self.assertFalse(self.writes())

    def test_old_recovery_model_version_and_fingerprint_stop_before_flash(self):
        for key, value in (("ro.product.vendor.model", "A101BM"),
                           ("ro.twrp.version", "3.7.1_12-0"),
                           ("ro.vendor.build.fingerprint", "old-twrp-fingerprint")):
            with self.subTest(key=key):
                original = self.host.recovery_props[key]
                self.host.recovery_props[key] = value
                self.host.mode = "device"
                with self.assertRaises(recovery.RecoveryError):
                    self.flow.execute()
                self.host.recovery_props[key] = original
                self.assertFalse(self.writes())

    def test_corrupt_pull_keeps_partial_backup_and_never_flashes(self):
        self.host.pull_bad = True
        with self.assertRaises(recovery.RecoveryError):
            self.flow.execute()
        self.assertTrue(list(self.flow.folder.glob("*.part")))
        self.assertFalse(self.writes())

    def test_failed_ram_boot_never_flashes(self):
        self.host.fail_ram_boot = True
        with self.assertRaises(recovery.RecoveryError):
            self.flow.execute()
        self.assertFalse(self.writes())

    def test_flash_failure_keeps_backup_and_never_retries_or_reboots(self):
        self.host.flash_failure = True
        with self.assertRaises(recovery.RecoveryError):
            self.flow.execute()
        self.assertEqual(len(self.writes()), 1)
        self.assertEqual(self.host.mode, "fastboot")
        self.assertEqual(self.flow.state["phase"], "flash_started")
        self.assertTrue((self.flow.folder / self.flow.state["backup"]).exists())

    def test_bad_readback_never_reboots_into_android_or_auto_restores(self):
        self.host.readback_bad = True
        with self.assertRaises(recovery.RecoveryError):
            self.flow.execute()
        self.assertEqual(len(self.writes()), 1)
        self.assertEqual(self.host.mode, "recovery")

    def test_already_installed_skips_write_and_never_calls_its_backup_stock(self):
        self.host.partition = self.data
        self.assertEqual(self.flow.execute(), 0)
        self.assertFalse(self.writes())
        self.assertTrue(self.flow.state["original_was_pinned_twrp"])

    def test_repeated_runs_preserve_old_backup_and_receipt(self):
        self.flow.execute()
        first = self.flow.folder
        before = (first / "state.json").read_bytes()
        self.flow.execute()
        self.assertNotEqual(first, self.flow.folder)
        self.assertEqual((first / "state.json").read_bytes(), before)

    def test_cancellation_is_deferred_until_flash_and_journal_complete(self):
        self.host.cancel_flash = signal.SIGINT
        with self.assertRaises(KeyboardInterrupt):
            self.flow.execute()
        self.assertEqual(self.flow.state["phase"], "flash_command_completed")
        self.assertEqual(self.host.mode, "fastboot")
        self.assertEqual(len(self.writes()), 1)
        options = self.writes()[0][1]
        self.assertIsNone(options["timeout"])
        if os.name == "nt":
            self.assertEqual(options["creationflags"], subprocess.CREATE_NEW_PROCESS_GROUP)
        else:
            self.assertTrue(options["start_new_session"])

    def test_disconnect_with_another_serial_stops_before_flash(self):
        original = self.host.run
        def changed(args, **kwargs):
            result = original(args, **kwargs)
            if list(map(str, args))[3:4] == ["boot"]:
                self.host.other_serial = True
            return result
        with patch.object(recovery.subprocess, "run", side_effect=changed), self.assertRaises(recovery.RecoveryError):
            self.flow.execute()
        self.assertFalse(self.writes())

    def test_changed_source_during_backup_is_not_accepted(self):
        original = self.host.run
        def changing(args, **kwargs):
            result = original(args, **kwargs)
            if list(map(str, args))[3:4] == ["shell"] and "dd if=" in str(args):
                self.host.partition = b"!" * len(self.host.partition)
            return result
        with patch.object(recovery.subprocess, "run", side_effect=changing), self.assertRaises(recovery.RecoveryError):
            self.flow.execute()
        self.assertFalse(self.writes())

    def test_slot_is_rechecked_after_backup_before_flash(self):
        original = self.host.run
        def changed(args, **kwargs):
            result = original(args, **kwargs)
            if list(map(str, args))[3:4] == ["pull"]:
                self.host.slot = "a"
            return result
        with patch.object(recovery.subprocess, "run", side_effect=changed), self.assertRaises(recovery.RecoveryError):
            self.flow.execute()
        self.assertFalse(self.writes())
        self.assertTrue((self.flow.folder / self.flow.state["backup"]).exists())

    def test_missing_recovery_after_ram_boot_times_out_without_flash(self):
        original = self.host.run
        def disappears(args, **kwargs):
            result = original(args, **kwargs)
            if list(map(str, args))[3:4] == ["boot"]:
                self.host.mode = "offline"
            return result
        with patch.object(recovery.subprocess, "run", side_effect=disappears), self.assertRaises(recovery.RecoveryError):
            self.flow.execute()
        self.assertGreaterEqual(self.host.now, 120)
        self.assertFalse(self.writes())

    def test_wrapper_records_failed_flash_and_retains_its_ambiguity(self):
        self.host.flash_failure = True
        with patch.object(recovery, "RecoveryInstaller", return_value=self.flow):
            self.assertEqual(recovery.install(self.root), 1)
        state = json.loads((self.flow.folder / "state.json").read_text())
        self.assertEqual(state["phase"], "stopped")
        self.assertEqual(state["stopped_phase"], "flash_started")
        self.assertTrue(state["flash_may_have_changed"])
        self.assertEqual(len(self.writes()), 1)
        self.assertTrue((self.flow.folder / state["backup"]).exists())


class EntryMenuTests(unittest.TestCase):
    def test_default_menu_exit_never_discovers_devices(self):
        with patch.object(sys, "argv", ["balmunlock"]), patch("builtins.input", return_value=""), \
                patch("subprocess.run", side_effect=AssertionError("No device calls")), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(unlock.main(), 0)
        self.assertIn("1.", output.getvalue())
        self.assertIn("2.", output.getvalue())
        self.assertIn("3.", output.getvalue())

    def test_menu_recovery_dispatch_has_one_selection_and_no_unlock_workflow(self):
        with patch.object(sys, "argv", ["balmunlock"]), patch("builtins.input", return_value="2"), \
                patch.object(recovery, "install", return_value=0) as install, \
                patch.object(unlock, "Workflow", side_effect=AssertionError("No unlock workflow")), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(unlock.main(), 0)
        install.assert_called_once()

    def test_explicit_recovery_bypasses_menu(self):
        with patch.object(sys, "argv", ["balmunlock", "--install-recovery"]), \
                patch("builtins.input", side_effect=AssertionError("No menu")), \
                patch.object(recovery, "install", return_value=0) as install:
            self.assertEqual(unlock.main(), 0)
        install.assert_called_once()

    def test_menu_combined_dispatches_without_direct_unlock_or_recovery(self):
        with patch.object(sys, "argv", ["balmunlock"]), patch("builtins.input", return_value="3"), \
                patch.object(combined_workflow, "combined", return_value=0) as combo, \
                patch.object(recovery, "install", side_effect=AssertionError("No standalone installation")), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(unlock.main(), 0)
        self.assertTrue(combo.call_args.args[1].unlock_and_recovery)

    def test_explicit_combined_allows_a_pinned_image_path(self):
        with patch.object(sys, "argv", ["balmunlock", "--unlock-and-recovery", "--recovery-image", "x.img"]), \
                patch("builtins.input", side_effect=AssertionError("No menu")), \
                patch.object(combined_workflow, "combined", return_value=0) as combo:
            self.assertEqual(unlock.main(), 0)
        self.assertEqual(combo.call_args.args[1].recovery_image, Path("x.img"))

    def test_offline_image_check_never_discovers_devices(self):
        with patch.object(sys, "argv", ["balmunlock", "--check-recovery-image"]), \
                patch.object(artifact, "verify_image", return_value=Path("approved.img")), \
                patch("subprocess.run", side_effect=AssertionError("No device calls")), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(unlock.main(), 0)

    def test_recovery_image_cannot_be_attached_to_an_unlock_mode(self):
        with patch.object(sys, "argv", ["balmunlock", "--unlock", "--recovery-image", "x.img"]), \
                contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
            unlock.main()
        self.assertEqual(error.exception.code, 2)


class CombinedTests(unittest.TestCase):
    setUp = RecoveryTests.setUp
    sleep = RecoveryTests.sleep
    verify = RecoveryTests.verify
    writes = RecoveryTests.writes

    def combo(self, *, result=0, phase="reboot_submitted", marker=False, fresh=False,
              observed=False, pending=False, route_serial="SERIAL", completed_serial="SERIAL"):
        class UnlockStage:
            def __init__(stage, lang, **kwargs):
                stage.signals = recovery.OperationSignals(lang)
                stage.env = {}
                stage.root = self.root
                stage.serial = None
                stage.state = {}
                stage.marker_possible = False
                stage.already_unlocked_observed = False
                stage.unlocked_this_run = False
                stage.guided_called = False
            def say(stage, *args):
                pass
            def route_session(stage, args):
                self.assertEqual(stage.initial_serial, "SERIAL")
                self.assertEqual(stage.env["ANDROID_SERIAL"], "SERIAL")
                stage.serial = route_serial
                return True
            def guided(stage, args):
                stage.guided_called = True
                stage.serial = completed_serial
                stage.state = {"phase": phase, "reboot_pending": pending}
                stage.marker_possible = marker
                stage.already_unlocked_observed = observed
                stage.unlocked_this_run = fresh
                return result
            def record_error(stage, error):
                pass
            def recovery_notice(stage):
                pass
        args = types.SimpleNamespace(lang="en", non_interactive=True, verbose=False,
                                     recovery_image=self.image, session=None,
                                     recover=False, prepare=False, unlock_prepared=False)
        self.unlock_stage = None
        def factory(*args, **kwargs):
            self.unlock_stage = UnlockStage(*args, **kwargs)
            return self.unlock_stage
        # The alias uses the real verifier in production, and the fixture's small pinned artifact here.
        with patch.object(combined_workflow, "verify_image", side_effect=self.verify), \
                patch.object(combined_workflow, "RecoveryInstaller", return_value=self.flow):
            return combined_workflow.combined(self.root, args, factory)

    def test_successful_completed_unlock_continues_on_same_phone(self):
        self.assertEqual(self.combo(), 0)
        self.assertEqual(len(self.writes()), 1)
        self.assertEqual(self.flow.state["phase"], "completed")

    def test_real_unlock_workflow_on_unlocked_phone_continues_without_unlock_or_wipe(self):
        args = types.SimpleNamespace(lang="en", non_interactive=True, verbose=False,
                                     recovery_image=self.image, session=None,
                                     recover=False, prepare=False, unlock_prepared=False)
        with patch.object(combined_workflow, "verify_image", side_effect=self.verify), \
                patch.object(combined_workflow, "RecoveryInstaller", return_value=self.flow), \
                patch.object(unlock, "ROOT", self.root), patch.object(unlock, "preflight", return_value=True):
            self.assertEqual(combined_workflow.combined(self.root, args, unlock.Workflow), 0)
        self.assertEqual(len(self.writes()), 1)
        self.assertFalse(any("flashing" in args or "chkcode" in args for args, _ in self.host.calls))

    def test_fresh_unlock_waits_for_setup_before_installing(self):
        with patch("builtins.input", return_value="yes") as prompt:
            self.assertEqual(self.combo(fresh=True), 0)
        self.assertEqual(prompt.call_count, 2)  # Setup readiness, then recovery installation consent.
        self.assertEqual(len(self.writes()), 1)

    def test_declining_setup_readiness_stops_without_installation(self):
        with patch("builtins.input", return_value=""):
            self.assertEqual(self.combo(fresh=True), 0)
        self.assertFalse(self.writes())
        self.assertFalse(any(args[3:4] == ["boot"] for args, _ in self.host.calls))

    def test_failed_unlock_never_starts_twrp(self):
        self.assertEqual(self.combo(result=1), 1)
        self.assertFalse(self.writes())
        self.assertIsNone(self.flow.folder)

    def test_declined_preparation_never_starts_twrp(self):
        self.assertEqual(self.combo(phase="prepared"), 0)
        self.assertFalse(self.writes())
        self.assertIsNone(self.flow.folder)

    def test_pending_marker_or_reboot_blocks_handoff(self):
        self.assertEqual(self.combo(marker=True), 0)
        self.assertEqual(self.combo(pending=True), 0)
        self.assertFalse(self.writes())

    def test_already_unlocked_observation_does_not_require_a_new_unlock_receipt(self):
        self.assertEqual(self.combo(phase="new", observed=True), 0)
        self.assertEqual(len(self.writes()), 1)

    def test_different_session_is_rejected_before_unlock_stage(self):
        self.assertEqual(self.combo(route_serial="OTHER"), 1)
        self.assertFalse(self.unlock_stage.guided_called)
        self.assertFalse(self.writes())

    def test_different_serial_after_unlock_blocks_handoff(self):
        self.assertEqual(self.combo(completed_serial="OTHER"), 0)
        self.assertFalse(self.writes())

    def test_missing_image_is_rejected_before_unlock_or_device_discovery(self):
        self.image.unlink()
        self.assertEqual(self.combo(), 1)
        self.assertIsNone(self.unlock_stage)
        self.assertFalse(self.host.calls)

    def test_unsupported_phone_is_rejected_before_unlock(self):
        self.host.props["ro.product.model"] = "X01A"
        self.assertEqual(self.combo(), 1)
        self.assertIsNone(self.unlock_stage)
        self.assertFalse(self.writes())
