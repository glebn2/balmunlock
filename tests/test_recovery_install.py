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
        data = bytearray(32768)
        data[:8] = b"ANDROID!"
        struct.pack_into("<I", data, 8, 256)
        struct.pack_into("<I", data, 16, 256)
        struct.pack_into("<I", data, 36, 4096)
        struct.pack_into("<I", data, 40, 2)
        data[-64:-60] = b"AVBf"
        struct.pack_into(">QQQ", data, len(data) - 52, 16384, 16384, 256)
        data[16384:16388] = b"AVB0"
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

    def test_other_image_passes_structure_but_release_packaging_requires_bundled_hash(self):
        data = bytearray(self.data)
        data[4096] = 1
        self.image.write_bytes(data)
        self.assertEqual(artifact.inspect_image(self.image), self.image.resolve())
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            artifact.verify_image(self.image)

    def test_out_of_bounds_payload_and_vbmeta_are_rejected_without_a_hash_reference(self):
        for offset, fmt, value in ((8, "<I", 0xFFFFFFFF),
                                   (len(self.data) - 44, ">Q", len(self.data)),
                                   (16384, "<I", 0)):
            with self.subTest(offset=offset):
                data = bytearray(self.data)
                struct.pack_into(fmt, data, offset, value)
                self.image.write_bytes(data)
                with self.assertRaises(ValueError):
                    artifact.inspect_image(self.image)

    def test_folder_accepts_a_single_image_with_any_name(self):
        root = Path(self.tmp.name)
        (root / "recovery").mkdir()
        replacement = root / "recovery" / "new-build.IMG"
        replacement.write_bytes(self.data)
        self.assertEqual(artifact.image_path(root), replacement)

    def test_multiple_images_require_an_explicit_path(self):
        root = Path(self.tmp.name)
        (root / "recovery").mkdir()
        for name in ("one.img", "two.img"):
            (root / "recovery" / name).write_bytes(self.data)
        with self.assertRaisesRegex(ValueError, "Multiple"):
            artifact.image_path(root)
        selected = root / "recovery" / "two.img"
        self.assertEqual(artifact.image_path(root, selected), selected)

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
        self.usb_transition_after_boot = 0
        self.usb_transition_polls = 0
        self.android_reboot_mode = "device"
        self.props = {"ro.boot.serialno": "SERIAL", "ro.product.model": "A101BM",
                      "ro.product.vendor.model": "A101BM", "ro.product.vendor.device": "A101BM",
                      "ro.board.platform": "lito", "ro.boot.slot_suffix": "_b",
                      "ro.build.version.sdk": "31", "ro.build.version.incremental": artifact.STOCK_BUILD,
                      "ro.build.version.release": "12",
                      "ro.build.fingerprint": artifact.STOCK_FINGERPRINT,
                      "ro.vendor.build.fingerprint": artifact.VENDOR_FINGERPRINT, "sys.boot_completed": "1",
                      "ro.build.version.security_patch": "2023-09-01"}
        self.recovery_props = {"ro.boot.serialno": "SERIAL", "ro.product.vendor.model": "Phone",
                               "ro.boot.slot_suffix": "_b", "ro.twrp.version": artifact.TWRP_VERSION,
                               "ro.vendor.build.fingerprint": artifact.TWRP_FINGERPRINT}

    def use_x01a(self):
        # Recorded stock X01A getprop; keep independent of the production profile.
        self.props.update({"ro.product.model": "X01A", "ro.product.vendor.model": "X01A",
                           "ro.product.vendor.device": "X01A",
                           "ro.build.version.incremental": "1.280MI.0648.a",
                           "ro.build.fingerprint": "BALMUDA/X01A/X01A:12/SKQ1.220303.001/1.280MI.0648.a:user/release-keys",
                           "ro.vendor.build.fingerprint": "BALMUDA/X01A/X01A:11/RKQ1.220301.001/1.280MI.0648.a:user/release-keys"})

    def use_lineage(self, model="A101BM", vendor="A101BM"):
        # Values from the 20261025 Lineage target-files: SDK 37 in system and vendor.
        self.props.update({"ro.product.model": model, "ro.product.vendor.model": vendor,
                           "ro.product.vendor.device": vendor, "ro.board.platform": "lito",
                           "ro.build.version.sdk": "37", "ro.build.version.release": "17",
                           "ro.build.version.incremental": "1792927504",
                           "ro.build.fingerprint": "BALMUDA/A101BM/A101BM:12/SKQ1.220303.001/1.280PO.0686.a:user/release-keys",
                           "ro.vendor.build.fingerprint": "BALMUDA/A101BM/A101BM:12/SKQ1.220303.001/1.280PO.0686.a:user/release-keys",
                           "ro.vendor.build.version.sdk": "37",
                           "ro.vendor.build.version.incremental": "1792927504"})

    def run(self, args, **kwargs):
        args = list(map(str, args))
        self.calls.append((args, kwargs))
        out, err, rc = "", "", 0
        if args == ["adb", "devices"]:
            out = "List of devices attached\n"
            if self.mode != "fastboot" and not self.usb_transition_polls:
                out += f"{'OTHER' if self.other_serial else 'SERIAL'}\t{self.mode}\n"
            if self.extra:
                out += "SECOND\tunauthorized\n"
        elif args == ["fastboot", "devices"]:
            if self.usb_transition_polls:
                self.usb_transition_polls -= 1
                out = "????????????\t fastboot\n"
            elif self.mode == "fastboot":
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
                        self.usb_transition_polls = self.usb_transition_after_boot
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
                self.mode = self.android_reboot_mode
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
        patch.object(recovery, "inspect_image", side_effect=self.verify).start()
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

    def test_full_install_backs_up_original_and_finishes_in_recovery_without_unlock_helpers(self):
        original = self.host.partition
        self.assertEqual(self.flow.execute(), 0)
        state = json.loads((self.flow.folder / "state.json").read_text())
        self.assertEqual(state["phase"], "completed")
        self.assertEqual((self.flow.folder / state["backup"]).read_bytes(), original)
        self.assertEqual(state["original_sha256"], hashlib.sha256(original).hexdigest())
        self.assertEqual(self.host.mode, "recovery")
        self.assertEqual(state["final_mode"], "recovery")
        self.assertTrue(state["recovery_ready"])
        self.assertNotIn("android_boot_completed", state)
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

    def test_other_image_warns_once_and_verifies_its_actual_hash(self):
        self.host.partition = self.data  # Replace an already installed bundled build.
        self.data = b"different-build!" * 256
        self.image.write_bytes(self.data)
        digest = hashlib.sha256(self.data).hexdigest()
        self.assertNotEqual(digest, recovery.IMAGE_SHA256)
        with patch("builtins.input", return_value="yes") as prompt, \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(self.flow.execute(), 0)
        self.assertEqual(prompt.call_count, 1)
        self.assertEqual(output.getvalue().count("Внимание:"), 1)
        self.assertEqual(self.flow.state["image_sha256"], digest)
        self.assertEqual(self.flow.state["installed_sha256"], digest)
        self.assertFalse(self.flow.state["bundled_image"])
        self.assertEqual(self.host.partition, self.data)

    def test_other_image_decline_never_reboots(self):
        self.data = b"different-build!" * 256
        self.image.write_bytes(self.data)
        with patch("builtins.input", return_value="no"):
            self.assertEqual(self.flow.execute(), 0)
        self.assertFalse(self.reboots())
        self.assertFalse(self.writes())

    def test_same_other_image_already_installed_skips_flash(self):
        self.data = b"different-build!" * 256
        self.image.write_bytes(self.data)
        self.host.partition = self.data
        self.assertEqual(self.flow.execute(), 0)
        self.assertFalse(self.writes())
        self.assertTrue(self.flow.state["original_matches_selected_image"])
        self.assertFalse(self.flow.state["original_was_pinned_twrp"])

    def test_changed_confirmed_image_stops_before_first_reboot(self):
        def confirm():
            self.image.write_bytes(b"x" * len(self.data))
            return "yes"
        with patch("builtins.input", side_effect=confirm), \
                patch.object(recovery, "inspect_image", side_effect=Path), \
                self.assertRaisesRegex(recovery.RecoveryError, "image changed"):
            self.flow.execute()
        self.assertFalse(self.reboots())
        self.assertFalse(self.writes())

    def test_changed_per_run_image_stops_before_flash_and_retains_backup(self):
        original = self.host.run
        def changed(args, **kwargs):
            result = original(args, **kwargs)
            if list(map(str, args))[3:4] == ["pull"]:
                (self.flow.folder / "recovery.img").write_bytes(b"x" * len(self.data))
            return result
        with patch.object(recovery.subprocess, "run", side_effect=changed), \
                patch.object(recovery, "inspect_image", side_effect=Path), \
                self.assertRaisesRegex(recovery.RecoveryError, "image changed"):
            self.flow.execute()
        self.assertFalse(self.writes())
        self.assertTrue((self.flow.folder / self.flow.state["backup"]).exists())

    def test_missing_artifact_refuses_before_device_access(self):
        self.image.unlink()
        with self.assertRaises(recovery.RecoveryError):
            self.flow.execute()
        self.assertFalse(self.host.calls)

    def test_other_device_platform_serial_and_slot_refuse_before_reboot(self):
        for key, value in (("ro.product.vendor.device", "OTHER"), ("ro.boot.slot_suffix", "_a"),
                           ("ro.board.platform", "other"), ("ro.boot.serialno", "OTHER"),
                           ("sys.boot_completed", "0")):
            before = self.host.props[key]
            self.host.props[key] = value
            with self.assertRaises(recovery.RecoveryError):
                self.flow.execute()
            self.host.props[key] = before
        self.assertFalse(self.reboots())
        self.assertFalse(self.writes())

    def test_x01a_installs_same_recovery_and_records_its_own_stock_build(self):
        self.host.use_x01a()
        original = self.host.partition
        self.assertEqual(self.flow.execute(), 0)
        state = json.loads((self.flow.folder / "state.json").read_text())
        self.assertEqual(state["phase"], "completed")
        self.assertEqual(state["stock_model"], "X01A")
        self.assertEqual(state["stock_build"], "1.280MI.0648.a")
        self.assertEqual((self.flow.folder / state["backup"]).read_bytes(), original)
        self.assertEqual(self.host.partition, self.data)
        self.assertEqual(self.writes()[0][0][3:5], ["flash", "recovery_b"])
        self.assertFalse(any("flashing" in args or "su" in args for args, _ in self.host.calls))

    def test_lineage_sdk37_installs_and_finishes_in_recovery(self):
        self.host.use_lineage()
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(self.flow.execute(), 0)
        self.assertIn("Android 17", output.getvalue())
        self.assertEqual(self.flow.state["phase"], "completed")
        self.assertEqual(self.flow.state["model"], "A101BM")
        self.assertEqual(self.flow.state["android_sdk"], "37")
        self.assertEqual(self.flow.state["android_build"], "1792927504")
        self.assertNotIn("stock_build", self.flow.state)
        self.assertEqual(len(self.writes()), 1)
        self.assertFalse(any("flashing" in args or "chkcode" in args for args, _ in self.host.calls))

    def test_lineage_accepts_a101bm_system_label_with_x01a_vendor(self):
        self.host.use_lineage(model="A101BM", vendor="X01A")
        self.assertEqual(self.flow.execute(), 0)
        self.assertEqual(self.flow.state["model"], "X01A")
        self.assertEqual(self.flow.state["system_model"], "A101BM")
        self.assertEqual(self.flow.state["vendor_model"], "X01A")
        self.assertEqual(self.flow.state["android_sdk"], "37")
        self.assertEqual(self.flow.state["phase"], "completed")

    def test_custom_rom_branding_is_not_used_as_a_hardware_model(self):
        self.host.use_lineage(model="BALMUDA Phone", vendor="A101BM")
        self.assertEqual(self.flow.execute(), 0)
        self.assertEqual(self.flow.state["model"], "A101BM")
        self.assertEqual(self.flow.state["system_model"], "BALMUDA Phone")

    def test_lineage_update_of_already_installed_image_skips_flash(self):
        self.host.use_lineage()
        self.host.partition = self.data
        self.assertEqual(self.flow.execute(), 0)
        self.assertFalse(self.writes())
        self.assertEqual(self.flow.state["phase"], "completed")

    def test_english_lineage_matching_image_finishes_with_english_instructions(self):
        self.host.use_lineage()
        self.host.partition = self.data
        self.flow.lang = "en"
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(self.flow.execute(), 0)
        text = output.getvalue()
        self.assertIn("Android 17", text)
        self.assertIn("This TWRP is already installed; no repeat flash.", text)
        self.assertIn("Phone remains in TWRP", text)
        self.assertIn("Choose Reboot -> System", text)
        self.assertNotRegex(text, r"[\u0400-\u04ff]")
        self.assertFalse(self.writes())
        self.assertEqual(self.flow.state["phase"], "completed")

    def test_english_lineage_other_image_warning_and_completion_are_localized(self):
        self.host.use_lineage()
        self.host.partition = self.data
        self.data = b"different-build!" * 256
        self.image.write_bytes(self.data)
        self.flow.lang = "en"
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(self.flow.execute(), 0)
        text = output.getvalue()
        self.assertIn("Warning: this image differs from the bundled TWRP.", text)
        self.assertIn("Original backed up. Writing recovery_b", text)
        self.assertIn("Phone remains in TWRP", text)
        self.assertIn("Choose Reboot -> System", text)
        self.assertNotRegex(text, r"[\u0400-\u04ff]")
        self.assertEqual(len(self.writes()), 1)
        self.assertEqual(self.flow.state["phase"], "completed")
        self.assertEqual(self.flow.state["installed_sha256"], hashlib.sha256(self.data).hexdigest())

    def test_model_remains_bound_across_identity_checks(self):
        self.flow.select_android()
        self.host.use_x01a()
        with self.assertRaisesRegex(recovery.RecoveryError, "model changed"):
            self.flow.android_identity()
        self.assertFalse(self.writes())

    def test_matching_image_finishes_without_attempting_android_reboot(self):
        self.host.partition = self.data
        self.host.android_reboot_mode = "recovery"  # Recorded hardware behavior.
        self.assertEqual(self.flow.execute(), 0)
        self.assertEqual(self.host.mode, "recovery")
        self.assertEqual(self.flow.state["phase"], "completed")
        self.assertEqual(self.flow.state["final_mode"], "recovery")
        self.assertFalse(self.writes())
        self.assertFalse(any(args[0] == "adb" and args[3:] == ["reboot"] for args, _ in self.host.calls))

    def test_different_image_finishes_in_recovery_after_verified_flash(self):
        self.host.android_reboot_mode = "recovery"  # Would make the old Android wait time out.
        self.assertEqual(self.flow.execute(), 0)
        self.assertEqual(self.host.mode, "recovery")
        self.assertEqual(self.flow.state["phase"], "completed")
        self.assertEqual(self.flow.state["final_mode"], "recovery")
        self.assertEqual(self.flow.state["installed_sha256"], hashlib.sha256(self.data).hexdigest())
        self.assertEqual(len(self.writes()), 1)
        self.assertFalse(any(args[0] == "adb" and args[3:] == ["reboot"] for args, _ in self.host.calls))

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

    def test_missing_twrp_version_stops_before_flash(self):
        self.host.recovery_props.pop("ro.twrp.version")
        with self.assertRaises(recovery.RecoveryError):
            self.flow.execute()
        self.assertFalse(self.writes())

    def test_other_twrp_version_model_and_fingerprint_are_recorded_without_refusal(self):
        self.host.recovery_props.update({"ro.product.vendor.model": "BALMUDA Phone",
                                         "ro.twrp.version": "3.8.0_14-new-build",
                                         "ro.vendor.build.fingerprint": "new-build-fingerprint"})
        self.assertEqual(self.flow.execute(), 0)
        self.assertEqual(self.flow.state["twrp_version"], "3.8.0_14-new-build")
        self.assertEqual(self.flow.state["twrp_fingerprint"], "new-build-fingerprint")
        self.assertEqual(len(self.writes()), 1)

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
        self.host.mode = "device"  # The user returns to Android before the next run.
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

    def test_windows_unknown_fastboot_serial_during_ram_boot_waits_and_continues(self):
        self.host.use_lineage()
        self.host.usb_transition_after_boot = 2
        self.assertEqual(self.flow.execute(), 0)
        self.assertGreaterEqual(self.host.now, 4)
        self.assertEqual(self.flow.state["phase"], "completed")
        self.assertEqual(len(self.writes()), 1)
        self.assertEqual(self.writes()[0][0][1:3], ["-s", "SERIAL"])
        self.assertEqual(sum(args[3:4] == ["boot"] for args, _ in self.host.calls), 1)

    def test_unknown_fastboot_handle_does_not_mask_the_identified_recovery(self):
        original = self.host.run
        def enumeration(args, **kwargs):
            result = original(args, **kwargs)
            if list(map(str, args)) == ["fastboot", "devices"] and self.host.mode == "recovery":
                result.stdout = "????????????\t fastboot\n"
            return result
        with patch.object(recovery.subprocess, "run", side_effect=enumeration):
            self.assertEqual(self.flow.execute(), 0)
        self.assertEqual(self.flow.state["phase"], "completed")
        self.assertEqual(self.writes()[0][0][1:3], ["-s", "SERIAL"])

    def test_unknown_serial_alone_times_out_without_addressing_or_flashing_it(self):
        self.flow.serial = "SERIAL"
        self.host.mode = "recovery"
        self.host.usb_transition_polls = 1000
        with self.assertRaisesRegex(recovery.RecoveryError, "Timed out"):
            self.flow.wait("recovery", 4)
        self.assertGreaterEqual(self.host.now, 4)
        self.assertFalse(self.writes())
        self.assertTrue(all(args in (["adb", "devices"], ["fastboot", "devices"])
                            for args, _ in self.host.calls))

    def test_unidentified_fastboot_device_cannot_be_selected_at_start(self):
        self.host.usb_transition_polls = 1
        with self.assertRaisesRegex(recovery.RecoveryError, "serial yet"):
            self.flow.execute()
        self.assertFalse(self.reboots())
        self.assertFalse(self.writes())

    def test_other_identified_phone_still_refuses_with_a_placeholder_present(self):
        self.flow.serial = "SERIAL"
        self.host.mode = "recovery"
        self.host.extra = True
        original = self.host.run
        def enumeration(args, **kwargs):
            result = original(args, **kwargs)
            if list(map(str, args)) == ["fastboot", "devices"]:
                result.stdout = "????????????\t fastboot\n"
            return result
        with patch.object(recovery.subprocess, "run", side_effect=enumeration), \
                self.assertRaisesRegex(recovery.RecoveryError, "multiple devices"):
            self.flow.wait("recovery", 4)
        self.assertFalse(self.writes())

    def test_a_malformed_serial_is_not_treated_as_the_windows_placeholder(self):
        self.flow.serial = "SERIAL"
        self.host.mode = "recovery"
        original = self.host.run
        def malformed(args, **kwargs):
            result = original(args, **kwargs)
            if list(map(str, args)) == ["fastboot", "devices"]:
                result.stdout = "BROKEN?SERIAL\t fastboot\n"
            return result
        with patch.object(recovery.subprocess, "run", side_effect=malformed), \
                self.assertRaisesRegex(recovery.RecoveryError, "Invalid device-list"):
            self.flow.wait("recovery", 4)
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
                patch.object(artifact, "image_path", return_value=Path("approved.img")), \
                patch.object(artifact, "inspect_image", return_value=Path("approved.img")), \
                patch.object(artifact, "file_hash", return_value="f" * 64), \
                patch("subprocess.run", side_effect=AssertionError("No device calls")), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(unlock.main(), 0)
        self.assertIn("f" * 64, output.getvalue())

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
              observed=False, pending=False, route_serial="SERIAL", completed_serial="SERIAL", after_unlock=None):
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
                if after_unlock is not None:
                    after_unlock()
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
        with patch.object(combined_workflow, "inspect_image", side_effect=self.verify), \
                patch.object(combined_workflow, "RecoveryInstaller", return_value=self.flow):
            return combined_workflow.combined(self.root, args, factory)

    def test_successful_completed_unlock_continues_on_same_phone(self):
        self.assertEqual(self.combo(), 0)
        self.assertEqual(len(self.writes()), 1)
        self.assertEqual(self.flow.state["phase"], "completed")

    def test_x01a_combined_flow_continues_after_completed_or_fresh_unlock(self):
        self.host.use_x01a()
        self.assertEqual(self.combo(fresh=True), 0)
        self.assertEqual(len(self.writes()), 1)
        self.assertEqual(self.flow.state["stock_model"], "X01A")
        self.assertEqual(self.flow.state["stock_build"], "1.280MI.0648.a")

    def test_combined_flow_accepts_other_image_and_uses_its_hash(self):
        self.data = b"different-build!" * 256
        self.image.write_bytes(self.data)
        self.assertEqual(self.combo(), 0)
        self.assertTrue(self.unlock_stage.guided_called)
        self.assertEqual(self.flow.state["installed_sha256"], hashlib.sha256(self.data).hexdigest())
        self.assertFalse(self.flow.state["bundled_image"])

    def test_real_unlock_workflow_on_unlocked_phone_continues_without_unlock_or_wipe(self):
        args = types.SimpleNamespace(lang="en", non_interactive=True, verbose=False,
                                     recovery_image=self.image, session=None,
                                     recover=False, prepare=False, unlock_prepared=False)
        with patch.object(combined_workflow, "inspect_image", side_effect=self.verify), \
                patch.object(combined_workflow, "RecoveryInstaller", return_value=self.flow), \
                patch.object(unlock, "ROOT", self.root), patch.object(unlock, "preflight", return_value=True):
            self.assertEqual(combined_workflow.combined(self.root, args, unlock.Workflow), 0)
        self.assertEqual(len(self.writes()), 1)
        self.assertFalse(any("flashing" in args or "chkcode" in args for args, _ in self.host.calls))

    def test_real_unlock_workflow_accepts_already_unlocked_x01a_without_wipe(self):
        self.host.use_x01a()
        self.test_real_unlock_workflow_on_unlocked_phone_continues_without_unlock_or_wipe()
        self.assertEqual(self.flow.state["stock_build"], "1.280MI.0648.a")

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

    def test_image_changed_during_unlock_never_starts_recovery(self):
        def changed():
            self.data = b"different-build!" * 256
            self.image.write_bytes(self.data)
        self.assertEqual(self.combo(after_unlock=changed), 1)
        self.assertTrue(self.unlock_stage.guided_called)
        self.assertFalse(self.writes())
        self.assertIsNone(self.flow.folder)

    def test_unsupported_phone_is_rejected_before_unlock(self):
        self.host.props["ro.product.vendor.device"] = "OTHER"
        self.assertEqual(self.combo(), 1)
        self.assertIsNone(self.unlock_stage)
        self.assertFalse(self.writes())

    def test_combined_unlock_on_lineage_stops_before_unlock_stage(self):
        self.host.use_lineage()
        self.assertEqual(self.combo(), 1)
        self.assertIsNone(self.unlock_stage)
        self.assertFalse(self.writes())
