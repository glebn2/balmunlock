"""Device isolation and session routing. Every transport call is simulated."""
import hashlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
import types
import unittest
from unittest.mock import patch

from test_safety import WorkflowFixture, ORIGINAL, backup, unlock
from session_store import SessionStore, directory_key


class SessionStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix=".test-balmunlock-", dir=Path(__file__).resolve().parents[1])
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.store = SessionStore(self.root)

    def test_devices_with_identical_original_data_have_separate_directories(self):
        first, second = self.store.create("FIRST"), self.store.create("SECOND")
        self.assertNotEqual(first.root, second.root)
        for session in (first, second):
            backup.save_backup(session.root / "chkcode_backup.bin", ORIGINAL)
        self.assertEqual(len(self.store.entries()), 2)
        self.assertEqual(self.store.select("FIRST"), first)
        self.assertEqual(backup.load_backup(first.root / "chkcode_backup.bin"), ORIGINAL)

    def test_serial_keys_do_not_collide_on_windows_case_or_character_mapping(self):
        serials = ("ABC", "abc", "A:B", "A_B", "CON", "a.", "a", "A" * 128)
        keys = [directory_key(serial) for serial in serials]
        self.assertEqual(len(set(key.lower() for key in keys)), len(serials))
        self.assertTrue(all(len(key) < 120 and ":" not in key for key in keys))

    def test_invalid_serials_cannot_escape_directory_or_create_files(self):
        for serial in ("../outside", "a/b", "a\\b", "", "x\n", "A" * 129, None):
            with self.subTest(serial=serial), self.assertRaises(ValueError):
                self.store.create(serial)
        self.assertFalse(self.store.directory.exists())

    def test_create_does_not_overwrite_owner_or_original(self):
        session = self.store.create("FIRST")
        owner = session.root / "session.json"
        before = owner.read_bytes()
        backup.save_backup(session.root / "chkcode_backup.bin", ORIGINAL)
        self.store.create("FIRST")
        self.assertEqual(owner.read_bytes(), before)
        self.assertEqual(backup.load_backup(session.root / "chkcode_backup.bin"), ORIGINAL)

    def test_foreign_receipt_invalidates_only_its_session(self):
        session = self.store.create("FIRST")
        (session.root / "chkcode_backup.device.json").write_text(json.dumps({"version": 1, "serial": "OTHER", "phase": "prepared"}))
        second = self.store.create("SECOND")
        entries = self.store.entries()
        self.assertTrue(next(entry for entry in entries if entry.root == session.root).error)
        self.assertFalse(self.store.select(second.key).error)

    def test_legacy_discovery_does_not_move_or_rewrite_files(self):
        path = self.root / "chkcode_backup.device.json"
        data = json.dumps({"version": 1, "serial": "FIRST", "phase": "reboot_submitted"})
        path.write_text(data)
        entry = self.store.select("FIRST")
        self.assertEqual(entry.key, "legacy")
        self.assertEqual(entry.root, self.root)
        self.assertEqual(path.read_text(), data)
        self.assertFalse(self.store.directory.exists())

    def test_legacy_orphan_is_retained_without_claiming_another_phone(self):
        backup.save_backup(self.root / "chkcode_backup.bin", ORIGINAL)
        entry = self.store.select("legacy")
        self.assertIsNone(entry.serial)
        self.store.create("SECOND")
        self.assertEqual(backup.load_backup(self.root / "chkcode_backup.bin"), ORIGINAL)

    def test_ambiguous_serial_requires_exact_session_key(self):
        (self.root / "chkcode_backup.device.json").write_text(json.dumps({"version": 1, "serial": "FIRST"}))
        created = self.store.create("FIRST")
        with self.assertRaises(ValueError):
            self.store.select("FIRST")
        self.assertEqual(self.store.select(created.key), created)

    def test_wrong_directory_owner_is_never_replaced(self):
        session = self.store.create("FIRST")
        owner = session.root / "session.json"
        data = json.dumps({"version": 1, "serial": "SECOND"})
        owner.write_text(data)
        with self.assertRaises(ValueError):
            self.store.create("FIRST")
        self.assertEqual(owner.read_text(), data)

    def test_file_in_place_of_sessions_directory_is_retained(self):
        self.store.directory.write_text("keep me")
        with self.assertRaises(ValueError):
            self.store.entries()
        self.assertEqual(self.store.directory.read_text(), "keep me")

    def test_linked_sessions_directory_is_refused_without_writes(self):
        target = self.root / "elsewhere"
        target.mkdir()
        try:
            self.store.directory.symlink_to(target, target_is_directory=True)
        except OSError:
            self.skipTest("Creating symlinks is not permitted on this host")
        with self.assertRaises(ValueError):
            self.store.create("FIRST")
        self.assertEqual(list(target.iterdir()), [])

    def test_linked_owner_is_not_accepted_or_replaced(self):
        session = self.store.create("FIRST")
        owner = session.root / "session.json"
        target = self.root / "saved-owner.json"
        target.write_bytes(owner.read_bytes())
        owner.unlink()
        try:
            owner.symlink_to(target)
        except OSError:
            self.skipTest("Creating symlinks is not permitted on this host")
        before = target.read_bytes()
        with self.assertRaises(ValueError):
            self.store.create("FIRST")
        self.assertEqual(target.read_bytes(), before)


