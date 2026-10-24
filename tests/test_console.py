"""Console UX checks: useful actions remain visible; raw helper output is logged."""
import contextlib
import io
import signal
import subprocess
import types
from unittest.mock import patch

from test_safety import WorkflowFixture, unlock
from operation_signals import OperationSignals, CancelRequested
from session_store import SessionStore


class ConsoleTests(WorkflowFixture):
    isolate_session_routing = False

    def setUp(self):
        super().setUp()
        self.flow.interactive = True
        self.args = types.SimpleNamespace(session=None, recover=False, prepare=False, unlock_prepared=False)
        self.enter(patch.object(unlock, "preflight", return_value=True))

    def noisy_probe(self):
        original = self.host.run
        def run(args, **kwargs):
            result = original(args, **kwargs)
            if str(args[1:2]).find("diag_probe.py") >= 0:
                result.stdout = "internal output; fastboot flashing unlock\n"
                result.stderr = "technical detail\n"
            return result
        return patch.object(unlock.subprocess, "run", run)

    def test_default_helper_output_is_retained_in_log_not_presented_as_user_commands(self):
        output, errors = io.StringIO(), io.StringIO()
        with self.noisy_probe(), contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            self.flow.tool("diag_probe.py")
        self.assertNotIn("flashing unlock", output.getvalue())
        self.assertNotIn("technical detail", errors.getvalue())
        log = (self.root / "balmunlock.log").read_text(encoding="utf-8")
        self.assertIn("internal output", log)
        self.assertIn("technical detail", log)

    def test_verbose_mode_can_show_helper_details(self):
        self.flow.verbose = True
        output = io.StringIO()
        with self.noisy_probe(), contextlib.redirect_stdout(output):
            self.flow.tool("diag_probe.py")
        self.assertIn("internal output", output.getvalue())

    def test_successful_dependency_check_is_compact_by_default(self):
        def details(lang):
            print("Python: diagnostic version")
            return True
        output = io.StringIO()
        with patch.object(unlock, "preflight", side_effect=details), contextlib.redirect_stdout(output):
            self.flow.check_host()
        self.assertIn("OK", output.getvalue())
        self.assertNotIn("Python:", output.getvalue())
        self.assertIn("Python:", (self.root / "balmunlock.log").read_text(encoding="utf-8"))

    def test_banner_and_selected_directory_are_shown_once(self):
        output = io.StringIO()
        session = SessionStore(self.root).create("SERIAL")
        with contextlib.redirect_stdout(output), patch("builtins.input", return_value="no"):
            self.flow.introduce()
            self.flow.bind_session(session)
            self.assertEqual(self.flow.guided(self.args), 0)
        self.assertEqual(output.getvalue().count("balmunlock " + unlock.VERSION), 1)
        self.assertEqual(output.getvalue().count(str(session.root)), 1)

    def test_declining_authorization_retry_stops_instead_of_asking_to_retry_again(self):
        self.host.adb_rows = "SERIAL unauthorized\n"
        with patch("builtins.input", side_effect=["yes", "no"]) as question:
            self.assertEqual(self.flow.guided(self.args), 0)
        self.assertEqual(question.call_count, 2)
        self.assertFalse(self.flow.backup.exists())
        self.assertFalse(self.flashed())

    def test_session_search_can_be_refreshed_without_restarting(self):
        SessionStore(self.root).create("FIRST")
        with patch.object(self.flow, "connected_devices", side_effect=[{}, {"SERIAL": {"adb"}}]), \
                patch("builtins.input", return_value="r"):
            self.assertTrue(self.flow.route_session(self.args))
        self.assertEqual(self.flow.session_serial, "SERIAL")

    def test_empty_keyboard_interrupt_has_a_readable_error(self):
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors):
            self.flow.show_error(KeyboardInterrupt())
        self.assertIn("Операция прервана", errors.getvalue())

    def test_help_is_available_in_both_languages_without_device_calls(self):
        for lang, expected in (("ru", "показать сохранённые сеансы"), ("en", "list saved sessions")):
            output = io.StringIO()
            with patch.object(unlock.sys, "argv", ["balmunlock", "--lang", lang, "--help"]), \
                    contextlib.redirect_stdout(output), self.assertRaises(SystemExit) as result:
                unlock.main()
            self.assertEqual(result.exception.code, 0)
            self.assertIn(expected, output.getvalue())
            self.assertIn("--verbose", output.getvalue())
        self.assertFalse(self.host.calls)

    def test_russian_cancellation_explains_waiting_for_active_write(self):
        output = io.StringIO()
        operation = OperationSignals("ru")
        with contextlib.redirect_stderr(output), operation.defer():
            operation.handle(signal.SIGINT, None)
        self.assertIn("Не отключайте USB", output.getvalue())
        with self.assertRaises(CancelRequested):
            operation.checkpoint()


class BootloaderConsoleTests(WorkflowFixture):
    isolate_bootloader_check = False

    def test_fastboot_driver_help_matches_the_host_platform(self):
        self.flow.serial = "SERIAL"
        self.host.properties["sys.boot_completed"] = "1"
        identity = {key: value for key, value in self.host.properties.items() if key.startswith("ro.")}
        for platform in ("nt", "posix"):
            output = io.StringIO()
            with patch.object(unlock.os, "name", platform), contextlib.redirect_stdout(output):
                self.flow.check_bootloader(identity)
            if platform == "nt":
                self.assertIn("Zadig", output.getvalue())
            else:
                self.assertIn("udev", output.getvalue())
                self.assertNotIn("Zadig", output.getvalue())
