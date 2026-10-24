"""Install the pinned TWRP through a prepared, already unlocked bootloader.

This workflow never invokes unlock helpers, DIAG, root preparation, or AVB flags.
All partition writes are explicit recovery_b writes; failures do not auto-restore.
"""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
import uuid

from operation_signals import OperationSignals
from recovery_image import (IMAGE_NAME, IMAGE_SHA256, IMAGE_SIZE, STOCK_BUILD,
                            STOCK_FINGERPRINT, TWRP_VERSION, TWRP_MODEL, TWRP_FINGERPRINT, VENDOR_FINGERPRINT,
                            file_hash, image_path, verify_image)
from session_lock import session_lock
from session_store import SessionStore, directory_key, regular_path, valid_serial
from ui_language import error_message

PARTITION = "recovery_b"
BLOCK = "/dev/block/by-name/recovery_b"


class RecoveryError(RuntimeError):
    def __init__(self, en, ru=None):
        super().__init__(en)
        self.ru = ru


def single_value(output, name):
    values = set(re.findall(r"^\s*(?:\(bootloader\)\s*)?" + re.escape(name)
                            + r":\s*(\S+)\s*$", output, re.M))
    if len(values) != 1:
        raise RecoveryError(f"Unknown/conflicting {name}", f"Неоднозначный ответ {name}")
    return values.pop()


def parse_properties(output):
    result = {}
    for key, value in re.findall(r"^\[([^\]]+)\]: \[(.*)\]$", output, re.M):
        if key in result and result[key] != value:
            raise RecoveryError(f"Conflicting property: {key}", f"Противоречивое свойство: {key}")
        result[key] = value
    return result


