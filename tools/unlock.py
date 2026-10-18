#!/usr/bin/env python3
"""Shared RU/EN unlock workflow. --preflight never invokes ADB or opens USB."""
# Modified 2026-10-18: persistent paths and helper dispatch for the console EXE.
from __future__ import annotations

import argparse
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
import hashlib
import importlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time

from chkcode_backup import checksum_path, load_backup, require_new_backup, save_backup
from cve_cleanup import STATE_NAME, CleanupError, cleanup_pending, load_state as load_cleanup_state
from device_policy import validate_identity
from operation_signals import OperationSignals
from session_lock import session_lock
from session_store import SessionStore, valid_serial
from runtime_paths import APP_NAME, VERSION, frozen, session_root, tool_command

ROOT = session_root()


def recovery_command():
    return ".\\balmunlock.exe --recover" if frozen() else "python tools/unlock.py --recover"


class Stop(RuntimeError):
    def __init__(self, message, ru=None):
        super().__init__(message)
        self.ru = ru


class AlreadyUnlocked(Stop):
    pass


class Declined(Stop):
    pass


def parse_unlocked(output: str):
    values = re.findall(
        r"^\s*(?:\(bootloader\)\s*)?(?:Device unlocked|unlocked)\s*:\s*"
        r"(true|false|yes|no)\s*$", output, re.I | re.M)
    states = {value.lower() in ("true", "yes") for value in values}
    return states.pop() if len(states) == 1 else None


def devices(output: str):
    return [line.split()[:2] for line in output.splitlines()
            if line.strip() and not line.startswith(("List of devices", "*"))]


def preflight(lang: str) -> bool:
    """Only host dependency lookup and library loading; no device discovery."""
    checks = {name: shutil.which(name) for name in ("adb", "fastboot")}
    try:
        usb = importlib.import_module("usb")
        backend = importlib.import_module("usb.backend.libusb1").get_backend()
        checks["pyusb"] = getattr(usb, "__version__", "installed")
        checks["libusb"] = "available" if backend is not None else None
    except (ImportError, OSError) as error:
        checks["pyusb/libusb"] = None
        print(str(error), file=sys.stderr)
    print("Локальная проверка (без обращения к телефону):" if lang == "ru"
          else "Host checks (no device access):")
    print(f"Python: {sys.version.split()[0]}")
    for name, value in checks.items():
        print(f"{name}: {value or 'MISSING'}")
    return all(checks.values())


