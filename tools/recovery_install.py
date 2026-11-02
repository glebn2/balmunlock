"""Install the selected TWRP through a prepared, already unlocked bootloader.

This workflow never invokes unlock helpers, DIAG, root preparation, or AVB flags.
Only the selected recovery partition is written. An explicit slot choice can
change the active boot slot; there is no automatic restore or second flash.
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
from recovery_image import (IMAGE_SHA256, IMAGE_SIZE, STOCK_IDENTITIES,
                            file_hash, image_path, inspect_image)
from session_lock import session_lock
from session_store import SessionStore, directory_key, regular_path, valid_serial
from ui_language import error_message

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
        self.slot = None
        self.initial_slot = None
        self.slot_switch_pending = False
        self.both_slots = False
        self.device_model = None
        self.android_build = None
        self.android_details = {}
        self.is_stock = False
        self.image_hash = None
        self.source_name = None
        self.folder = None
        self.state = {}
        self.events = []
        self.signals = OperationSignals(lang)
        self.env = os.environ.copy()

    def say(self, ru, en):
        print("\n" + (ru if self.lang == "ru" else en), flush=True)

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

    def connections(self, waiting=False):
        found = {}
        for program in ("adb", "fastboot"):
            output = self.run([program, "devices"], timeout=10).stdout
            for line in output.splitlines():
                if not line.strip() or line.startswith(("List of devices", "*")):
                    continue
                fields = line.split()
                # Windows fastboot may still enumerate an unaddressable USB
                # handle while the selected phone switches to recovery.
                if (program == "fastboot" and len(fields) == 2
                        and fields[1] == "fastboot" and re.fullmatch(r"\?+", fields[0])):
                    if not waiting or self.serial is None:
                        self.fail("Fastboot пока не прочитал серийный номер устройства. "
                                  "Дождитесь подключения Android и повторите запуск.",
                                  "Fastboot cannot read the device serial yet. "
                                  "Wait for Android to connect and start again.")
                    self.log("Waiting for USB re-enumeration; unidentified fastboot handle is not a device identity.")
                    continue
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
            found = self.connections(waiting=True)
            if found.get(self.serial) == {mode}:
                return
            if time.monotonic() >= deadline:
                self.fail(f"Телефон не появился в режиме {mode}. Проверьте USB и драйвер.",
                          f"Timed out waiting for {mode}. Check USB and the driver.")
            time.sleep(2)

    def properties(self):
        return parse_properties(self.adb("shell", "getprop").stdout)

    def bind_slot(self, suffix):
        if suffix not in ("_a", "_b"):
            self.fail("Не удалось определить активный слот a/b.", "Cannot determine the active a/b slot.")
        slot = suffix[1:]
        if self.slot is not None and slot != self.slot:
            self.fail("Активный слот изменился в ходе установки. Начните новый запуск из Android.",
                      "Active slot changed during installation. Start a new run from Android.")
        self.slot = slot
        if self.initial_slot is None:
            self.initial_slot = slot

    @property
    def partition(self):
        if self.slot not in ("a", "b"):
            raise RecoveryError("Recovery slot has not been selected", "Слот recovery не выбран")
        return f"recovery_{self.slot}"

    @property
    def block(self):
        return "/dev/block/by-name/" + self.partition

    @property
    def partitions(self):
        if self.both_slots:
            # Update the inactive recovery first, keeping the active one until last.
            other = "b" if self.slot == "a" else "a"
            return ["recovery_" + other, self.partition]
        return [self.partition]

    def partition_block(self, partition):
        if partition not in self.partitions:
            self.fail("Раздел не входит в подтверждённый выбор recovery.",
                      "Partition is outside the confirmed recovery selection.")
        return "/dev/block/by-name/" + partition

    def save_partition(self, partition, phase, **fields):
        self.partition_block(partition)
        records = self.state.setdefault("partitions", {})
        records.setdefault(partition, {}).update(fields, phase=phase)
        if fields.get("flash_may_have_changed"):
            self.state["flash_may_have_changed"] = True
        # Retain the 0.7.0 single-partition receipt fields for manual restoration.
        self.save(phase, current_partition=partition, **(fields if not self.both_slots else {}))

    def android_identity(self, require_stock=False):
        props = self.properties()
        # Custom ROM branding and SDK do not identify the physical device.
        model = (props.get("ro.product.vendor.device") or props.get("ro.product.vendor.model")
                 or props.get("ro.product.device") or props.get("ro.product.model"))
        if model not in STOCK_IDENTITIES:
            self.fail("Нужен BALMUDA Phone A101BM или X01A.", "BALMUDA Phone A101BM or X01A required.")
        platform = props.get("ro.board.platform") or props.get("ro.product.board")
        if platform != "lito":
            self.fail("Не подтверждена платформа BALMUDA Phone (lito).", "BALMUDA Phone platform (lito) not confirmed.")
        if self.device_model is not None and model != self.device_model:
            self.fail("Модель телефона изменилась в ходе установки.", "Phone model changed during installation.")
        for key, value in {"ro.boot.serialno": self.serial}.items():
            if props.get(key) != value:
                self.fail(f"Не совпадает {key}: {props.get(key)!r}.",
                          f"Device identity mismatch for {key}: {props.get(key)!r}.")
        self.bind_slot(props.get("ro.boot.slot_suffix"))
        is_stock = all(props.get(key) == value for key, value in STOCK_IDENTITIES[model].items())
        if require_stock and not is_stock:
            self.fail("Для разблокировки нужен штатный Android 12 / 1.280. "
                      "Для установки TWRP на кастомном Android выберите пункт 2.",
                      "Unlocking requires stock Android 12 / 1.280. "
                      "Select option 2 to install TWRP on custom Android.")
        self.device_model = model
        self.android_build = props.get("ro.build.version.incremental", "")
        self.is_stock = is_stock
        self.android_details = {
            "model": model, "slot": self.slot, "system_model": props.get("ro.product.model", ""),
            "vendor_model": props.get("ro.product.vendor.model", ""),
            "vendor_device": props.get("ro.product.vendor.device", ""),
            "android_release": props.get("ro.build.version.release", ""),
            "android_sdk": props.get("ro.build.version.sdk", ""),
            "android_build": self.android_build,
            "android_fingerprint": props.get("ro.build.fingerprint", ""),
            "vendor_fingerprint": props.get("ro.vendor.build.fingerprint", ""),
        }
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

    def check_bootloader(self, allow_slot_switch=False):
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
        expected_slot = self.initial_slot if allow_slot_switch and self.slot_switch_pending else self.slot
        expected = {"product": "lito", "current-slot": expected_slot, "slot-count": "2",
                    "snapshot-update-status": "none"}
        for key, value in expected.items():
            if self.getvar(key) != value:
                self.fail(f"Не подходит состояние загрузчика: {key}.", f"Unsupported bootloader state: {key}.")
        for partition in self.partitions:
            try:
                size = int(self.getvar("partition-size:" + partition), 16)
            except ValueError as error:
                raise RecoveryError("Invalid recovery partition size", "Некорректный размер раздела recovery") from error
            if size != IMAGE_SIZE:
                self.fail(f"Размер {partition} отличается от ожидаемого.", f"Unexpected {partition} size.")
        if allow_slot_switch and self.slot_switch_pending:
            self.say(f"Переключаю активный слот {self.initial_slot} → {self.slot}.",
                     f"Switching the active slot {self.initial_slot} -> {self.slot}.")
            with self.signals.defer():
                self.save("slot_switch_started", slot_switch_may_have_changed=True)
                self.fb("--set-active=" + self.slot)
                if self.getvar("current-slot") != self.slot:
                    self.fail("Загрузчик не подтвердил выбранный слот. Установка остановлена.",
                              "Bootloader did not confirm the selected slot. Installation stopped.")
                self.slot_switch_pending = False
                self.save("slot_switch_verified", active_slot=self.slot)
            self.signals.checkpoint()

    def check_twrp(self):
        self.wait("recovery", 120)
        props = self.properties()
        for key, value in {"ro.boot.serialno": self.serial, "ro.boot.slot_suffix": "_" + self.slot}.items():
            if props.get(key) != value:
                self.fail(f"Recovery: не совпадает {key}.", f"Recovery identity mismatch: {key}.")
        version = props.get("ro.twrp.version", "")
        if not version.strip():
            self.fail("TWRP не сообщил свою версию.", "TWRP did not report its version.")
        if self.adb("shell", "id", "-u").stdout.strip() != "0":
            self.fail("В TWRP нет доступа для чтения раздела. Новые права не запрашиваются.",
                      "TWRP cannot read the partition. No privilege preparation will be attempted.")
        self.state.update(twrp_version=version, twrp_fingerprint=props.get("ro.vendor.build.fingerprint", ""))
        self.log(f"TWRP version: {version}; fingerprint: {props.get('ro.vendor.build.fingerprint', '')}")

    def check_selected_image(self, path):
        inspect_image(path)
        if file_hash(path) != self.image_hash:
            self.fail("Выбранный образ изменился после подтверждения. Установка остановлена.",
                      "Selected image changed after confirmation. Installation stopped.")

    def block_hash(self, partition=None):
        partition = partition or self.partition
        block = self.partition_block(partition)
        output = self.adb("shell", "sha256sum", block, timeout=120).stdout
        match = re.fullmatch(r"([0-9a-fA-F]{64})\s+" + re.escape(block) + r"\s*", output)
        if match is None:
            self.fail(f"Не удалось прочитать хеш {partition}.", f"Cannot read {partition} SHA-256.")
        return match[1].lower()

    def backup(self, partition=None):
        partition = partition or self.partition
        block = self.partition_block(partition)
        if self.adb("shell", "blockdev", "--getsize64", block).stdout.strip() != str(IMAGE_SIZE):
            self.fail(f"Не подтверждён размер исходного {partition}.", f"Original {partition} size not confirmed.")
        before = self.block_hash(partition)
        temporary = "/tmp/balmunlock-recovery-" + uuid.uuid4().hex + ".img"
        self.adb("shell", f"dd if={block} of={temporary} bs=1048576 count=96", timeout=120)
        staged = self.adb("shell", "sha256sum", temporary, timeout=120).stdout.split()
        if staged != [before, temporary] or self.block_hash(partition) != before:
            self.fail("Исходный recovery изменился или прочитан с ошибкой. Запись запрещена.",
                      "Original recovery changed or was read incorrectly. No flash allowed.")
        target = self.folder / f"{partition}.before-install.img.part"
        self.adb("pull", temporary, str(target), timeout=180)
        if target.stat().st_size != IMAGE_SIZE or file_hash(target) != before:
            self.fail("Бэкап на ПК не совпадает с разделом. Запись запрещена.",
                      "Host backup does not match the partition. No flash allowed.")
        backup = target.with_suffix("")
        target.rename(backup)
        (backup.with_suffix(".img.sha256")).write_text(f"{before}  {backup.name}\n", encoding="ascii")
        self.save_partition(partition, "backup_verified", original_sha256=before, backup=backup.name,
                  original_bytes=IMAGE_SIZE,
                  original_was_pinned_twrp=(before == IMAGE_SHA256),
                  original_matches_selected_image=(before == self.image_hash))
        self.adb("shell", "rm", temporary)
        return before

    def check_backups(self):
        for partition in self.partitions:
            record = self.state["partitions"][partition]
            path = self.folder / record["backup"]
            if path.stat().st_size != IMAGE_SIZE or file_hash(path) != record["original_sha256"]:
                self.fail(f"Бэкап {partition} на ПК изменился. Запись запрещена.",
                          f"The {partition} host backup changed. No flash allowed.")

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
        self.save("confirmed", image_sha256=self.image_hash, image_bytes=IMAGE_SIZE,
                  source_filename=self.source_name, image_file="recovery.img",
                  bundled_image=(self.image_hash == IMAGE_SHA256),
                  initial_slot=self.initial_slot, slot_switch_requested=self.slot_switch_pending,
                  target_mode="both" if self.both_slots else "single", target_partitions=self.partitions,
                  **({} if self.both_slots else {"partition": self.partition, "block_device": self.block}),
                  **self.android_details,
                  **({"stock_model": self.device_model, "stock_build": self.android_build} if self.is_stock else {}))

    def select_android(self, require_stock=False):
        """Read-only admission, also used before the combined workflow can wipe."""
        if any(shutil.which(tool) is None for tool in ("adb", "fastboot")):
            self.fail("Не найдены ADB/fastboot. Проверьте файлы программы.", "ADB/fastboot not found. Check the app files.")
        found = self.connections()
        if len(found) != 1 or next(iter(found.values())) != {"device"}:
            self.fail("Начните из работающего Android с разрешённой отладкой по USB.",
                      "Start from running Android with authorized USB debugging.")
        self.serial = next(iter(found))
        if self.env.get("ANDROID_SERIAL") not in (None, "", self.serial):
            self.fail("ANDROID_SERIAL указывает на другой телефон.", "ANDROID_SERIAL selects another phone.")
        self.env["ANDROID_SERIAL"] = self.serial
        if self.android_identity(require_stock=require_stock).get("sys.boot_completed") != "1":
            self.fail("Дождитесь полной загрузки Android.", "Wait for Android to finish booting.")

    def execute(self, override=None, expected_sha256=None):
        # No device discovery before the artifact passes its offline checks.
        try:
            source = inspect_image(image_path(self.root, override))
        except FileNotFoundError as error:
            self.fail("Не найден образ TWRP. Положите один .img в папку recovery рядом с программой.",
                      "Missing TWRP image. Place one .img in the recovery directory beside the app.")
        self.image_hash = file_hash(source)
        self.source_name = source.name
        if expected_sha256 is not None and self.image_hash != expected_sha256:
            self.fail("Образ TWRP изменился во время анлока. Установка остановлена.",
                      "TWRP image changed during unlocking. Installation stopped.")
        self.select_android()
        self.clean_unlock_session()
        self.say(f"Образ: {source.name}\nSHA-256: {self.image_hash}",
                 f"Image: {source.name}\nSHA-256: {self.image_hash}")
        if self.image_hash != IMAGE_SHA256:
            self.say("Внимание: этот образ отличается от комплектной сборки TWRP. "
                     "Подтверждение ниже разрешает установку выбранного файла.",
                     "Warning: this image differs from the bundled TWRP. "
                     "Confirmation below authorizes installing the selected file.")
        android_label = self.android_details["android_release"] or "SDK " + self.android_details["android_sdk"]
        while True:
            if self.both_slots:
                self.say(f"Установка TWRP: {self.device_model}, Android {android_label}, оба recovery: recovery_a и recovery_b. "
                         f"Сначала сохраню оба оригинала на ПК. Активный слот {self.slot} не изменится. Сохраните важные данные отдельно.",
                         f"Install TWRP: {self.device_model}, Android {android_label}, both recovery_a and recovery_b. "
                         f"Back up both originals before flashing. Active slot {self.slot} stays unchanged. Keep important data separately.")
            else:
                self.say(f"Установка TWRP: {self.device_model}, Android {android_label}, слот {self.slot}. "
                         f"Сохраню recovery_{self.slot} на ПК, затем заменю только {self.partition}. Сохраните важные данные отдельно.",
                         f"Install TWRP: {self.device_model}, Android {android_label}, slot {self.slot}. "
                         f"Back up recovery_{self.slot} to the PC, then replace {self.partition} only. Keep important data separately.")
            if self.slot != self.initial_slot:
                self.say(f"Активный слот будет переключён с {self.initial_slot} на {self.slot}. "
                         "Это также меняет слот следующей загрузки Android; его работоспособность программа не проверяет.",
                         f"The active slot will change from {self.initial_slot} to {self.slot}. "
                         "This also selects the next Android boot slot; the app does not check whether that Android boots.")
            target_label = "both" if self.both_slots else self.slot
            self.say(f"yes — установить в {target_label}; a/b/both — выбрать слоты; Enter/N — выход.",
                     f"yes — install in {target_label}; a/b/both — choose slots; Enter/N — exit.")
            answer = input().strip().lower()
            if answer == "yes":
                break
            if answer in ("", "n", "no", "нет"):
                return 0
            if answer in ("a", "b", "both"):
                self.both_slots = answer == "both"
                self.slot = self.initial_slot if self.both_slots else answer
                self.slot_switch_pending = self.slot != self.initial_slot
                self.android_details["slot"] = self.slot
                continue
            self.say("Введите yes, a, b, both или нажмите Enter.", "Enter yes, a, b, both, or press Enter to exit.")
        self.create_folder()
        self.say(f"Бэкап и журнал: {self.folder}", f"Backup and log: {self.folder}")
        # Use an immutable per-run copy, so changing the source during the round-trip cannot change what is flashed.
        image = self.folder / "recovery.img"
        shutil.copyfile(source, image)
        self.check_selected_image(image)
        self.say("Проверяю разблокировку и соединение с загрузчиком. При необходимости установите драйвер fastboot "
                 "18D1:D00D (Windows: WinUSB, preset tools/balmuda-fastboot.cfg). Ожидание до 5 минут.",
                 "Checking the unlocked bootloader. If needed, install the 18D1:D00D fastboot driver "
                 "(Windows: WinUSB, tools/balmuda-fastboot.cfg preset). Waiting up to 5 minutes.")
        self.save("bootloader_requested")
        self.adb("reboot", "bootloader")
        self.check_bootloader(allow_slot_switch=True)
        self.say("Временно загружаю TWRP для сохранения recovery. При вопросе на экране выберите Keep Read Only.",
                 "Temporarily booting TWRP to back up recovery. Select Keep Read Only if prompted on the phone.")
        self.save("ram_boot_requested")
        self.fb("boot", str(image), timeout=120)
        self.check_twrp()
        originals = {partition: self.backup(partition) for partition in self.partitions}
        to_flash = [partition for partition in self.partitions if originals[partition] != self.image_hash]
        if to_flash:
            self.check_backups()
            self.adb("reboot", "bootloader")
            for partition in to_flash:
                self.check_bootloader()
                self.check_selected_image(image)
                self.check_backups()
                self.say(f"Оригинал сохранён. Записываю {partition}; не отключайте кабель.",
                         f"Original backed up. Writing {partition}; do not disconnect USB.")
                self.signals.checkpoint()
                # Protect each writer AND its durable per-partition receipt.
                with self.signals.defer():
                    self.save_partition(partition, "flash_started", flash_may_have_changed=True)
                    self.fb("flash", partition, str(image), timeout=None)
                    self.save_partition(partition, "flash_command_completed")
                self.signals.checkpoint()
            self.fb("reboot", "recovery")
            self.check_twrp()
        else:
            self.say("Этот TWRP уже установлен; повторной записи не будет.",
                     "This TWRP is already installed; no repeat flash.")
        for partition in self.partitions:
            if self.block_hash(partition) != self.image_hash:
                self.fail(f"Хеш {partition} после установки не совпал. Не продолжайте установку.",
                          f"Installed {partition} hash mismatch. Stop here.")
            self.save_partition(partition, "partition_verified", installed_sha256=self.image_hash)
        self.save("completed", final_mode="recovery", recovery_ready=True, verified_partitions=self.partitions)
        if self.both_slots:
            self.say(f"Оба recovery проверены. Телефон остаётся в TWRP, активный слот {self.slot} не изменён. "
                     "Для загрузки Android выберите Reboot → System. Оба бэкапа recovery сохранены.",
                     f"Both recovery partitions verified. Phone remains in TWRP; active slot {self.slot} is unchanged. "
                     "Choose Reboot -> System to boot Android. Both recovery backups retained.")
        elif self.slot != self.initial_slot:
            self.say(f"{self.partition} проверен. Телефон остаётся в TWRP, активный слот {self.slot}. "
                     f"Прежний Android находится в слоте {self.initial_slot}; перед его запуском верните этот активный слот. "
                     "Бэкап recovery сохранён.",
                     f"{self.partition} verified. Phone remains in TWRP, active slot {self.slot}. "
                     f"The previous Android is in slot {self.initial_slot}; select that active slot before booting it. "
                     "Recovery backup retained.")
        else:
            self.say("Recovery проверен. Телефон остаётся в TWRP. "
                     "Для загрузки Android выберите Reboot → System. Бэкап recovery сохранён.",
                     "Recovery verified. Phone remains in TWRP. "
                     "Choose Reboot -> System to boot Android. Recovery backup retained.")
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
