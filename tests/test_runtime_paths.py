"""Offline checks of frozen helper dispatch and durable state paths."""
import importlib
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import launcher
import runtime_paths


class RuntimePathTests(unittest.TestCase):
    def test_source_helper_command_keeps_explicit_session_root(self):
        root = Path("session")
        with patch.object(runtime_paths.sys, "frozen", False, create=True):
            self.assertEqual(runtime_paths.tool_command("backup_chkcode.py", "-o", "x", root=root),
                             [sys.executable, root / "tools" / "backup_chkcode.py", "-o", "x"])

    def test_frozen_helper_restarts_exe_without_treating_it_as_python(self):
        with patch.object(runtime_paths.sys, "frozen", True, create=True):
            self.assertEqual(runtime_paths.tool_command("backup_chkcode.py", "-o", "x"),
                             [sys.executable, "--internal-tool", "backup_chkcode.py", "-o", "x"])

    def test_frozen_receipts_use_executable_directory_not_bundle_or_cwd(self):
        exe = Path.cwd() / "portable" / "balmunlock.exe"
        with patch.object(runtime_paths.sys, "frozen", True, create=True), \
                patch.object(runtime_paths.sys, "executable", str(exe)):
            self.assertEqual(runtime_paths.session_root(), exe.parent)
            source = Path(runtime_paths.__file__).with_name("cve_cleanup.py")
            spec = importlib.util.spec_from_file_location("frozen_cleanup_test", source)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            self.assertEqual(module.DEFAULT_STATE, exe.parent / module.STATE_NAME)

    def test_unknown_helper_cannot_fall_through_to_unlock(self):
        with self.assertRaises(ValueError):
            runtime_paths.tool_command("untrusted.py")
        with patch.object(launcher, "configure_runtime"), \
                patch.object(sys, "argv", ["balmunlock", "--internal-tool", "untrusted.py"]), \
                patch.object(launcher.runpy, "run_module") as run:
            self.assertEqual(launcher.main(), 2)
            run.assert_not_called()

    def test_frozen_helper_dispatch_preserves_arguments_and_does_not_run_controller(self):
        with patch.object(launcher, "configure_runtime"), \
                patch.object(sys, "argv", ["balmunlock", "--internal-tool", "backup_chkcode.py", "-o", "path with spaces"]), \
                patch.object(launcher.runpy, "run_module") as run:
            self.assertEqual(launcher.main(), 0)
            self.assertEqual(sys.argv, ["backup_chkcode.py", "-o", "path with spaces"])
            run.assert_called_once_with("backup_chkcode", run_name="__main__")