class RecoveryInstaller:
    def __init__(self, root, lang="ru", interactive=True, verbose=False):
        self.root = Path(root).resolve()
        self.lang, self.interactive, self.verbose = lang, interactive, verbose
        self.serial = None
        self.folder = None
        self.state = {}
        self.events = []
        self.signals = OperationSignals(lang)
        self.env = os.environ.copy()

    def say(self, ru, en):
        print(ru if self.lang == "ru" else en, flush=True)

    def fail(self, ru, en):
        raise RecoveryError(en, ru)

    def log(self, text):
        record = f"{datetime.now(timezone.utc).isoformat()} | {text}\n"
        if self.folder is None:
            self.events.append(record)
        else:
            with (self.folder / "recovery-install.log").open("a", encoding="utf-8") as stream:
                stream.write(record)

    def save(self, phase, **fields):
        self.state.update(fields, phase=phase, serial=self.serial)
        temporary = self.folder / "state.tmp"
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(self.state, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, self.folder / "state.json")

    def run(self, args, timeout=15, check=True):
        self.signals.checkpoint()
        try:
            result = subprocess.run(list(map(str, args)), cwd=self.root, env=self.env,
                                    capture_output=True, text=True, encoding="utf-8",
                                    errors="replace", timeout=timeout,
                                    **self.signals.child_options())
        except (OSError, subprocess.TimeoutExpired) as error:
            raise RecoveryError(str(error), "Не удалось выполнить команду. Проверьте USB, файлы программы и журнал.") from error
        self.log(f"COMMAND {list(map(str, args))}\nEXIT {result.returncode}\n{result.stdout}\n{result.stderr}")
        if self.verbose:
            print(result.stdout + result.stderr, end="", flush=True)
        if check and result.returncode:
            self.fail("Команда завершилась с ошибкой. Подробности в журнале.", "Command failed. See the operation log.")
        return result

    def adb(self, *args, **kwargs):
        return self.run(["adb", "-s", self.serial, *args], **kwargs)

    def fb(self, *args, **kwargs):
        return self.run(["fastboot", "-s", self.serial, *args], **kwargs)

    def connections(self):
        found = {}
        for program in ("adb", "fastboot"):
            output = self.run([program, "devices"], timeout=10).stdout
            for line in output.splitlines():
                if not line.strip() or line.startswith(("List of devices", "*")):
                    continue
                fields = line.split()
                if len(fields) < 2 or not valid_serial(fields[0]):
                    self.fail("Не удалось разобрать список устройств.", "Invalid device-list response.")
                found.setdefault(fields[0], set()).add(fields[1])
        if len(found) > 1 or (found and self.serial is not None and set(found) != {self.serial}):
            self.fail("Подключён другой телефон или несколько устройств. Остановлено.",
                      "Another phone or multiple devices detected. Stopped.")
        return found

    def wait(self, mode, timeout):
        deadline = time.monotonic() + timeout
        while True:
            found = self.connections()
            if found.get(self.serial) == {mode}:
                return
            if time.monotonic() >= deadline:
                self.fail(f"Телефон не появился в режиме {mode}. Проверьте USB и драйвер.",
                          f"Timed out waiting for {mode}. Check USB and the driver.")
            time.sleep(2)

    def properties(self):
        return parse_properties(self.adb("shell", "getprop").stdout)

    def android_identity(self):
        props = self.properties()
        expected = {"ro.boot.serialno": self.serial, "ro.product.model": "A101BM",
                    "ro.product.vendor.model": "A101BM", "ro.boot.slot_suffix": "_b",
                    "ro.build.version.sdk": "31", "ro.build.version.incremental": STOCK_BUILD,
                    "ro.build.fingerprint": STOCK_FINGERPRINT,
                    "ro.vendor.build.fingerprint": VENDOR_FINGERPRINT}
        for key, value in expected.items():
            if props.get(key) != value:
                self.fail(f"Не подходит {key}: {props.get(key)!r}. Нужен штатный A101BM 1.280, слот b.",
                          f"Identity mismatch for {key}: {props.get(key)!r}. Requires stock A101BM 1.280, slot b.")
        return props

    def clean_unlock_session(self):
        terminal = {"reboot_submitted", "already_unlocked", "rollback_reboot_submitted",
                    "rollback_diag_verified", "recovery_android_online", "recovery_reboot_submitted"}
        for entry in SessionStore(self.root).entries():
            if entry.serial not in (None, self.serial):
                continue
            receipt = entry.root / "chkcode_backup.device.json"
            if entry.error or (entry.root / "cve_cleanup.device.json").exists():
                self.fail("Сначала завершите прежний сеанс через пункт разблокировки/восстановления.",
                          "Finish the previous unlock/recovery session first.")
            if receipt.exists():
                state = json.loads(receipt.read_text(encoding="utf-8"))
                if (state.get("marker_possible") is not False or state.get("reboot_pending", False) is not False
                        or state.get("phase") not in terminal):
                    self.fail("Прежний сеанс требует проверки. Установка TWRP не начинается.",
                              "The previous session needs attention. TWRP installation will not start.")

    def getvar(self, name):
        reply = self.fb("getvar", name)
        return single_value(reply.stdout + "\n" + reply.stderr, name)

    def check_bootloader(self):
        self.wait("fastboot", 300)
        if self.getvar("is-userspace") != "no":
            self.fail("Нужен загрузчик, а не fastbootd.", "Bootloader required, not fastbootd.")
        result = self.fb("getvar", "unlocked", check=False)
        if result.returncode == 0:
            value = single_value(result.stdout + "\n" + result.stderr, "unlocked")
        else:
            result = self.fb("oem", "device-info")
            value = single_value(result.stdout + "\n" + result.stderr, "Device unlocked")
        if value.lower() not in ("yes", "true"):
            self.fail("Установка доступна только с разблокированным загрузчиком. Анлок не запускается.",
                      "Recovery requires an unlocked bootloader. Unlocking will not be started.")
        expected = {"product": "lito", "current-slot": "b", "slot-count": "2",
                    "snapshot-update-status": "none"}
        for key, value in expected.items():
            if self.getvar(key) != value:
                self.fail(f"Не подходит состояние загрузчика: {key}.", f"Unsupported bootloader state: {key}.")
        try:
            size = int(self.getvar("partition-size:" + PARTITION), 16)
        except ValueError as error:
            raise RecoveryError("Invalid recovery partition size", "Некорректный размер раздела recovery") from error
        if size != IMAGE_SIZE:
            self.fail("Размер recovery_b отличается от ожидаемого.", "Unexpected recovery_b size.")

    def check_twrp(self):
        self.wait("recovery", 120)
        props = self.properties()
        for key, value in {"ro.boot.serialno": self.serial, "ro.product.vendor.model": TWRP_MODEL,
                           "ro.vendor.build.fingerprint": TWRP_FINGERPRINT,
                           "ro.boot.slot_suffix": "_b"}.items():
            if props.get(key) != value:
                self.fail(f"Recovery: не совпадает {key}.", f"Recovery identity mismatch: {key}.")
        version = props.get("ro.twrp.version", "")
        if version != TWRP_VERSION and not version.startswith(TWRP_VERSION + "-"):
            self.fail("Не подтверждена ожидаемая версия TWRP.", "Expected TWRP version not confirmed.")
        if self.adb("shell", "id", "-u").stdout.strip() != "0":
            self.fail("В TWRP нет доступа для чтения раздела. Новые права не запрашиваются.",
                      "TWRP cannot read the partition. No privilege preparation will be attempted.")

    def block_hash(self):
        output = self.adb("shell", "sha256sum", BLOCK, timeout=120).stdout
        match = re.fullmatch(r"([0-9a-fA-F]{64})\s+" + re.escape(BLOCK) + r"\s*", output)
        if match is None:
            self.fail("Не удалось прочитать хеш recovery_b.", "Cannot read recovery_b SHA-256.")
        return match[1].lower()

    def backup(self):
        if self.adb("shell", "blockdev", "--getsize64", BLOCK).stdout.strip() != str(IMAGE_SIZE):
            self.fail("Не подтверждён размер исходного recovery_b.", "Original recovery_b size not confirmed.")
        before = self.block_hash()
        temporary = "/tmp/balmunlock-recovery-" + uuid.uuid4().hex + ".img"
        self.adb("shell", f"dd if={BLOCK} of={temporary} bs=1048576 count=96", timeout=120)
        staged = self.adb("shell", "sha256sum", temporary, timeout=120).stdout.split()
        if staged != [before, temporary] or self.block_hash() != before:
            self.fail("Исходный recovery изменился или прочитан с ошибкой. Запись запрещена.",
                      "Original recovery changed or was read incorrectly. No flash allowed.")
        target = self.folder / "recovery_b.before-install.img.part"
        self.adb("pull", temporary, str(target), timeout=180)
        if target.stat().st_size != IMAGE_SIZE or file_hash(target) != before:
            self.fail("Бэкап на ПК не совпадает с разделом. Запись запрещена.",
                      "Host backup does not match the partition. No flash allowed.")
        backup = target.with_suffix("")
        target.rename(backup)
        (backup.with_suffix(".img.sha256")).write_text(f"{before}  {backup.name}\n", encoding="ascii")
        self.save("backup_verified", original_sha256=before, backup=backup.name,
                  original_was_pinned_twrp=(before == IMAGE_SHA256))
        self.adb("shell", "rm", temporary)
        return before

    def create_folder(self):
        base = self.root / "recovery-sessions"
        regular_path(base)
        base.mkdir(exist_ok=True)
        owner = base / directory_key(self.serial)
        regular_path(owner)
        owner.mkdir(exist_ok=True)
        self.folder = owner / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:8])
        self.folder.mkdir()
        (self.folder / "recovery-install.log").write_text("".join(self.events), encoding="utf-8")
        self.save("confirmed", image_sha256=IMAGE_SHA256, image_bytes=IMAGE_SIZE,
                  partition=PARTITION, stock_build=STOCK_BUILD)

    def select_android(self):
        """Read-only admission, also used before the combined workflow can wipe."""
        if any(shutil.which(tool) is None for tool in ("adb", "fastboot")):
            self.fail("Не найдены ADB/fastboot. Проверьте файлы программы.", "ADB/fastboot not found. Check the app files.")
        found = self.connections()
        if len(found) != 1 or next(iter(found.values())) != {"device"}:
            self.fail("Начните из штатного Android с разрешённой отладкой по USB.",
                      "Start from stock Android with authorized USB debugging.")
        self.serial = next(iter(found))
        if self.env.get("ANDROID_SERIAL") not in (None, "", self.serial):
            self.fail("ANDROID_SERIAL указывает на другой телефон.", "ANDROID_SERIAL selects another phone.")
        self.env["ANDROID_SERIAL"] = self.serial
        if self.android_identity().get("sys.boot_completed") != "1":
            self.fail("Дождитесь полной загрузки штатного Android.", "Wait for stock Android to finish booting.")

    def execute(self, override=None):
        # No device discovery before the artifact passes its offline checks.
        try:
            source = verify_image(image_path(self.root, override))
        except FileNotFoundError as error:
            self.fail(f"Не найден {IMAGE_NAME}. Положите проверенный образ в папку recovery рядом с программой.",
                      f"Missing {IMAGE_NAME}. Place the verified image in the recovery directory beside the app.")
        self.select_android()
        self.clean_unlock_session()
        self.say("Установка TWRP: A101BM 1.280, слот b. Сохраню текущий recovery на ПК, затем заменю только recovery_b. "
                 "Сохраните важные данные отдельно. Продолжить? [yes/N]",
                 "Install TWRP: A101BM 1.280, slot b. Back up the current recovery, then replace recovery_b only. "
                 "Keep a separate copy of important data. Continue? [yes/N]")
        if input().strip().lower() != "yes":
            return 0
        self.create_folder()
        self.say(f"Бэкап и журнал: {self.folder}", f"Backup and log: {self.folder}")
        # Use an immutable per-run copy, so changing the source during the round-trip cannot change what is flashed.
        image = self.folder / IMAGE_NAME
        shutil.copyfile(source, image)
        verify_image(image)
        self.say("Проверяю разблокировку и соединение с загрузчиком. При необходимости установите драйвер fastboot "
                 "18D1:D00D (Windows: WinUSB, preset tools/balmuda-fastboot.cfg). Ожидание до 5 минут.",
                 "Checking the unlocked bootloader. If needed, install the 18D1:D00D fastboot driver "
                 "(Windows: WinUSB, tools/balmuda-fastboot.cfg preset). Waiting up to 5 minutes.")
        self.save("bootloader_requested")
        self.adb("reboot", "bootloader")
        self.check_bootloader()
        self.say("Временно загружаю TWRP для сохранения recovery. При вопросе на экране выберите Keep Read Only.",
                 "Temporarily booting TWRP to back up recovery. Select Keep Read Only if prompted on the phone.")
        self.save("ram_boot_requested")
        self.fb("boot", str(image), timeout=120)
        self.check_twrp()
        original = self.backup()
        if original != IMAGE_SHA256:
            self.adb("reboot", "bootloader")
            self.check_bootloader()
            verify_image(image)
            # The global lock, no timer, and deferred signals cover the writer AND the durable receipt.
            self.say("Оригинал сохранён. Записываю recovery_b; не отключайте кабель.",
                     "Original backed up. Writing recovery_b; do not disconnect USB.")
            self.signals.checkpoint()
            with self.signals.defer():
                self.save("flash_started", flash_may_have_changed=True)
                self.fb("flash", PARTITION, str(image), timeout=None)
                self.save("flash_command_completed")
            self.signals.checkpoint()
            self.fb("reboot", "recovery")
            self.check_twrp()
        else:
            self.say("Этот TWRP уже установлен; повторной записи не будет.",
                     "This TWRP is already installed; no repeat flash.")
        if self.block_hash() != IMAGE_SHA256:
            self.fail("Хеш recovery_b после установки не совпал. Не продолжайте установку.",
                      "Installed recovery_b hash mismatch. Stop here.")
        self.save("partition_verified", installed_sha256=IMAGE_SHA256)
        self.say("Раздел проверен. Проверяю возврат в Android...", "Partition verified. Checking Android return...")
        self.adb("reboot")
        self.wait("device", 180)
        deadline = time.monotonic() + 180
        while self.android_identity().get("sys.boot_completed") != "1":
            if time.monotonic() >= deadline:
                self.fail("Возврат Android не подтверждён. Бэкап и журнал сохранены.",
                          "Android return not confirmed. Backup and log retained.")
            time.sleep(2)
        self.save("completed", android_boot_completed=True)
        self.say("TWRP установлен и проверен. Телефон вернулся в Android. Бэкап recovery сохранён.",
                 "TWRP installed and verified. Phone returned to Android. Recovery backup retained.")
        return 0


def install(root, lang="ru", interactive=True, verbose=False, image=None):
    flow = RecoveryInstaller(root, lang, interactive, verbose)
    try:
        with session_lock(Path(root) / ".unlock.lock"), flow.signals:
            return flow.execute(image)
    except (OSError, ValueError, RecoveryError, KeyboardInterrupt, EOFError) as error:
        message = error_message(error, lang)
        # Never turn a failure into an automatic second write or restore.
        try:
            if flow.folder is not None:
                flow.save("stopped", stopped_phase=flow.state.get("phase"), error=message, technical_error=str(error))
                flow.log(f"STOP {type(error).__name__}: {error}")
        except OSError:
            pass
        flow.say(f"Остановлено: {message}", f"Stopped: {message}")
        if verbose:
            print(str(error), flush=True)
        if flow.folder:
            flow.say(f"Бэкап, образ и журнал: {flow.folder}. Автоматического отката или повторной записи нет.",
                     f"Backup, image and log: {flow.folder}. No automatic restore or repeat flash.")
        return 1
