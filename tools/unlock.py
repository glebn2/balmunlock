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

from chkcode_backup import load_backup, require_new_backup, save_backup
from cve_cleanup import STATE_NAME, CleanupError, cleanup_pending
from device_policy import validate_identity
from operation_signals import OperationSignals
from session_lock import session_lock
from runtime_paths import frozen, session_root, tool_command

ROOT = session_root()


def recovery_command():
    return ".\\unmuda.exe --recover" if frozen() else "python tools/unlock.py --recover"


class Stop(RuntimeError):
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
    def __init__(self, lang="ru"):
        self.lang = lang
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
        self.env["PYTHONUTF8"] = "1"

    def say(self, ru, en):
        print(ru if self.lang == "ru" else en, flush=True)

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

    def tool(self, name, *args):
        # Never kill a partition-writing process on a host-side timer.
        timeout = None if name in ("unlock_chkcode.py", "restore_chkcode.py") else 600
        # libusb opens all WinUSB interfaces of this composite device. The
        # Windows ADB server can hold MI_02 and make libusb_open fail for DIAG
        # with ACCESS (-3), even though MI_00 has its own valid WinUSB driver.
        # Keep this host-side coordination out of the low-level USB library.
        exclusive_diag = os.name == "nt" and name in (
            "kdiag_shell.py", "backup_chkcode.py", "unlock_chkcode.py", "restore_chkcode.py")
        if exclusive_diag:
            self.run(["adb", "kill-server"])
        try:
            result = self.run(tool_command(name, *args, root=ROOT), timeout=timeout)
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
        while time.monotonic() < deadline:
            result = self.run(["adb", "devices"], timeout=5)
            rows = devices(result.stdout)
            if len(rows) > 1:
                raise Stop("Connect exactly one Android device, including offline/unauthorized devices.")
            if rows:
                if len(rows[0]) != 2 or rows[0][1] != "device":
                    raise Stop("ADB device is not authorized/online. Authorize it and retry before writing.")
                self.serial = rows[0][0]
                if self.env.get("ANDROID_SERIAL") not in (None, "", self.serial):
                    raise Stop("ANDROID_SERIAL does not match the attached device.")
                self.env["ANDROID_SERIAL"] = self.serial
                return
            time.sleep(1)
        raise Stop("Timed out waiting for an authorized ADB device. No chkcode write attempted.")

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
        return bool(re.search(r"is-userspace:\s*yes\b", result.stdout + result.stderr, re.I))

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
                    if self.is_userspace():
                        raise Stop("Automatic rollback refuses fastbootd; keep the current state.")
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
        try:
            self.wait_fastboot(timeout=300)
        except Stop as error:
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
                raise Stop("The bootloader is already unlocked. No unlock or marker write needed.")
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
        self.tool("cve31317.py")
        self.wait_prop("sys.usb.config", "diag,modem,adb")
        self.tool("kdiag_shell.py", "setprop vendor.kc.diag.status start")
        self.wait_prop("init.svc.fs_sys_call_diag", "running")
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

    def resume_prepared(self):
        """Explicit future unlock entry; never recreate or replace originals."""
        self.load_session()
        if (self.state.get("phase") != "prepared" or self.marker_possible or
                self.marker_verified or self.state.get("reboot_pending")):
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
        if self.is_userspace():
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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lang", choices=("ru", "en"), default="ru")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--preflight", action="store_true", help="host checks only; no ADB/USB")
    mode.add_argument("--recover", action="store_true", help="resume chkcode recovery from the saved device receipt")
    mode.add_argument("--prepare", action="store_true", help="activate DIAG and back up chkcode; stop before marker/unlock")
    mode.add_argument("--unlock-prepared", action="store_true", help="START UNLOCK using the checked prepared session; erases user data")
    args = parser.parse_args()
    if args.preflight:
        return 0 if preflight(args.lang) else 1
    flow = Workflow(args.lang)
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