class Workflow:
    def __init__(self, lang="ru", interactive=False, verbose=False):
        self.lang = lang
        self.interactive = interactive
        self.verbose = verbose
        self.introduced = False
        self.session_announced = False
        self.log_warning = False
        self.serial = None
        self.root = ROOT
        self.session_serial = None
        self.backup = self.root / "chkcode_backup.bin"
        self.recovery_backup = self.root / "chkcode_backup.recovery.bin"
        self.receipt = self.root / "chkcode_backup.device.json"
        self.marker_possible = False
        self.marker_verified = False
        self.original = None
        self.state = {}
        self.signals = OperationSignals(lang)
        self.env = os.environ.copy()
        self.initial_serial = self.env.get("ANDROID_SERIAL")
        self.env["PYTHONUTF8"] = "1"
        self.env["BALMUNLOCK_CONTROLLER"] = "1"

    def say(self, ru, en):
        print(ru if self.lang == "ru" else en, flush=True)

    def introduce(self):
        if not self.introduced:
            print(f"{APP_NAME} {VERSION}", flush=True)
            self.introduced = True

    def show_directory(self):
        if not self.session_announced:
            self.say(f"Бэкапы и журналы: {self.root}", f"Backups and logs: {self.root}")
            self.session_announced = True

    def detail(self, text):
        """Best-effort diagnostics must never change a verified write result."""
        try:
            with (self.root / "balmunlock.log").open("a", encoding="utf-8") as stream:
                stream.write(f"{datetime.now(timezone.utc).isoformat()} | {text}\n")
        except OSError:
            if not self.log_warning:
                self.say("Не удалось сохранить диагностический журнал. Проверьте место на диске и право записи в папку.",
                         "Could not save the diagnostic log. Check free disk space and directory write permissions.")
                self.log_warning = True

    def show_error(self, error):
        self.detail(f"ERROR {type(error).__name__}: {error}")
        message = getattr(error, "ru", None) if self.lang == "ru" else None
        label = "Причина" if self.lang == "ru" else "Reason"
        lines = str(error).splitlines()
        fallback = "Операция прервана." if self.lang == "ru" else "Operation interrupted."
        print(f"{label}: {message or (lines[0] if lines else fallback)}", file=sys.stderr, flush=True)
        if self.verbose and message:
            print(str(error), file=sys.stderr, flush=True)

    def phase_label(self, phase):
        if phase in ("reboot_submitted", "already_unlocked"):
            return "анлок завершён" if self.lang == "ru" else "unlock completed"
        if phase in ("prepared", "backup_ready"):
            return "бэкапы сохранены, анлок не начат" if self.lang == "ru" else "backups saved; unlock not started"
        if phase in ("rollback_reboot_submitted", "rollback_diag_verified", "recovery_android_online", "recovery_reboot_submitted"):
            return "откат завершён" if self.lang == "ru" else "rollback completed"
        labels = {"new": ("новый сеанс", "new session"),
                  "invalid": ("журнал требует проверки", "receipt needs inspection"),
                  "cleanup_pending": ("нужна очистка временных настроек", "temporary settings need cleanup"),
                  "backup_without_receipt": ("бэкап без журнала", "backup without a receipt")}
        ru, en = labels.get(phase, ("нужно завершить восстановление", "recovery needs completion"))
        return ru if self.lang == "ru" else en

    def ask(self, ru, en):
        self.say(ru + " [yes/N]", en + " [yes/N]")
        while True:
            answer = input().strip().lower()
            if answer == "yes":
                return True
            if answer in ("", "no", "n", "нет"):
                return False
            self.say("Для продолжения введите yes, для выхода — no или нажмите Enter.",
                     "Enter yes to continue, or no/Enter to stop.")

    def retry(self, ru, en):
        if not self.interactive:
            return False
        if not self.ask(ru, en):
            raise Declined("Retry declined by the user")
        return True

    def bind_session(self, session):
        if session.error:
            raise Stop(f"Saved session is invalid: {session.root}. {session.error}. Files retained.",
                       "Файлы сеанса повреждены или противоречат друг другу. Сохраните папку и проверьте исходные копии журналов.")
        if self.initial_serial and session.serial and self.initial_serial != session.serial:
            raise Stop("Selected session conflicts with ANDROID_SERIAL; no device command sent")
        self.root = session.root
        self.session_serial = session.serial
        self.serial = session.serial
        self.backup = self.root / "chkcode_backup.bin"
        self.recovery_backup = self.root / "chkcode_backup.recovery.bin"
        self.receipt = self.root / "chkcode_backup.device.json"
        if session.serial:
            self.env["ANDROID_SERIAL"] = session.serial
        self.say(f"Серийный номер: {session.serial or 'не установлен'}", f"Serial: {session.serial or 'unknown'}")
        self.session_announced = False
        self.show_directory()

    def choose_session(self, entries, allow_refresh=False):
        self.say("Сохранённые сеансы:", "Saved sessions:")
        for index, entry in enumerate(entries, 1):
            print(f"  {index}. {entry.serial or '?'} — {self.phase_label(entry.phase)}", flush=True)
            if self.verbose:
                print(f"     {entry.key}", flush=True)
        if not self.interactive:
            raise Stop("Select a saved session with --session SERIAL_OR_KEY; see --list-sessions")
        self.say("Введите номер сеанса или нажмите Enter для выхода.",
                 "Enter a session number, or press Enter to stop.")
        if allow_refresh:
            self.say("Для нового телефона: подключите его и введите r для повторного поиска.",
                     "For a new phone: connect it and enter r to search again.")
        while True:
            answer = input().strip()
            if allow_refresh and answer.lower() == "r":
                return None
            if answer.lower() in ("", "no", "n", "нет"):
                raise Declined("Session selection cancelled")
            if answer.isascii() and answer.isdigit() and 1 <= int(answer) <= len(entries):
                return entries[int(answer) - 1]
            self.say("Введите номер из списка или нажмите Enter.",
                     "Enter a number from the list, or press Enter.")

    def connected_devices(self):
        """Read-only transport discovery; no reboot, shell command or DIAG activation."""
        found = {}
        for program in ("adb", "fastboot"):
            try:
                result = self.run([program, "devices"], timeout=10, check=False)
            except Stop:
                continue
            if result.returncode:
                continue
            for row in devices(result.stdout):
                if len(row) != 2 or not valid_serial(row[0]):
                    continue
                if (program == "adb" and row[1] in ("device", "unauthorized", "offline", "recovery", "sideload")) or (
                        program == "fastboot" and row[1] == "fastboot"):
                    found.setdefault(row[0], set()).add(program)
        return found

    def route_session(self, args):
        store = SessionStore(ROOT)
        if args.session:
            self.bind_session(store.select(args.session))
            return True
        while True:
            entries = store.entries()
            found = self.connected_devices()
            if len(found) > 1:
                if self.retry("Подключено несколько телефонов. Оставьте один и введите yes для выбора его сеанса.",
                              "Several phones are connected. Leave one and enter yes to select its session."):
                    continue
                raise Stop("Connect one phone at a time; no session selected")
            if found:
                serial = next(iter(found))
                if self.initial_serial and serial != self.initial_serial:
                    if self.retry("Подключён другой телефон, чем указан в ANDROID_SERIAL. Подключите нужный. Проверить снова?",
                                  "The attached phone differs from ANDROID_SERIAL. Connect the expected phone. Check again?"):
                        continue
                    raise Stop("ANDROID_SERIAL differs from the connected phone; no session selected")
                matching = [entry for entry in entries if entry.serial == serial]
                if len(matching) > 1:
                    session = self.choose_session(matching)
                elif matching:
                    session = matching[0]
                elif args.recover:
                    self.say("Для подключённого телефона нет сохранённого сеанса восстановления. "
                             "Новые бэкапы и анлок не запускаются.",
                             "No recovery session exists for the attached phone. No new backup or unlock is started.")
                    if entries:
                        self.bind_session(self.choose_session(entries))
                        return True
                    return False
                elif found[serial] == {"fastboot"}:
                    self.say("Телефон в загрузчике, но подходящий сеанс не найден. Для восстановления нужны его исходные бэкапы. "
                             "Если они лежат рядом с программой без журнала, выберите --session legacy. "
                             "Для первого анлока телефон должен быть загружен в Android.",
                             "The phone is in fastboot without a matching session. Recovery needs its original backups. "
                             "For older backups without a receipt beside the program, use --session legacy. "
                             "A first unlock must start from Android.")
                    raise Stop("Phone is in fastboot without a matching saved session. "
                               "Use its original backup folder or --session legacy if old backups have no receipt. "
                               "For a first unlock, return the unmodified phone to Android first.")
                else:
                    self.say("Новый телефон: создаю отдельный сеанс.", "New phone: creating a separate session.")
                    session = store.create(serial)
                self.bind_session(session)
                return True
            if entries:
                self.say("Телефон пока не обнаружен. Выберите сохранённый сеанс или повторите поиск после подключения.",
                         "No phone detected. Select a saved session, or search again after connecting a phone.")
                session = self.choose_session(entries, allow_refresh=True) if self.interactive or len(entries) > 1 else entries[0]
                if session is None:
                    continue
                self.bind_session(session)
                return True
            if args.recover:
                self.say("Сохранённых сеансов нет. Восстанавливать нечего.",
                         "No saved sessions exist. Nothing needs recovery.")
                return False
            self.say("Подключите один телефон с включённой отладкой по USB. Ожидаю подключение...",
                     "Connect one phone with USB debugging enabled. Waiting for connection...")
            self.select_adb()
            self.bind_session(store.create(self.serial))
            return True

    def run(self, args, timeout=15, check=True):
        try:
            result = subprocess.run([str(arg) for arg in args], cwd=self.root,
                                    env=self.env, capture_output=True, text=True,
                                    encoding="utf-8", errors="replace", timeout=timeout,
                                    **self.signals.child_options())
        except subprocess.TimeoutExpired as error:
            self.detail(f"TIMEOUT {timeout}s: {args[0:3]}")
            raise Stop(f"Command timed out ({timeout}s): {args[0:3]}",
                       "Команда не завершилась за отведённое время. Проверьте подключение телефона; подробности в журнале.") from error
        except OSError as error:
            raise Stop(str(error), f"Не удалось запустить {args[0]}. Проверьте файлы сборки или зависимости по README.md.") from error
        self.detail(f"COMMAND {list(map(str, args))}\nEXIT {result.returncode}\n{result.stdout}\n{result.stderr}")
        if check and result.returncode:
            raise Stop(f"Command failed: {args}\n{result.stdout}\n{result.stderr}",
                       "Команда завершилась с ошибкой. Подробности сохранены в журнале; следуйте указаниям восстановления ниже.")
        return result

    def tool(self, name, *args, check=True):
        if name == "cve31317.py" and self.root != ROOT:
            args = (*args, "--state", str(self.root / STATE_NAME))
        # Never kill a partition-writing process on a host-side timer.
        timeout = None if name in ("unlock_chkcode.py", "restore_chkcode.py") else 600
        if name == "diag_probe.py":
            timeout = 30
        # libusb opens all WinUSB interfaces of this composite device. The
        # Windows ADB server can hold MI_02 and make libusb_open fail for DIAG
        # with ACCESS (-3), even though MI_00 has its own valid WinUSB driver.
        # Keep this host-side coordination out of the low-level USB library.
        exclusive_diag = os.name == "nt" and name in (
            "kdiag_shell.py", "backup_chkcode.py", "unlock_chkcode.py", "restore_chkcode.py",
            "diag_probe.py", "verify_chkcode.py")
        if exclusive_diag:
            self.run(["adb", "kill-server"])
        try:
            result = self.run(tool_command(name, *args, root=ROOT), timeout=timeout, check=check)
        finally:
            if exclusive_diag:
                try:
                    self.run(["adb", "start-server"])
                except Stop as error:
                    # Do not turn a verified partition write into an ambiguous
                    # failed write merely because the host ADB restart failed.
                    # The next ADB command can start its server again.
                    print(f"ADB server restart pending: {error}", file=sys.stderr, flush=True)
            if name == "cve31317.py":
                # The child can be killed by timeout/Ctrl+C before its finally.
                self.cleanup_cve()
        if self.verbose:
            print(result.stdout, end="", flush=True)
            print(result.stderr, end="", file=sys.stderr, flush=True)
        return result

    def cleanup_cve(self):
        with self.signals.defer():
            try:
                cleaned = cleanup_pending(self.root / STATE_NAME, expected_serial=self.serial,
                                          child_options=self.signals.child_options())
            except CleanupError as error:
                raise Stop(str(error)) from error
        self.signals.checkpoint()
        return cleaned

    def fastboot(self, *args, timeout=10, check=True):
        return self.run(["fastboot", "-s", self.serial, *args], timeout, check)

    def select_adb(self, timeout=120):
        deadline = time.monotonic() + timeout
        connection_hint_shown = False
        while True:
            result = self.run(["adb", "devices"], timeout=5)
            rows = devices(result.stdout)
            if len(rows) > 1:
                if self.retry("Подключено несколько Android-устройств. Отключите остальные, затем введите yes для повторной проверки.",
                              "Several Android devices are connected. Disconnect the others, then enter yes to check again."):
                    deadline = time.monotonic() + timeout
                    continue
                raise Stop("Connect exactly one Android device, including offline/unauthorized devices.")
            if rows:
                if len(rows[0]) != 2 or rows[0][1] != "device":
                    if self.retry("Телефон ещё не разрешил отладку. Разблокируйте экран, подтвердите запрос «Разрешить отладку по USB» "
                                  "и отметьте «Всегда разрешать с этого компьютера». Если запроса нет, переподключите кабель. Проверить снова?",
                                  "ADB is offline or unauthorized. Unlock the screen, accept USB debugging and select Always allow "
                                  "from this computer. Reconnect the cable if no prompt appears. Check again?"):
                        deadline = time.monotonic() + timeout
                        continue
                    raise Stop("ADB device is not authorized/online. Authorize it and retry before writing.")
                candidate = rows[0][0]
                if self.env.get("ANDROID_SERIAL") not in (None, "", candidate):
                    if self.retry("Подключён другой телефон. Подключите исходный телефон сохранённого сеанса. Проверить снова?",
                                  "A different phone is connected. Connect the original session device. Check again?"):
                        deadline = time.monotonic() + timeout
                        continue
                    raise Stop("ANDROID_SERIAL does not match the attached device.")
                self.serial = candidate
                self.env["ANDROID_SERIAL"] = self.serial
                return
            if self.interactive and not connection_hint_shown:
                self.say("Телефон не виден в ADB. Разблокируйте экран, включите отладку, выберите передачу файлов и проверьте кабель. "
                         "Если он остался в fastboot после начальной проверки драйвера, верните его в Android кнопкой питания. "
                         "Если анлок уже начинался, используйте папку с его бэкапами и журналом для восстановления.",
                         "No ADB device is visible. Unlock the screen, enable debugging, select file transfer and check the cable. "
                         "If initial driver setup left the phone in fastboot, return to Android with the power button. "
                         "If unlocking had already started, use its original backup/receipt directory for recovery.")
                connection_hint_shown = True
            if time.monotonic() >= deadline:
                if self.retry("Телефон не появился в ADB. Проверьте кабель, передачу файлов, отладку по USB и драйвер ADB. Подождать и проверить снова?",
                              "No ADB device appeared. Check the cable, file transfer, USB debugging and ADB driver. Wait and check again?"):
                    deadline = time.monotonic() + timeout
                    continue
                raise Stop("Timed out waiting for an authorized ADB device. No chkcode write attempted.")
            time.sleep(1)

    def fastboot_present(self):
        try:
            result = self.run(["fastboot", "devices"], timeout=5, check=False)
        except Stop:
            return False
        return result.returncode == 0 and [self.serial, "fastboot"] in devices(result.stdout)

    def wait_fastboot(self, timeout=120):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.fastboot_present():
                return
            time.sleep(1)
        raise Stop("Timed out waiting for the same serial in fastboot.")

    def unlocked(self):
        # The model's guide documents oem device-info; getvar is a fallback.
        # Unsupported/empty replies must never mean success.
        for command in (("oem", "device-info"), ("getvar", "unlocked")):
            result = self.fastboot(*command, timeout=5, check=False)
            if result.returncode == 0:
                state = parse_unlocked(result.stdout + "\n" + result.stderr)
                if state is not None:
                    return state
                if re.search(r"(?:Device unlocked|unlocked)\s*:", result.stdout + result.stderr, re.I):
                    return None  # Conflicting/malformed state is not an unsupported command.
        return None

    def save_state(self, phase, **fields):
        self.state.update(fields)
        self.state.update(phase=phase, marker_possible=self.marker_possible,
                          marker_verified=self.marker_verified)
        # Atomic replacement keeps a complete prior state across an interruption.
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.receipt.parent,
                                             prefix=".unlock-state-", suffix=".tmp", delete=False) as stream:
                temporary = Path(stream.name)
                json.dump(self.state, stream, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.receipt)
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink()

    def checked_original(self):
        errors = []
        for path in (self.backup, self.recovery_backup):
            try:
                data = load_backup(path)
                if self.original is not None and data != self.original:
                    raise ValueError("Image differs from this session's original")
                if hashlib.sha256(data).hexdigest() != self.state.get("sha256"):
                    raise ValueError("Image hash does not match the device receipt")
                if self.original is None:
                    self.original = data
                if path == self.recovery_backup:
                    self.say("Основной бэкап недоступен/повреждён; выбрана проверенная вторая копия.",
                             "Primary backup unavailable/damaged; using the verified second copy.")
                return path
            except (OSError, ValueError) as error:
                errors.append(f"{path.name}: {error}")
        raise Stop("No valid original backup for this session. No restore attempted.\n" + "\n".join(errors))

    def load_session(self):
        state = json.loads(self.receipt.read_text(encoding="utf-8"))
        if not isinstance(state, dict) or state.get("version") != 1:
            raise Stop("Unsupported recovery receipt. See README.md#recovery.")
        serial = state.get("serial", "")
        if not isinstance(serial, str) or not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", serial):
            raise Stop("Invalid recovery serial.")
        if not isinstance(state.get("marker_possible"), bool) or not isinstance(state.get("marker_verified"), bool):
            raise Stop("Incomplete recovery receipt.")
        if not isinstance(state.get("sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", state["sha256"]):
            raise Stop("Invalid recovery image hash.")
        if "reboot_pending" in state and not isinstance(state["reboot_pending"], bool):
            raise Stop("Invalid saved reboot state. Keep the original receipt.")
        if self.session_serial is not None and serial != self.session_serial:
            raise Stop("Recovery receipt belongs to a different session directory; no commands sent")
        self.state = state
        self.serial = serial
        self.env["ANDROID_SERIAL"] = serial
        self.checked_original()
        self.marker_possible = state["marker_possible"]
        self.marker_verified = state["marker_verified"]

    def adb_present(self):
        try:
            result = self.run(["adb", "-s", self.serial, "get-state"], timeout=5, check=False)
        except Stop:
            return False
        return result.returncode == 0 and result.stdout.strip() == "device"

    def is_userspace(self):
        result = self.fastboot("getvar", "is-userspace", check=False)
        modes = {value.lower() for value in re.findall(
            r"^\s*(?:\(bootloader\)\s*)?is-userspace:\s*(yes|no)\s*$",
            result.stdout + "\n" + result.stderr, re.I | re.M)}
        if result.returncode or len(modes) != 1:
            return None
        return modes == {"yes"}

    def recover(self, timeout=120):
        """One bounded recovery attempt; retain the receipt if USB is unavailable."""
        self.signals.start_recovery()
        self.checked_original()
        if not self.marker_possible and not self.state.get("reboot_pending"):
            self.say("Метка уже снята по журналу; повторной записи не будет.",
                     "The receipt records a completed restore; no repeat write.")
            return True
        self.say("Автоматический откат chkcode. Ожидание того же устройства до 120 секунд...",
                 "Attempting automatic chkcode rollback. Waiting up to 120s for the same device...")
        self.save_state("recovery_wait")
        deadline = time.monotonic() + timeout
        last_error = "Original device not found over ADB/fastboot"
        while time.monotonic() < deadline:
            try:
                if not self.marker_possible:
                    # A completed flash is never repeated just because reboot
                    # failed or its USB reply was lost.
                    if self.adb_present():
                        self.save_state("recovery_android_online", reboot_pending=False)
                        return True
                    if self.fastboot_present():
                        self.fastboot("reboot", timeout=15)
                        self.save_state("recovery_reboot_submitted", reboot_pending=False)
                        return True
                    time.sleep(1)
                    continue
                if self.adb_present():
                    image = self.checked_original()
                    with self.signals.defer():
                        self.save_state("rollback_diag_started")
                        # No automatic DIAG activation or reboot to obtain access.
                        self.tool("restore_chkcode.py", "--backup", str(image), "--yes")
                        self.marker_possible = False
                        self.save_state("rollback_diag_verified", reboot_pending=False)
                    self.signals.checkpoint()
                    self.say("Откат chkcode подтверждён полным чтением. Телефон оставлен в Android.",
                             "chkcode rollback verified by a full read. The phone remains in Android.")
                    return True
                if self.fastboot_present():
                    userspace = self.is_userspace()
                    if userspace is True:
                        raise Stop("Automatic rollback refuses fastbootd; keep the current state.")
                    if userspace is None:
                        last_error = "Bootloader mode is unknown; no automatic flash/reboot"
                        time.sleep(1)
                        continue
                    state = self.unlocked()
                    if state is None:
                        last_error = "Bootloader lock state is unknown; no automatic flash/reboot"
                        time.sleep(1)
                        continue
                    if not state and not self.marker_verified:
                        raise Stop("Locked bootloader without a verified service marker; no automatic flash.")
                    image = self.checked_original()
                    with self.signals.defer():
                        self.save_state("rollback_fastboot_started", observed_unlocked=state)
                        # The upstream ABL analysis permits this service write
                        # even when locked, if our marker was verified.
                        self.fastboot("flash", "chkcode", str(image), timeout=None)
                        self.marker_possible = False
                        self.save_state("rollback_fastboot_restored", reboot_pending=True)
                    self.signals.checkpoint()
                    self.fastboot("reboot", timeout=15)
                    self.save_state("rollback_reboot_submitted", reboot_pending=False)
                    self.say("Исходный chkcode восстановлен, команда перезагрузки принята. "
                             "Проверьте загрузку Android.",
                             "Original chkcode restored; reboot accepted. Check Android boot.")
                    return True
            except Stop as error:
                # Failed writes are not repeated in this recovery attempt.
                last_error = str(error)
                if self.state.get("phase") in ("rollback_diag_started", "rollback_fastboot_started",
                                               "rollback_fastboot_restored"):
                    break
            time.sleep(1)
        self.save_state("recovery_pending", recovery_error=last_error)
        self.say("Автооткат не завершён. Бэкап и состояние сохранены. "
                 f"После восстановления USB: {recovery_command()}",
                 "Automatic rollback incomplete. Backup/state retained. "
                 f"After restoring USB: {recovery_command()}")
        print(last_error, file=sys.stderr, flush=True)
        return False

    def wait_unlocked(self, timeout=300):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.fastboot_present() and self.unlocked() is True:
                return
            time.sleep(1)
        raise Stop("Unlock not confirmed within 300s. Starting recovery instead of continuing unlock.")

    def wait_prop(self, key, expected, timeout=30):
        deadline = time.monotonic() + timeout
        last = "unavailable"
        while time.monotonic() < deadline:
            try:
                result = self.run(["adb", "-s", self.serial, "shell", "getprop", key],
                                  timeout=5, check=False)
                last = result.stdout.strip()
                if result.returncode == 0 and last == expected:
                    return
            except Stop as error:
                last = str(error)
            time.sleep(1)
        raise Stop(f"Required postcondition not observed: {key}={expected}; last={last!r}")

    def check_bootloader(self, identity):
        """Check the driver and locked bootloader before any CVE or marker write."""
        self.say("Проверка загрузчика. Телефон перезагрузится в fastboot; анлок пока не начинается.",
                 "Checking the bootloader. The phone will reboot into fastboot; unlocking has not started.")
        if os.name == "nt":
            self.say("Если понадобится драйвер: Zadig (https://zadig.akeo.ie/) → Device → Load Preset Device → "
                     "tools/balmuda-fastboot.cfg → WinUSB для 18D1:D00D. Ожидаю подключение до 5 минут.",
                     "If a driver is needed: Zadig (https://zadig.akeo.ie/) > Device > Load Preset Device > "
                     "tools/balmuda-fastboot.cfg > WinUSB for 18D1:D00D. Waiting up to 5 minutes.")
        else:
            self.say("Ожидаю fastboot до 5 минут. Если телефон не обнаружен, проверьте USB-права/udev для 18D1:D00D.",
                     "Waiting up to 5 minutes for fastboot. If missing, check USB permissions/udev for 18D1:D00D.")
        self.run(["adb", "-s", self.serial, "reboot", "bootloader"])
        while True:
            try:
                self.wait_fastboot(timeout=300)
                break
            except Stop as error:
                if os.name == "nt":
                    self.say("Fastboot не обнаружен. Установите WinUSB для 18D1:D00D через "
                             "Zadig → Device → Load Preset Device → tools/balmuda-fastboot.cfg.",
                             "Fastboot is not detected. Install WinUSB for 18D1:D00D using "
                             "Zadig > Device > Load Preset Device > tools/balmuda-fastboot.cfg.")
                else:
                    self.say("Fastboot не обнаружен. Проверьте USB-права/udev для 18D1:D00D и кабель.",
                             "Fastboot is not detected. Check USB permissions/udev for 18D1:D00D and the cable.")
                try:
                    again = self.retry("Телефон оставьте в загрузчике. После исправления проверить снова?",
                                       "Keep the phone in the bootloader. Check again after fixing the connection?")
                except Declined:
                    self.say("Начальная проверка остановлена до записи метки. Телефон пока в загрузчике; "
                             "верните его в Android долгим удержанием кнопки питания.",
                             "Initial check stopped before any marker write. The phone remains in the bootloader; "
                             "return it to Android by holding its power button.")
                    raise
                if again:
                    continue
                raise Stop("Fastboot driver check failed before any chkcode write. "
                           "Install the bootloader driver, restart the phone into Android "
                           "using its power button, then rerun. See README.md#drivers.") from error
        try:
            result = self.fastboot("getvar", "is-userspace", check=False)
            modes = set(re.findall(r"^\s*(?:\(bootloader\)\s*)?is-userspace:\s*(yes|no)\s*$",
                                   result.stdout + "\n" + result.stderr, re.I | re.M))
            if result.returncode or {value.lower() for value in modes} != {"no"}:
                raise Stop("Bootloader mode not confirmed (fastbootd/unknown). No marker write.")
            state = self.unlocked()
            if state is None:
                raise Stop("Bootloader lock state unknown. No marker write.")
            if state:
                raise AlreadyUnlocked("The bootloader is already unlocked. No unlock or marker write needed.")
        except (Stop, KeyboardInterrupt):
            # This is the stock, marker-free device; return it to Android on refusal.
            # A failed return must not hide the original validation/cancellation error.
            try:
                self.fastboot("reboot", timeout=15)
            except (Stop, KeyboardInterrupt) as error:
                self.detail(f"Return to Android not confirmed: {error}")
                self.say("Возврат в Android не подтверждён. При начальной проверке метка ещё не записывалась; "
                         "если телефон остался в загрузчике, перезапустите его долгим удержанием кнопки питания.",
                         "Return to Android was not confirmed. No marker was written during this initial check; "
                         "if still in the bootloader, restart the phone by holding its power button.")
            raise
        self.fastboot("reboot", timeout=15)
        self.say("Fastboot проверен. Ожидание Android и повторная проверка телефона...",
                 "Fastboot checked. Waiting for Android and rechecking device identity...")
        self.wait_prop("sys.boot_completed", "1", timeout=180)
        for key, expected in identity.items():
            result = self.run(["adb", "-s", self.serial, "shell", "getprop", key])
            if result.stdout.strip() != expected:
                raise Stop(f"Device identity changed after the fastboot check: {key}. No marker write.")

    def ensure_diag(self):
        if not self.interactive:
            return
        while True:
            try:
                result = self.tool("diag_probe.py", check=False)
                code = result.returncode
            except Stop as error:
                self.record_error(error)
                code = 2
            if code == 0:
                return
            if code == 3:
                raise Stop("DIAG identity is ambiguous or belongs to another phone. No retry of partition operations.")
            if os.name == "nt":
                self.say("DIAG пока недоступен. Оставьте телефон подключённым. В Zadig (https://zadig.akeo.ie/) включите Options → List All Devices, "
                         "выберите BALMUDA_Android (Interface 0), проверьте USB ID 0482:0A9D и установите WinUSB. "
                         "Дождитесь завершения установки драйвера.",
                         "DIAG is not accessible yet. Keep the phone connected. In Zadig (https://zadig.akeo.ie/) enable Options > List All Devices, "
                         "select BALMUDA_Android (Interface 0), verify USB ID 0482:0A9D and install WinUSB. Wait for installation to finish.")
            else:
                self.say("DIAG пока недоступен. Проверьте USB-права/udev для 0482:0A9D, кабель и привязку qcserial.",
                         "DIAG is not accessible yet. Check USB permissions/udev for 0482:0A9D, the cable and qcserial binding.")
            if code == 4:
                self.say("Интерфейс открывается, но DIAG не отвечает. Переподключите кабель и проверьте телефон.",
                         "The interface opens, but DIAG does not respond. Reconnect the cable and check the phone.")
            if not self.ask("После исправления введите yes для повторной проверки. Бэкап и метка пока не записываются. Проверить?",
                            "Enter yes after fixing the connection. No backup or marker write occurs during this check. Retry?"):
                raise Declined("DIAG setup paused by the user; no marker write.")

    def activate_diag(self):
        self.say("Подготовка диагностического режима DIAG. Это может занять несколько минут; "
                 "дождитесь следующего сообщения. При необходимости появится инструкция установки драйвера.",
                 "Preparing DIAG diagnostics. This may take a few minutes; wait for the next message. "
                 "Driver installation steps will appear if needed.")
        self.tool("cve31317.py")
        self.wait_prop("sys.usb.config", "diag,modem,adb")
        self.ensure_diag()
        self.tool("kdiag_shell.py", "setprop vendor.kc.diag.status start")
        try:
            self.wait_prop("init.svc.fs_sys_call_diag", "running")
        except Stop:
            self.say("Файловый сервис телефона не запустился. Это отдельная проверка после подключения DIAG. "
                     "Сохраните журнал ошибки и сверьте прошивку с README.md#diagnostics; запись метки не начинается.",
                     "The phone file service did not start after DIAG connection. Retain the error log and check "
                     "firmware/service diagnostics in README.md#diagnostics; no marker write starts.")
            raise

    def execute(self, prepare_only=False):
        # Refuse reruns before any ADB command or DIAG activation.
        require_new_backup(self.backup)
        require_new_backup(self.recovery_backup)
        if self.receipt.exists():
            raise Stop(f"Previous device receipt exists: {self.receipt}. See README.md#recovery.")
        if (self.root / STATE_NAME).exists():
            raise Stop("CVE cleanup is pending. Use --recover before starting a new session.")
        self.say("Ожидаю единственное авторизованное ADB-устройство...",
                 "Waiting for exactly one authorized ADB device...")
        self.select_adb()
        identity = {}
        for key in ("ro.product.model", "ro.build.version.incremental",
                    "ro.build.version.security_patch", "ro.build.version.sdk"):
            result = self.run(["adb", "-s", self.serial, "shell", "getprop", key])
            identity[key] = result.stdout.strip()
        validate_identity(identity, lang=self.lang)
        self.detail(json.dumps({"serial": self.serial, **identity}, ensure_ascii=False))
        self.say(f"Телефон: {identity['ro.product.model']}, Android 12, прошивка {identity['ro.build.version.incremental']}.",
                 f"Phone: {identity['ro.product.model']}, Android 12, build {identity['ro.build.version.incremental']}.")
        if not prepare_only:
            self.check_bootloader(identity)
        self.activate_diag()
        self.say("Создание бэкапов. Служебный раздел будет полностью прочитан дважды; запись в него ещё не начинается.",
                 "Creating backups. The service partition will be read twice in full; partition writes have not started.")
        self.tool("backup_chkcode.py", "-o", str(self.backup))
        original = self.original = load_backup(self.backup)
        # An independently validated local copy survives loss/corruption of one
        # backup file. Both copies stay on this host; this is not off-host backup.
        save_backup(self.recovery_backup, original)
        # Identity is a host receipt, not proof of firmware compatibility.
        self.state = {"version": 1, "serial": self.serial, **identity,
                      "sha256": hashlib.sha256(original).hexdigest()}
        self.save_state("backup_ready")
        self.say("Две проверенные копии служебного раздела и журнал сохранены.",
                 "Two verified service-partition copies and the receipt have been saved.")
        if prepare_only:
            self.save_state("prepared")
            self.say("Подготовка завершена: две проверенные копии chkcode и журнал сохранены. "
                     "Сервисная метка не записывалась; разблокировка не начиналась.",
                     "Preparation complete: two verified chkcode copies and the receipt saved. "
                     "No service marker written; unlocking has not started.")
            return
        self.unlock_from_backup()

    def resume_prepared(self, restart_recovered=False):
        """Explicit future unlock entry; never recreate or replace originals."""
        self.load_session()
        recovered = {"rollback_reboot_submitted", "rollback_diag_verified",
                     "recovery_android_online", "recovery_reboot_submitted"}
        allowed = {"prepared", "backup_ready"} | (recovered if restart_recovered and self.interactive else set())
        if (self.state.get("phase") not in allowed or self.marker_possible or
                (self.marker_verified and not restart_recovered) or self.state.get("reboot_pending")):
            raise Stop("Not a clean prepared session. Use recovery for a started unlock.")
        if (self.root / STATE_NAME).exists():
            raise Stop("CVE cleanup is pending. Finish cleanup before unlocking.")
        self.select_adb()
        for key in ("ro.product.model", "ro.build.version.incremental",
                    "ro.build.version.security_patch", "ro.build.version.sdk"):
            result = self.run(["adb", "-s", self.serial, "shell", "getprop", key])
            if result.stdout.strip() != self.state.get(key):
                raise Stop(f"Prepared device identity changed: {key}. No marker write.")
        validate_identity(self.state, lang=self.lang)
        if self.interactive:
            # Prove the marker is absent before rebooting a resumed device.
            self.activate_diag()
            self.tool("verify_chkcode.py", "--backup", str(self.checked_original()))
            if restart_recovered:
                previous_phase = self.state["phase"]
                self.marker_verified = False
                self.save_state("prepared", previous_phase=previous_phase, reboot_pending=False)
            identity = {key: self.state[key] for key in ("ro.product.model", "ro.build.version.incremental",
                        "ro.build.version.security_patch", "ro.build.version.sdk")}
            self.check_bootloader(identity)
            self.activate_diag()
        else:
            self.wait_prop("sys.usb.config", "diag,modem,adb")
            self.wait_prop("init.svc.fs_sys_call_diag", "running")
        # unlock_chkcode independently compares the entire live partition to
        # this checked image before it permits the first marker write.
        self.unlock_from_backup()

    def unlock_from_backup(self):
        image = self.checked_original()
        self.say("Бэкапы проверены. Записывается служебная метка; не отключайте USB и дождитесь запроса на телефоне.",
                 "Backups verified. Writing the service marker; keep USB connected and wait for the phone's confirmation screen.")
        # Conservatively assume a partial write even if the helper fails.
        with self.signals.defer():
            self.marker_possible = True
            self.save_state("marker_write_started")
            self.tool("unlock_chkcode.py", "--backup", str(image), "--yes")
            self.marker_verified = True
            self.save_state("marker_verified")
        self.signals.checkpoint()
        self.save_state("bootloader_requested")
        self.run(["adb", "-s", self.serial, "reboot", "bootloader"])
        self.wait_fastboot()
        if self.is_userspace() is not False:
            raise Stop("Device is in fastbootd, not the bootloader. No unlock attempted.")
        self.say("На телефоне кнопками громкости выберите Unlock the bootloader и подтвердите по подсказке экрана "
                 "(на A101BM — кнопкой питания). Данные будут стёрты. Ожидаю подтверждение до 5 минут...",
                 "On the phone, use the volume buttons to select Unlock the bootloader, then confirm as shown "
                 "(power button on A101BM). Data will be erased. Waiting up to 5 minutes...")
        self.save_state("unlock_requested")
        self.fastboot("flashing", "unlock", timeout=180)
        self.wait_unlocked()
        self.save_state("unlock_verified")
        self.say("Разблокировка подтверждена. Восстанавливаю исходный служебный раздел; не отключайте USB.",
                 "Unlock confirmed. Restoring the original service partition; keep USB connected.")
        # Revalidate the disk image immediately before flash; no zero image.
        image = self.checked_original()
        with self.signals.defer():
            self.save_state("restore_started")
            # Never cancel a partition write before its result and journal.
            self.fastboot("flash", "chkcode", str(image), timeout=None)
            self.marker_possible = False
            self.save_state("restored", reboot_pending=True)
        self.signals.checkpoint()
        self.fastboot("reboot", timeout=15)
        self.save_state("reboot_submitted", reboot_pending=False)
        self.say("Fastboot подтвердил прошивку исходного chkcode и принял перезагрузку. "
                 "Дождитесь приветственного экрана Android. Папку с бэкапами сохраните.",
                 "Fastboot confirmed flashing the original chkcode and accepted reboot. "
                 "Wait for Android's welcome screen and retain the backup directory.")

    def recovery_notice(self):
        if not self.marker_possible and not self.state.get("reboot_pending"):
            return
        if not self.marker_possible:
            self.say("chkcode восстановлен; завершение перезагрузки пока не подтверждено.",
                     "chkcode restored; reboot completion is still pending.")
        self.say("Не запускайте полный анлок заново. "
                 f"Для продолжения восстановления: {recovery_command()}",
                 "Do not rerun full unlock. "
                 f"To resume recovery: {recovery_command()}")
        self.say(f"Исходный телефон: {self.serial}\nСохраните всю папку: {self.root}",
                 f"Original phone: {self.serial}\nRetain the complete directory: {self.root}")

    def record_error(self, error):
        self.detail(f"ERROR {type(error).__name__}: {error}")
        path = self.root / "balmunlock-last-error.txt"
        try:
            path.write_text(f"Phase: {self.state.get('phase', 'before backup')}\n"
                            f"Serial: {self.serial or 'not selected'}\n{type(error).__name__}: {error}\n",
                            encoding="utf-8")
            self.say(f"Подробности сохранены: {path}", f"Details saved: {path}")
        except OSError:
            pass

    def backup_artifacts(self):
        return [p for image in (self.backup, self.recovery_backup) for p in (image, checksum_path(image))
                if p.exists() or p.is_symlink()]

    def adopt_backups(self):
        """Rebuild a missing receipt only after two complete, matching live reads."""
        images = []
        for path in (self.backup, self.recovery_backup):
            if path.exists() or checksum_path(path).exists() or path.is_symlink():
                try:
                    images.append((path, load_backup(path)))
                except (OSError, ValueError) as error:
                    raise Stop(f"Backup without a receipt cannot be validated: {path}. {error}. "
                               "Keep this folder and restore the original file/checksum from a known good copy; "
                               "do not generate a replacement checksum.") from error
        if not images or any(data != images[0][1] for _, data in images):
            raise Stop("Saved originals disagree or no complete original exists. Keep all files; no device command sent.")
        if not self.ask("Бэкап есть, но журнал отсутствует. Проверить подключённый телефон и дважды сравнить весь "
                        "chkcode с бэкапом без записи в раздел, чтобы восстановить журнал?",
                        "A backup exists without a receipt. Check the phone and compare two full live chkcode reads "
                        "against it, without partition writes, to rebuild the receipt?"):
            raise Declined("Backup inspection cancelled.")
        self.select_adb()
        identity = {}
        for key in ("ro.product.model", "ro.build.version.incremental", "ro.build.version.security_patch", "ro.build.version.sdk"):
            identity[key] = self.run(["adb", "-s", self.serial, "shell", "getprop", key]).stdout.strip()
        validate_identity(identity, lang=self.lang)
        self.activate_diag()
        self.tool("verify_chkcode.py", "--backup", str(images[0][0]))
        original = images[0][1]
        for path in (self.backup, self.recovery_backup):
            if not path.exists():
                save_backup(path, original)
        if self.receipt.exists():
            raise Stop("A receipt appeared during inspection. It was not replaced; recheck the saved session.")
        self.original = original
        self.state = {"version": 1, "serial": self.serial, **identity,
                      "sha256": hashlib.sha256(original).hexdigest()}
        self.marker_possible = self.marker_verified = False
        self.save_state("prepared")
        self.say("Журнал восстановлен после полного сравнения. Исходные бэкапы сохранены; метка не записывалась.",
                 "Receipt rebuilt after full comparison. Original backups retained; no marker written.")

    def guided_recover(self):
        while True:
            try:
                if self.recover():
                    self.say("Восстановление завершено. Проверьте экран телефона; бэкапы сохраните.",
                             "Recovery finished. Check the phone screen and retain the backups.")
                    self.say("Если анлок ещё нужен, обычный следующий запуск предложит повторить его с этими бэкапами после проверки телефона.",
                             "If unlocking is still needed, the next normal launch offers to retry using these backups after checking the phone.")
                    return 0
            except (Stop, OSError, ValueError) as error:
                self.record_error(error)
                self.show_error(error)
            if not self.ask("Восстановление пока не завершено. Подключите исходный телефон, проверьте кабель "
                            "и драйвер его текущего режима. Повторить только восстановление?",
                            "Recovery is unfinished. Connect the original phone and check the cable and driver "
                            "for its current mode. Retry recovery only?"):
                self.recovery_notice()
                return 1

    def guided(self, args):
        self.introduce()
        self.show_directory()
        terminal = {"reboot_submitted", "rollback_reboot_submitted", "rollback_diag_verified",
                    "recovery_android_online", "recovery_reboot_submitted", "already_unlocked"}
        while True:
            try:
                if (self.root / STATE_NAME).exists():
                    try:
                        cleanup_state = load_cleanup_state(self.root / STATE_NAME)
                    except CleanupError as error:
                        raise Stop(f"Cleanup receipt is invalid. Retain it and the error log: {error}") from error
                    self.say(f"Ожидает очистки телефон: {cleanup_state['serial']}. Подключите именно его.",
                             f"Pending cleanup device: {cleanup_state['serial']}. Connect this phone.")
                    if not self.ask("С прошлого запуска остались временные настройки Android. Восстановить их "
                                    "на исходном телефоне и затем продолжить проверку сеанса? Анлок пока не начинается.",
                                    "Temporary Android settings remain from an earlier run. Restore them on the original "
                                    "phone, then continue checking the session? Unlocking will not start yet."):
                        return 0
                    self.cleanup_cve()
                    self.say("Временные настройки восстановлены.", "Temporary settings restored.")
                if self.receipt.exists():
                    self.load_session()
                    phase = self.state.get("phase")
                    stage_ru = ("нужно завершить восстановление" if self.marker_possible or self.state.get("reboot_pending") else
                                "операция завершена" if phase in terminal else
                                "подготовка сохранена, анлок ещё не начат" if phase in ("prepared", "backup_ready") else
                                "этап требует проверки")
                    self.say(f"Найден сеанс: {self.state.get('ro.product.model', 'BALMUDA')}, "
                             f"телефон {self.serial}: {stage_ru}. Бэкапы проверены.",
                             f"Saved session: {self.state.get('ro.product.model', 'BALMUDA')}, "
                             f"device {self.serial}, phase {phase}. Backups verified.")
                    if self.marker_possible or self.state.get("reboot_pending"):
                        if self.marker_possible:
                            ru, en = ("Предыдущий анлок прерван. Восстановить исходный служебный раздел? Новый анлок не начинается.",
                                      "The previous unlock was interrupted. Restore the original service partition? No new unlock will start.")
                        else:
                            ru, en = ("Исходный раздел уже восстановлен. Завершить перезагрузку без повторной прошивки?",
                                      "The original partition is already restored. Finish reboot without flashing again?")
                        if not self.ask(ru, en):
                            return 0
                        return self.guided_recover()
                    if phase in terminal:
                        if phase not in ("reboot_submitted", "already_unlocked") and not args.recover and not args.prepare:
                            if self.ask("Предыдущий откат завершён. Проверить телефон и повторить анлок, используя эти же "
                                        "оригинальные бэкапы? Если загрузчик уже открыт, повторного анлока не будет. "
                                        "Разблокировка СТИРАЕТ пользовательские данные.",
                                        "The previous rollback finished. Check the phone and retry unlocking using these original "
                                        "backups? An already unlocked device will not be unlocked again. Unlocking ERASES user data."):
                                self.check_host()
                                self.resume_prepared(restart_recovered=True)
                                return 0
                        self.say("Этот сеанс уже завершён; повторная запись не нужна. Проверьте загрузку Android. "
                                 "Сохраните эту папку с бэкапами. Для другого телефона подключите его и снова запустите программу: "
                                 "будет выбран его отдельный сеанс.",
                                 "This session is already finished; no repeat write is needed. Check Android boot and "
                                 "retain this backup folder. Connect another phone and relaunch to select its separate session.")
                        return 0
                    if phase not in ("prepared", "backup_ready") or self.marker_verified:
                        raise Stop("Unrecognized saved phase. Keep the folder, receipt and both backups; "
                                   "no automatic write is permitted. See README.md#recovery.")
                    if args.recover or args.prepare:
                        self.say("Подготовка уже сохранена, метка ещё не записывалась. "
                                 "Обычный запуск предложит продолжить анлок из этих бэкапов.",
                                 "Preparation is saved; the marker has not been written. "
                                 "A normal launch offers to continue unlocking from these backups.")
                        return 0
                    if not self.ask("Продолжить анлок из сохранённой подготовки? Исходные бэкапы останутся. "
                                    "Разблокировка СТИРАЕТ пользовательские данные; потребуется подтверждение на телефоне.",
                                    "Continue unlocking from the saved preparation? Original backups are retained. "
                                    "Unlocking ERASES user data and requires confirmation on the phone."):
                        return 0
                    self.check_host()
                    self.resume_prepared()
                    return 0
                if self.backup_artifacts():
                    self.check_host()
                    self.adopt_backups()
                    continue
                if args.recover:
                    self.say("В этой папке нет незавершённого сеанса. Восстанавливать нечего.",
                             "There is no unfinished session in this directory. Nothing needs recovery.")
                    return 0
                if args.unlock_prepared:
                    raise Stop("No prepared session exists in this directory. Start normally to create verified backups first.")
                if not self.ask("Подготовить DIAG и бэкапы без анлока?" if args.prepare else
                                "Проверить телефон и начать анлок, если он нужен? Разблокировка СТИРАЕТ пользовательские данные. "
                                "Сохраните нужные файлы и оставайтесь рядом. Уже открытый загрузчик повторно не разблокируется.",
                                "Prepare DIAG and backups without unlocking?" if args.prepare else
                                "Check the phone and unlock if needed? Unlocking ERASES user data. "
                                "Back up required files and stay nearby. An already unlocked bootloader will not be unlocked again."):
                    return 0
                self.check_host()
                self.execute(prepare_only=args.prepare)
                return 0
            except AlreadyUnlocked:
                self.say("Загрузчик уже разблокирован. Запись метки и повторный анлок не нужны; "
                         "проверьте возврат телефона в Android.",
                         "The bootloader is already unlocked. No marker or repeat unlock is needed; "
                         "check that the phone returns to Android.")
                if self.receipt.exists() and not self.marker_possible:
                    self.save_state("already_unlocked", reboot_pending=False)
                return 0
            except (Declined, EOFError):
                self.say("Остановлено по вашему выбору. Все бэкапы сохранены.",
                         "Stopped at your request. All backups are retained.")
                return 0
            except (Stop, OSError, ValueError, KeyboardInterrupt) as error:
                self.record_error(error)
                self.say("Операция не завершена. Бэкапы и журнал не удаляются.",
                         "The operation is unfinished. Backups and the receipt are retained.")
                self.show_error(error)
                if self.marker_possible or self.state.get("reboot_pending"):
                    if self.marker_possible:
                        self.say("Метка могла быть записана. Сначала восстанавливается исходный служебный раздел.",
                                 "The marker may have been written. Restoring the original service partition first.")
                    else:
                        self.say("Исходный раздел уже восстановлен. Осталось завершить перезагрузку; повторной прошивки не будет.",
                                 "The original partition is already restored. Only reboot remains; no repeat flash.")
                    self.guided_recover()
                    return 1
                if isinstance(error, KeyboardInterrupt):
                    return 1
                self.say("В этом запуске запись метки не начиналась. Если проблема с USB — исправьте соединение/драйвер. "
                         "Если повреждены бэкап или журнал — восстановите их из сохранённой копии. "
                         "Не удаляйте файлы и не пересоздавайте SHA. Для помощи сохраните эту папку и balmunlock-last-error.txt.",
                         "No marker write started in this run. Fix USB/driver issues, or restore damaged backup/receipt "
                         "files from a known good copy. Do not delete files or regenerate SHA. "
                         "For assistance retain this folder and balmunlock-last-error.txt.")
                if not self.ask("После исправления повторить проверку сохранённого состояния в этом окне?",
                                "After fixing the cause, recheck the saved state in this window?"):
                    return 1
                if not self.receipt.exists() and not (self.root / STATE_NAME).exists() and not self.session_serial:
                    self.serial = None
                    if self.initial_serial is None:
                        self.env.pop("ANDROID_SERIAL", None)
                    else:
                        self.env["ANDROID_SERIAL"] = self.initial_serial

    def check_host(self):
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(output):
            available = preflight(self.lang)
        self.detail(output.getvalue())
        if self.verbose:
            print(output.getvalue(), end="", flush=True)
        if available:
            self.say("Проверка файлов программы и зависимостей: OK.", "Program files and dependencies: OK.")
            return
        if frozen():
            self.say("Не хватает файлов portable-сборки. Распакуйте весь ZIP, включая _internal и platform-tools, "
                     "сохранив существующие бэкапы и журнал.",
                     "Portable files are missing. Extract the entire ZIP, including _internal and platform-tools, "
                     "while retaining existing backups and the receipt.")
        else:
            self.say("Не найдены зависимости хоста. Проверьте Python/PyUSB/libusb, ADB и fastboot по разделу своей ОС в README.md.",
                     "Host dependencies are missing. Check Python/PyUSB/libusb, ADB and fastboot in your OS section of README.md.")
        raise Stop("Host dependencies unavailable; no device operation started.")


def main():
    language = argparse.ArgumentParser(add_help=False, prog=APP_NAME)
    language.add_argument("--lang", choices=("ru", "en"), default="ru")
    selected_language, _ = language.parse_known_args()
    def help_text(ru, en):
        return ru if selected_language.lang == "ru" else en
    parser = argparse.ArgumentParser(prog=APP_NAME, description=help_text(
        "Пошаговая разблокировка загрузчика BALMUDA Phone. Обычный запуск сам выбирает сеанс телефона.",
        "Guided BALMUDA Phone bootloader unlocking. A normal launch selects the phone's session automatically."))
    parser.add_argument("--lang", choices=("ru", "en"), default="ru", help=help_text("язык сообщений и справки", "message and help language"))
    parser.add_argument("--version", action="version", version=f"{APP_NAME} {VERSION}")
    parser.add_argument("--verbose", action="store_true", help=help_text(
        "показывать технические подробности в консоли; они также сохраняются в balmunlock.log",
        "show technical console output; details are also saved to balmunlock.log"))
    interaction = parser.add_mutually_exclusive_group()
    interaction.add_argument("--guided", action="store_true", help=help_text("включить вопросы также при перенаправленном вводе", "enable guided prompts, also with redirected stdin"))
    interaction.add_argument("--non-interactive", action="store_true", help=help_text("при ошибке завершать работу без предложения повторить", "stop on errors instead of offering guided retries"))
    parser.add_argument("--session", metavar="SERIAL_OR_KEY", help=help_text("выбрать сохранённый сеанс; legacy — старые файлы рядом с программой", "select a saved session; legacy selects older files beside the program"))
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--preflight", action="store_true", help=help_text("подробно проверить зависимости на ПК, без ADB/USB", "detailed host checks only; no ADB/USB"))
    mode.add_argument("--recover", action="store_true", help=help_text("продолжить восстановление исходного раздела без нового анлока", "resume recovery of the original partition without a new unlock"))
    mode.add_argument("--prepare", action="store_true", help=help_text("подготовить DIAG и бэкапы, без метки и анлока", "prepare DIAG and backups without a marker or unlock"))
    mode.add_argument("--unlock-prepared", action="store_true", help=help_text("начать анлок из сохранённой подготовки; стирает данные", "START UNLOCK from saved preparation; erases user data"))
    mode.add_argument("--list-sessions", action="store_true", help=help_text("показать сохранённые сеансы без ADB/USB", "list saved sessions without ADB/USB access"))
    args = parser.parse_args()
    if args.preflight:
        return 0 if preflight(args.lang) else 1
    if args.list_sessions:
        try:
            entries = SessionStore(ROOT).entries()
            if not entries:
                print("Сохранённых сеансов нет." if args.lang == "ru" else "No saved sessions.")
            listing = Workflow(args.lang)
            for entry in entries:
                print(f"{entry.serial or '?'} | {listing.phase_label(entry.phase)} | {entry.key}\n  {entry.root}")
                if entry.error:
                    print(f"  ERROR: {entry.error}")
            return 0
        except (OSError, ValueError) as error:
            print(f"STOP: {error}", file=sys.stderr)
            return 1
    flow = Workflow(args.lang, interactive=not args.non_interactive and (args.guided or sys.stdin.isatty()), verbose=args.verbose)
    flow.introduce()
    if flow.interactive:
        try:
            with session_lock(ROOT / ".unlock.lock"), flow.signals:
                if not flow.route_session(args):
                    return 0
                return flow.guided(args)
        except (Declined, EOFError):
            flow.say("Остановлено по вашему выбору. Бэкапы сохранены.", "Stopped at your request. Backups retained.")
            return 0
        except (Stop, OSError, ValueError, KeyboardInterrupt, EOFError) as error:
            flow.record_error(error)
            flow.say("Не удалось выбрать или открыть сеанс. Причина указана ниже. Сохраните бэкапы; "
                     "проверьте список через --list-sessions. После устранения причины повторите запуск.",
                     "Cannot select or open the session. See the reason below and retain the backups. "
                     "Use --list-sessions to inspect saved sessions, fix the cause and relaunch.")
            flow.show_error(error)
            flow.recovery_notice()
            return 1
    if args.prepare:
        flow.say("Подготовить DIAG и бэкап chkcode, без записи метки и анлока? [yes/N]",
                 "Prepare DIAG and a chkcode backup, without marker writes or unlocking? [yes/N]")
    elif args.recover:
        flow.say("Восстановить chkcode на исходном устройстве из сохранённого сеанса? [yes/N]",
                 "Recover chkcode on the original device using the saved session? [yes/N]")
    else:
        flow.say("Разблокировка Balmuda Phone СТИРАЕТ пользовательские данные. Продолжить? [yes/N]",
                 "Balmuda Phone unlocking ERASES user data. Continue? [yes/N]")
    try:
        if input().strip().lower() != "yes":
            return 0
        with session_lock(ROOT / ".unlock.lock"), flow.signals:
            if not flow.route_session(args):
                return 0
            if args.recover:
                # Fastboot recovery must remain usable without pyusb/ADB.
                cleaned = flow.cleanup_cve()
                if cleaned and not flow.receipt.exists():
                    flow.say("Временные настройки CVE восстановлены, payload удалён.",
                             "Temporary CVE settings restored; payload removed.")
                    return 0
                flow.load_session()
                return 0 if flow.recover() else 1
            flow.check_host()
            try:
                if args.prepare:
                    flow.execute(prepare_only=True)
                elif args.unlock_prepared:
                    flow.resume_prepared()
                else:
                    flow.execute()
            except (Stop, OSError, ValueError, KeyboardInterrupt, EOFError) as error:
                flow.record_error(error)
                flow.show_error(error)
                if flow.marker_possible or flow.state.get("reboot_pending"):
                    try:
                        flow.save_state("error", failed_phase=flow.state.get("phase"), original_error=str(error))
                        restored = flow.recover()
                        flow.say("Откат завершён." if restored else "Восстановление нужно продолжить.",
                                 "Rollback completed." if restored else "Recovery needs completion.")
                    except (Stop, OSError, ValueError, KeyboardInterrupt) as recovery_error:
                        flow.show_error(recovery_error)
                flow.recovery_notice()
                return 1
    except (Stop, OSError, ValueError, KeyboardInterrupt, EOFError) as error:
        flow.record_error(error)
        flow.show_error(error)
        flow.recovery_notice()
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
