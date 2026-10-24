"""Two UI languages; Windows display language or POSIX message locale. No setlocale."""
import locale
import os
import sys


def _windows_ui_language():
    import ctypes
    function = ctypes.WinDLL("kernel32", use_last_error=True).GetUserDefaultUILanguage
    function.argtypes = []
    function.restype = ctypes.c_ushort
    return function()


def _language_name(value):
    primary = value.lower().replace("-", "_").split("_", 1)[0].split(".", 1)[0].split("@", 1)[0]
    return "ru" if primary in ("ru", "russian") else "en"


def default_language():
    if sys.platform == "win32":
        try:
            lang_id = _windows_ui_language()
            if lang_id:
                return "ru" if lang_id & 0x3ff == 0x19 else "en"
        except (OSError, AttributeError, RuntimeError):
            pass
    for name in ("LC_ALL", "LC_MESSAGES", "LANG"):
        if os.environ.get(name):
            return _language_name(os.environ[name])
    try:
        value = locale.getlocale(getattr(locale, "LC_MESSAGES", locale.LC_CTYPE))[0]
        return _language_name(value or "")
    except (ValueError, TypeError, OSError):
        return "en"


def language_from_args(args):
    """Read only valid explicit overrides for startup errors; the CLI validates syntax."""
    selected = None
    for index, value in enumerate(args):
        if value == "--lang" and index + 1 < len(args) and args[index + 1] in ("ru", "en"):
            selected = args[index + 1]
        elif value in ("--lang=ru", "--lang=en"):
            selected = value.partition("=")[2]
    return selected or default_language()


class LocalizedValueError(ValueError):
    def __init__(self, en, ru):
        super().__init__(en)
        self.ru = ru


def error_message(error, lang):
    if lang == "ru":
        if getattr(error, "ru", None):
            return error.ru
        if isinstance(error, (KeyboardInterrupt, EOFError)):
            return "Операция прервана."
        if isinstance(error, FileNotFoundError):
            return f"Не найден файл или инструмент: {error.filename or 'см. --verbose'}."
        if isinstance(error, PermissionError):
            return f"Нет доступа к файлу или каталогу: {error.filename or 'см. --verbose'}."
        return "Проверка или операция не завершена. Подробности — в журнале или с --verbose."
    return str(error).splitlines()[0] if str(error) else "Operation interrupted."
