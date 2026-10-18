#!/usr/bin/env python3
"""Shared RU/EN unlock workflow. --preflight never invokes ADB or opens USB."""
# Modified 2026-10-18: persistent paths and helper dispatch for the console EXE.
from __future__ import annotations

import argparse
import hashlib
import importlib
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
from runtime_paths import VERSION, frozen, session_root, tool_command

ROOT = session_root()


def recovery_command():
    return ".\\unmuda.exe --recover" if frozen() else "python tools/unlock.py --recover"


class Stop(RuntimeError):
    pass


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
    def __init__(self, lang="ru", interactive=False):
        self.lang = lang
        self.interactive = interactive
        self.serial = None
        self.backup = ROOT / "chkcode_backup.bin"
        self.recovery_backup = ROOT / "chkcode_backup.recovery.bin"
        self.receipt = ROOT / "chkcode_backup.device.json"
        self.marker_possible = False
        self.marker_verified = False
        self.original = None
        self.state = {}
        self.signals = OperationSignals()
        self.env = os.environ.copy()
        self.initial_serial = self.env.get("ANDROID_SERIAL")
        self.env["PYTHONUTF8"] = "1"

    def say(self, ru, en):
        print(ru if self.lang == "ru" else en, flush=True)

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
        return self.interactive and self.ask(ru, en)

    def run(self, args, timeout=15, check=True):
        try:
            result = subprocess.run([str(arg) for arg in args], cwd=ROOT,
                                    env=self.env, capture_output=True, text=True,
                                    encoding="utf-8", errors="replace", timeout=timeout,
                                    **self.signals.child_options())
        except subprocess.TimeoutExpired as error:
            raise Stop(f"Command timed out ({timeout}s): {args[0:3]}") from error
        except OSError as error:
            raise Stop(str(error)) from error
        if check and result.returncode:
            raise Stop(f"Command failed: {args}\n{result.stdout}\n{result.stderr}")
        return result

    def tool(self, name, *args, check=True):
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
        print(result.stdout, end="", flush=True)
        print(result.stderr, end="", file=sys.stderr, flush=True)
        return result

    def cleanup_cve(self):
        with self.signals.defer():
            try:
                cleaned = cleanup_pending(ROOT / STATE_NAME, expected_serial=self.serial,
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
        self.say("Проверка fastboot до записи сервисной метки. Телефон перезагрузится. "
                 "Если нужен драйвер Windows: Zadig → Device → Load Preset Device → "
                 "tools/balmuda-fastboot.cfg → WinUSB (18D1:D00D). Ожидание до 5 минут.",
                 "Checking fastboot before writing the service marker. The phone will reboot. "
                 "If Windows needs a driver: Zadig > Device > Load Preset Device > "
                 "tools/balmuda-fastboot.cfg > WinUSB (18D1:D00D). Waiting up to 5 minutes.")
        self.run(["adb", "-s", self.serial, "reboot", "bootloader"])
        while True:
            try:
                self.wait_fastboot(timeout=300)
                break
            except Stop as error:
                if self.retry("Fastboot пока не обнаружен. Установите WinUSB для 18D1:D00D через "
                              "Zadig → Device → Load Preset Device → tools/balmuda-fastboot.cfg. "
                              "Телефон оставьте в загрузчике. Драйвер установлен — проверить снова?",
                              "Fastboot is not detected yet. Install WinUSB for 18D1:D00D using "
                              "Zadig > Device > Load Preset Device > tools/balmuda-fastboot.cfg. "
                              "Keep the phone in the bootloader. Driver installed — check again?"):
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
                print(f"Return to Android not confirmed: {error}", file=sys.stderr, flush=True)
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
                self.say("DIAG пока недоступен. Оставьте телефон подключённым. В Zadig включите Options → List All Devices, "
                         "выберите BALMUDA_Android (Interface 0), проверьте USB ID 0482:0A9D и установите WinUSB. "
                         "Дождитесь завершения установки драйвера.",
                         "DIAG is not accessible yet. Keep the phone connected. In Zadig enable Options > List All Devices, "
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
        self.say("Включение DIAG. Если понадобится драйвер, программа покажет шаги установки.",
                 "Enabling DIAG. Driver installation steps will appear if needed.")
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
        if (ROOT / STATE_NAME).exists():
            raise Stop("CVE cleanup is pending. Use --recover before starting a new session.")
        self.say("Ожидаю единственное авторизованное ADB-устройство...",
                 "Waiting for exactly one authorized ADB device...")
        self.select_adb()
        identity = {}
        for key in ("ro.product.model", "ro.build.version.incremental",
                    "ro.build.version.security_patch", "ro.build.version.sdk"):
            result = self.run(["adb", "-s", self.serial, "shell", "getprop", key])
            identity[key] = result.stdout.strip()
        validate_identity(identity)
        print(json.dumps({"serial": self.serial, **identity}, ensure_ascii=False), flush=True)
        if not prepare_only:
            self.check_bootloader(identity)
        self.activate_diag()
        self.tool("backup_chkcode.py", "-o", str(self.backup))
        original = self.original = load_backup(self.backup)
        # An independently validated local copy survives loss/corruption of one
        # backup file. Both copies stay on this host; this is not off-host backup.
        save_backup(self.recovery_backup, original)
        # Identity is a host receipt, not proof of firmware compatibility.
        self.state = {"version": 1, "serial": self.serial, **identity,
                      "sha256": hashlib.sha256(original).hexdigest()}
        self.save_state("backup_ready")
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
        if (ROOT / STATE_NAME).exists():
            raise Stop("CVE cleanup is pending. Finish cleanup before unlocking.")
        self.select_adb()
        for key in ("ro.product.model", "ro.build.version.incremental",
                    "ro.build.version.security_patch", "ro.build.version.sdk"):
            result = self.run(["adb", "-s", self.serial, "shell", "getprop", key])
            if result.stdout.strip() != self.state.get(key):
                raise Stop(f"Prepared device identity changed: {key}. No marker write.")
        validate_identity(self.state)
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
        self.say("Проверенный бэкап сохранён. Сейчас будет записана сервисная метка.",
                 "Verified backup saved. The service marker will now be written.")
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
        self.say("Подтвердите разблокировку на экране телефона. Ожидаю до 5 минут...",
                 "Confirm the unlock on the phone. Waiting up to 5 minutes...")
        self.save_state("unlock_requested")
        self.fastboot("flashing", "unlock", timeout=180)
        self.wait_unlocked()
        self.save_state("unlock_verified")
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
        self.say("Fastboot подтвердил восстановление chkcode и принял перезагрузку. "
                 "Загрузку Android проверьте на телефоне.",
                 "Fastboot confirmed chkcode restore and accepted reboot. "
                 "Check Android boot on the phone.")

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
        print(f"Serial: {self.serial}\nBackup: {self.backup}\nSecond copy: {self.recovery_backup}"
              f"\nReceipt: {self.receipt}", flush=True)

    def record_error(self, error):
        path = ROOT / "unmuda-last-error.txt"
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
        validate_identity(identity)
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
                print(str(error), file=sys.stderr, flush=True)
            if not self.ask("Восстановление пока не завершено. Подключите исходный телефон, проверьте кабель "
                            "и драйвер его текущего режима. Повторить только восстановление?",
                            "Recovery is unfinished. Connect the original phone and check the cable and driver "
                            "for its current mode. Retry recovery only?"):
                self.recovery_notice()
                return 1

    def guided(self, args):
        print(f"unmuda {VERSION}", flush=True)
        self.say(f"Рабочая папка: {ROOT}", f"Session directory: {ROOT}")
        terminal = {"reboot_submitted", "rollback_reboot_submitted", "rollback_diag_verified",
                    "recovery_android_online", "recovery_reboot_submitted", "already_unlocked"}
        while True:
            try:
                if (ROOT / STATE_NAME).exists():
                    try:
                        cleanup_state = load_cleanup_state(ROOT / STATE_NAME)
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
                        if not self.ask("Предыдущая операция не завершена. Вернуть исходный chkcode/завершить "
                                        "перезагрузку? Это восстановление, без нового анлока.",
                                        "The earlier operation is unfinished. Restore original chkcode/finish reboot? "
                                        "This is recovery, without starting another unlock."):
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
                                 "Сохраните эту папку с бэкапами. Для другого телефона используйте отдельную распакованную папку.",
                                 "This session is already finished; no repeat write is needed. Check Android boot and "
                                 "retain this backup folder. Use a separate extracted directory for another phone.")
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
                                "Начать анлок? Все пользовательские данные будут СТЁРТЫ. Сохраните нужные файлы "
                                "и оставайтесь рядом для подтверждения на телефоне.",
                                "Prepare DIAG and backups without unlocking?" if args.prepare else
                                "Start unlocking? All user data will be ERASED. Back up required files "
                                "and stay nearby to confirm on the phone."):
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
                print(f"Details: {error}", file=sys.stderr, flush=True)
                if self.marker_possible or self.state.get("reboot_pending"):
                    self.say("Метка могла быть записана. Сначала выполняется восстановление исходного сеанса.",
                             "The marker may have been written. Recovering the original session first.")
                    self.guided_recover()
                    return 1
                if isinstance(error, KeyboardInterrupt):
                    return 1
                self.say("В этом запуске запись метки не начиналась. Если проблема с USB — исправьте соединение/драйвер. "
                         "Если повреждены бэкап или журнал — восстановите их из сохранённой копии. "
                         "Не удаляйте файлы и не пересоздавайте SHA. Для помощи сохраните эту папку и unmuda-last-error.txt.",
                         "No marker write started in this run. Fix USB/driver issues, or restore damaged backup/receipt "
                         "files from a known good copy. Do not delete files or regenerate SHA. "
                         "For assistance retain this folder and unmuda-last-error.txt.")
                if not self.ask("После исправления повторить проверку сохранённого состояния в этом окне?",
                                "After fixing the cause, recheck the saved state in this window?"):
                    return 1
                if not self.receipt.exists() and not (ROOT / STATE_NAME).exists():
                    self.serial = None
                    if self.initial_serial is None:
                        self.env.pop("ANDROID_SERIAL", None)
                    else:
                        self.env["ANDROID_SERIAL"] = self.initial_serial

    def check_host(self):
        if preflight(self.lang):
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
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lang", choices=("ru", "en"), default="ru")
    interaction = parser.add_mutually_exclusive_group()
    interaction.add_argument("--guided", action="store_true", help="enable guided prompts, also with redirected stdin")
    interaction.add_argument("--non-interactive", action="store_true", help="stop on errors instead of offering guided retries")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--preflight", action="store_true", help="host checks only; no ADB/USB")
    mode.add_argument("--recover", action="store_true", help="resume chkcode recovery from the saved device receipt")
    mode.add_argument("--prepare", action="store_true", help="activate DIAG and back up chkcode; stop before marker/unlock")
    mode.add_argument("--unlock-prepared", action="store_true", help="START UNLOCK using the checked prepared session; erases user data")
    args = parser.parse_args()
    if args.preflight:
        return 0 if preflight(args.lang) else 1
    flow = Workflow(args.lang, interactive=not args.non_interactive and (args.guided or sys.stdin.isatty()))
    if flow.interactive:
        try:
            with session_lock(ROOT / ".unlock.lock"), flow.signals:
                return flow.guided(args)
        except (Stop, OSError, ValueError, KeyboardInterrupt, EOFError) as error:
            flow.record_error(error)
            flow.say("Продолжить сейчас нельзя. Закройте другой запущенный экземпляр, проверьте право записи "
                     "в эту папку и сохраните бэкапы. После устранения причины снова запустите программу из той же папки.",
                     "Cannot continue now. Close any other running instance, check write access to this directory "
                     "and retain the backups. Fix the cause and relaunch from the same folder.")
            print(f"Details: {error}", file=sys.stderr, flush=True)
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
            if args.recover:
                # Fastboot recovery must remain usable without pyusb/ADB.
                cleaned = flow.cleanup_cve()
                if cleaned and not flow.receipt.exists():
                    flow.say("Временные настройки CVE восстановлены, payload удалён.",
                             "Temporary CVE settings restored; payload removed.")
                    return 0
                flow.load_session()
                return 0 if flow.recover() else 1
            if not preflight(args.lang):
                return 1
            try:
                if args.prepare:
                    flow.execute(prepare_only=True)
                elif args.unlock_prepared:
                    flow.resume_prepared()
                else:
                    flow.execute()
            except (Stop, OSError, ValueError, KeyboardInterrupt, EOFError) as error:
                print(f"STOP: {error}", file=sys.stderr, flush=True)
                if flow.marker_possible or flow.state.get("reboot_pending"):
                    try:
                        flow.save_state("error", failed_phase=flow.state.get("phase"), original_error=str(error))
                        restored = flow.recover()
                        print("ROLLBACK OK" if restored else "ROLLBACK PENDING", file=sys.stderr, flush=True)
                    except (Stop, OSError, ValueError, KeyboardInterrupt) as recovery_error:
                        print(f"ROLLBACK STOP: {recovery_error}", file=sys.stderr, flush=True)
                flow.recovery_notice()
                return 1
    except (Stop, OSError, ValueError, KeyboardInterrupt, EOFError) as error:
        print(f"STOP: {error}", file=sys.stderr, flush=True)
        flow.recovery_notice()
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
