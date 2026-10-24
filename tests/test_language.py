"""Language selection and user guidance checks, without device access."""
import contextlib
import io
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import ui_language as ui
import launcher
import unlock
from recovery_image import LocalizedValueError
from recovery_install import RecoveryError


class LanguageTests(unittest.TestCase):
    def test_windows_uses_display_language_not_regional_or_shell_locale(self):
        for language_id, expected in ((0x0419, "ru"), (0x0819, "ru"), (0x0409, "en"),
                                      (0x0411, "en"), (0x0407, "en")):
            with self.subTest(language_id=language_id), patch.object(ui.sys, "platform", "win32"), \
                    patch.object(ui, "_windows_ui_language", return_value=language_id), \
                    patch.dict(os.environ, {"LANG": "ru_RU.UTF-8", "LC_ALL": "ru_RU.UTF-8"}, clear=True):
                self.assertEqual(ui.default_language(), expected)

    def test_linux_locale_precedence_and_english_fallback(self):
        cases = [({"LANG": "ru_RU.UTF-8"}, "ru"), ({"LANG": "ru-RU"}, "ru"),
                 ({"LANG": "Russian_Russia.1251"}, "ru"), ({"LANG": "rue_UA.UTF-8"}, "en"),
                 ({"LC_ALL": "C", "LANG": "ru_RU.UTF-8"}, "en"),
                 ({"LC_MESSAGES": "ru_RU.UTF-8", "LANG": "de_DE.UTF-8"}, "ru"),
                 ({"LC_MESSAGES": "fr_FR.UTF-8", "LANG": "ru_RU.UTF-8"}, "en"),
                 ({"LC_ALL": "", "LC_MESSAGES": "", "LANG": "ru_RU.UTF-8"}, "ru"),
                 ({"LANG": "C.UTF-8"}, "en"), ({"LANG": "unknown"}, "en")]
        for env, expected in cases:
            with self.subTest(env=env), patch.object(ui.sys, "platform", "linux"), patch.dict(os.environ, env, clear=True):
                self.assertEqual(ui.default_language(), expected)

    def test_missing_or_broken_locale_defaults_to_english(self):
        with patch.object(ui.sys, "platform", "linux"), patch.dict(os.environ, {}, clear=True):
            with patch.object(ui.locale, "getlocale", return_value=(None, None)):
                self.assertEqual(ui.default_language(), "en")
            with patch.object(ui.locale, "getlocale", side_effect=ValueError("broken")):
                self.assertEqual(ui.default_language(), "en")

    def test_windows_api_failure_uses_locale_fallback(self):
        with patch.object(ui.sys, "platform", "win32"), patch.object(ui, "_windows_ui_language", side_effect=OSError()), \
                patch.dict(os.environ, {"LANG": "ru_RU.UTF-8"}, clear=True):
            self.assertEqual(ui.default_language(), "ru")

    def test_explicit_overrides_and_last_override_wins(self):
        with patch.object(ui, "default_language", return_value="ru"):
            self.assertEqual(ui.language_from_args(["--lang", "en"]), "en")
            self.assertEqual(ui.language_from_args(["--lang=en", "--lang", "ru"]), "ru")
            self.assertEqual(ui.language_from_args(["en"]), "ru")
            self.assertEqual(ui.language_from_args(["--lang", "invalid"]), "ru")

    def test_auto_menu_and_help_are_selected_consistently_without_usb(self):
        for lang, menu, help_word in (("ru", "Только разблокировка", "язык сообщений"),
                                      ("en", "Unlock bootloader only", "message and help language")):
            with patch.object(unlock, "default_language", return_value=lang), \
                    patch("subprocess.run", side_effect=AssertionError("No device calls")):
                with patch.object(sys, "argv", ["balmunlock"]), patch("builtins.input", return_value=""), \
                        contextlib.redirect_stdout(io.StringIO()) as output:
                    self.assertEqual(unlock.main(), 0)
                self.assertIn(menu, output.getvalue())
                with patch.object(sys, "argv", ["balmunlock", "--help"]), \
                        contextlib.redirect_stdout(io.StringIO()) as output, self.assertRaises(SystemExit) as result:
                    unlock.main()
                self.assertEqual(result.exception.code, 0)
                self.assertIn(help_word, output.getvalue())
                self.assertIn("показать справку" if lang == "ru" else "show this help", output.getvalue())
                self.assertTrue(output.getvalue().startswith("использование:" if lang == "ru" else "usage:"))

    def test_explicit_cli_language_overrides_detected_default(self):
        with patch.object(unlock, "default_language", return_value="ru"), \
                patch.object(sys, "argv", ["balmunlock", "--lang", "en"]), \
                patch("builtins.input", return_value=""), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(unlock.main(), 0)
        self.assertIn("Unlock bootloader only", output.getvalue())
        self.assertNotIn("Только разблокировка", output.getvalue())

    def test_linux_restore_hint_uses_the_shell_entry(self):
        with patch.object(unlock, "frozen", return_value=False), patch.object(unlock.os, "name", "posix"):
            self.assertEqual(unlock.recovery_command(), "bash unlock.sh --recover")

    def test_startup_failure_has_an_actionable_translation(self):
        for lang, expected in (("ru", "Распакуйте весь"), ("en", "Extract the entire")):
            with patch.object(launcher, "configure_runtime", side_effect=RuntimeError("missing test file")), \
                    patch.object(sys, "argv", ["balmunlock", "--lang", lang]), \
                    contextlib.redirect_stderr(io.StringIO()) as output:
                self.assertEqual(launcher.main(), 2)
            self.assertIn(expected, output.getvalue())

    def test_recovery_errors_show_only_the_requested_language(self):
        for error in (LocalizedValueError("Invalid image", "Неверный образ"),
                      RecoveryError("Invalid image", "Неверный образ")):
            self.assertEqual(ui.error_message(error, "ru"), "Неверный образ")
            self.assertEqual(ui.error_message(error, "en"), "Invalid image")

    def test_technical_error_is_retained_in_log_but_russian_hint_is_localized(self):
        with tempfile.TemporaryDirectory(prefix=".test-balmunlock-", dir=ROOT) as tmp:
            flow = unlock.Workflow("ru")
            flow.root = Path(tmp)
            with contextlib.redirect_stderr(io.StringIO()) as output:
                flow.show_error(unlock.Stop("technical failure detail"))
            self.assertIn("Подробности", output.getvalue())
            self.assertNotIn("technical failure detail", output.getvalue())
            self.assertIn("technical failure detail", (flow.root / "balmunlock.log").read_text(encoding="utf-8"))


class ShellEntryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bash = shutil.which("bash") if os.name != "nt" else None
        if cls.bash is None:
            candidate = Path(r"C:\Program Files\Git\bin\bash.exe")
            if candidate.is_file():
                cls.bash = str(candidate)
        if cls.bash is None:
            raise unittest.SkipTest("Bash is not installed")

    def test_shell_syntax(self):
        for name in ("unlock.sh", "unlock.en.sh"):
            result = subprocess.run([self.bash, "-n", name], cwd=ROOT, capture_output=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_wrappers_forward_to_the_same_controller_without_running_it(self):
        # exec does not use shell functions: replace the executable on PATH.
        # --preflight is a second guard against device access if the stub fails.
        with tempfile.TemporaryDirectory(prefix=".test-balmunlock-", dir=ROOT) as temporary:
            fake = Path(temporary) / "python3"
            fake.write_text('#!/usr/bin/env bash\nprintf "%s\\n" "$@"\n', encoding="utf-8")
            fake.chmod(0o755)
            bin_path = Path(temporary).as_posix()
            if os.name == "nt":
                bin_path = "/" + bin_path[0].lower() + bin_path[2:]
            stub = 'export PATH="$2:$PATH"; source "$1" --preflight --lang ru'
            for name, forced in (("unlock.sh", False), ("unlock.en.sh", True)):
                result = subprocess.run([self.bash, "-c", stub, "test", name, bin_path], cwd=ROOT,
                                        capture_output=True, text=True, encoding="utf-8", timeout=15)
                self.assertEqual(result.returncode, 0, result.stderr)
                args = result.stdout.splitlines()
                self.assertEqual(args[:2], ["-X", "utf8"])
                self.assertTrue(args[2].endswith("/tools/unlock.py"))
                self.assertEqual(args[3:], (["--lang", "en"] if forced else []) + ["--preflight", "--lang", "ru"])