class SessionRoutingTests(WorkflowFixture):
    isolate_session_routing = False

    def setUp(self):
        super().setUp()
        self.flow.interactive = True
        self.store = SessionStore(self.root)
        self.args = types.SimpleNamespace(session=None, recover=False, prepare=False, unlock_prepared=False)
        self.enter(patch.object(unlock, "preflight", return_value=True))

    def connect(self, serial, mode="adb"):
        self.enter(patch.object(unlock.Workflow, "connected_devices", return_value={serial: {mode}}))

    def prepared_session(self, serial="SERIAL", phase="prepared", marker=False, reboot=False):
        session = self.store.create(serial)
        original = ORIGINAL
        for name in ("chkcode_backup.bin", "chkcode_backup.recovery.bin"):
            backup.save_backup(session.root / name, original)
        (session.root / "chkcode_backup.device.json").write_text(json.dumps({
            "version": 1, "serial": serial, "phase": phase, "marker_possible": marker,
            "marker_verified": marker, "reboot_pending": reboot,
            "sha256": hashlib.sha256(original).hexdigest(), **{k: v for k, v in self.host.properties.items() if k.startswith("ro.")}}))
        return session

    def test_new_phone_gets_new_directory_after_old_completed_unlock(self):
        first = self.prepared_session("FIRST", "reboot_submitted")
        old_files = {path.name: path.read_bytes() for path in first.root.iterdir()}
        self.connect("SERIAL")
        self.assertTrue(self.flow.route_session(self.args))
        self.assertNotEqual(self.flow.root, first.root)
        with patch("builtins.input", return_value="no"):
            self.assertEqual(self.flow.guided(self.args), 0)
        self.assertEqual({path.name: path.read_bytes() for path in first.root.iterdir()}, old_files)
        self.assertFalse(self.flashed())

    def test_new_phone_unlock_uses_only_its_new_backup_directory(self):
        first = self.prepared_session("FIRST", "reboot_submitted")
        self.connect("SERIAL")
        self.flow.route_session(self.args)
        new_root = self.flow.root
        with patch("builtins.input", return_value="yes"):
            self.assertEqual(self.flow.guided(self.args), 0)
        self.assertEqual(self.flashed()[0][-1], str(new_root / "chkcode_backup.bin"))
        self.assertEqual(json.loads((first.root / "chkcode_backup.device.json").read_text())["phase"], "reboot_submitted")
        self.assertEqual(json.loads(self.flow.receipt.read_text())["phase"], "reboot_submitted")

    def test_known_completed_phone_reuses_its_session_without_write_or_erase_prompt(self):
        session = self.prepared_session(phase="reboot_submitted")
        self.connect("SERIAL")
        self.flow.route_session(self.args)
        with patch("builtins.input", side_effect=AssertionError("No erase question")):
            self.assertEqual(self.flow.guided(self.args), 0)
        self.assertEqual(self.flow.root, session.root)
        self.assertFalse(self.host.calls)

    def test_known_pending_phone_in_fastboot_routes_to_original_recovery(self):
        session = self.prepared_session(phase="marker_verified", marker=True)
        self.connect("SERIAL", "fastboot")
        self.flow.route_session(self.args)
        with patch("builtins.input", return_value="yes"):
            self.assertEqual(self.flow.guided(self.args), 0)
        self.assertEqual(self.flashed()[0][-1], str(session.root / "chkcode_backup.bin"))
        self.assertFalse(any("unlock" in args for args, _ in self.host.calls))

    def test_other_pending_phone_does_not_block_a_new_phone_or_use_its_backup(self):
        old = self.prepared_session("FIRST", "marker_verified", marker=True)
        self.connect("SERIAL")
        self.flow.route_session(self.args)
        self.assertFalse(self.flow.receipt.exists())
        self.assertEqual(self.flow.session_serial, "SERIAL")
        self.assertTrue(json.loads((old.root / "chkcode_backup.device.json").read_text())["marker_possible"])

    def test_unknown_fastboot_phone_does_not_create_session_or_reboot(self):
        self.connect("UNKNOWN", "fastboot")
        with self.assertRaises(unlock.Stop):
            self.flow.route_session(self.args)
        self.assertFalse(self.store.directory.exists())
        self.assertFalse(self.host.calls)

    def test_explicit_session_is_selected_without_transport_discovery(self):
        session = self.prepared_session()
        self.args.session = "SERIAL"
        with patch.object(self.flow, "connected_devices", side_effect=AssertionError("No discovery")):
            self.flow.route_session(self.args)
        self.assertEqual(self.flow.root, session.root)

    def test_legacy_session_for_old_phone_does_not_block_new_phone(self):
        self.flow.interactive = False
        self.flow.execute(prepare_only=True)
        self.flow.save_state("reboot_submitted", reboot_pending=False)
        self.flow = unlock.Workflow(interactive=True)
        self.connect("SECOND")
        self.flow.route_session(self.args)
        self.assertEqual(self.flow.session_serial, "SECOND")
        self.assertNotEqual(self.flow.root, self.root)
        self.assertTrue((self.root / "chkcode_backup.device.json").exists())

    def test_legacy_matching_phone_continues_in_place(self):
        self.flow.execute(prepare_only=True)
        self.flow = unlock.Workflow(interactive=True)
        self.connect("SERIAL")
        self.flow.route_session(self.args)
        self.assertEqual(self.flow.root, self.root)
        self.assertFalse(self.store.directory.exists())

    def test_wrong_session_receipt_refuses_before_any_recovery_command(self):
        session = self.prepared_session()
        self.flow.bind_session(session)
        state = json.loads(self.flow.receipt.read_text())
        state.update(serial="OTHER", marker_possible=True, reboot_pending=True)
        self.flow.receipt.write_text(json.dumps(state))
        with patch("builtins.input", return_value="no"):
            self.assertEqual(self.flow.guided(self.args), 1)
        self.assertFalse(self.host.calls)
        self.assertFalse(self.flow.marker_possible)

    def test_cleanup_helper_and_children_use_selected_directory_but_original_source_paths(self):
        session = self.store.create("SERIAL")
        self.flow.bind_session(session)
        self.flow.tool("cve31317.py")
        args, kwargs = next((args, kwargs) for args, kwargs in self.host.calls if args[0] == unlock.sys.executable)
        self.assertEqual(args[1], str(self.root / "tools" / "cve31317.py"))
        self.assertEqual(args[-2:], ["--state", str(session.root / unlock.STATE_NAME)])
        self.assertEqual(kwargs["cwd"], session.root)

    def test_list_sessions_is_host_only(self):
        self.prepared_session()
        with patch.object(unlock.sys, "argv", ["unlock", "--list-sessions"]), patch("sys.stdout", new_callable=io.StringIO) as output:
            self.assertEqual(unlock.main(), 0)
        self.assertIn("SERIAL", output.getvalue())
        self.assertFalse(self.host.calls)

    def test_two_connected_devices_cannot_be_confirmed_into_an_arbitrary_session(self):
        with patch.object(self.flow, "connected_devices", return_value={"FIRST": {"adb"}, "SECOND": {"fastboot"}}), \
                patch("builtins.input", return_value="no"), self.assertRaises(unlock.Stop):
            self.flow.route_session(self.args)
        self.assertFalse(self.store.directory.exists())

    def test_explicit_recovery_for_unknown_phone_does_not_create_or_unlock(self):
        self.connect("SERIAL")
        self.args.recover = True
        self.assertFalse(self.flow.route_session(self.args))
        self.assertFalse(self.store.directory.exists())

    def test_discovery_only_lists_transports_and_merges_the_same_serial(self):
        found = self.flow.connected_devices()
        self.assertEqual(found, {"SERIAL": {"adb", "fastboot"}})
        self.assertEqual([args for args, _ in self.host.calls], [["adb", "devices"], ["fastboot", "devices"]])

    def test_unavailable_adb_does_not_block_fastboot_recovery_selection(self):
        self.prepared_session(phase="marker_verified", marker=True)
        original = self.host.run
        def no_adb(args, **kwargs):
            if args == ["adb", "devices"]:
                raise FileNotFoundError("ADB unavailable")
            return original(args, **kwargs)
        with patch.object(unlock.subprocess, "run", no_adb):
            self.assertTrue(self.flow.route_session(self.args))
        self.assertEqual(self.flow.session_serial, "SERIAL")

    def test_full_cli_new_device_is_not_blocked_by_another_completed_legacy_session(self):
        old = self.prepared_session("FIRST", "reboot_submitted")
        for path in old.root.iterdir():
            if path.name != "session.json":
                (self.root / path.name).write_bytes(path.read_bytes())
        before = (self.root / "chkcode_backup.device.json").read_bytes()
        with patch.object(unlock.sys, "argv", ["unlock", "--unlock", "--guided"]), patch("builtins.input", return_value="yes"):
            self.assertEqual(unlock.main(), 0)
        selected = self.store.select("SERIAL")
        self.assertEqual(json.loads((selected.root / "chkcode_backup.device.json").read_text())["phase"], "reboot_submitted")
        self.assertEqual((self.root / "chkcode_backup.device.json").read_bytes(), before)
