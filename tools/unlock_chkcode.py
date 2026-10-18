#!/usr/bin/env python3
# Modified 2026-10-17: validate the backup and live partition before writing.
"""unlock_chkcode.py — запись сервисной магии LOOTBFCK в chkcode (шаг анлока).

ЧТО ДЕЛАЕТ (проверено на A101BM 1.280PO.0686.a):
  пишет 8 байт 'LOOTBFCK' (0x4C4F4F54 + 0x4246434B LE) по смещению 0
  раздела chkcode. Загрузчик (ABL) при KcFastbootCheck()==TRUE:
    1) регистрирует ПОЛНУЮ таблицу fastboot-команд (flash/erase/boot/...);
    2) пропускает проверки IsUnlocked/IsCriticalPartition внутри flash/erase;
    3) ПОБОЧНОЕ ДЕЙСТВИЕ: при каждой загрузке входит в fastboot-режим
       (BootIntoFastboot=TRUE) — Android сам НЕ ЗАГРУЗИТСЯ, пока магия
       не снята! Возврат: проверенный исходный бэкап (см. README.md#recovery).

Скрипт требует полный исходный бэкап chkcode с корректной SHA-256
и совпадение текущего раздела с этим бэкапом перед записью.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from chkcode_backup import CHKCODE_SIZE, MAGIC, load_backup

CHKCODE_PATH = "/dev/block/bootdevice/by-name/chkcode"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backup", default="chkcode_backup.bin",
                    help="файл бэкапа (обязателен; создаётся backup_chkcode.py)")
    ap.add_argument("--yes", action="store_true", help="не спрашивать подтверждение")
    a = ap.parse_args()

    backup = Path(a.backup)
    try:
        original = load_backup(backup)
    except (OSError, ValueError) as error:
        print(f"ОТКАЗ: {error}", file=sys.stderr)
        return 2

    if not a.yes:
        print("Запись LOOTBFCK включит сервисный режим: телефон будет ВСЕГДА")
        print("загружаться в fastboot, пока магия не снята. Продолжить? [yes/N]")
        if input().strip().lower() != "yes":
            print("Отменено.")
            return 1

    from diag_transport import open_transport
    from fs_sys_call import FSDiag, FSError, O_RDWR, SEEK_SET

    tr = open_transport()
    try:
        fs = FSDiag(tr)
        # A valid file alone does not prove it belongs to the connected phone.
        if fs.read_file(CHKCODE_PATH, CHKCODE_SIZE) != original:
            print("ОТКАЗ: текущий chkcode не совпадает с бэкапом. Записи не было.",
                  file=sys.stderr)
            return 2
        fd = fs.open(CHKCODE_PATH, O_RDWR)
        try:
            fs.lseek(fd, 0, SEEK_SET)
            n = fs.write(fd, MAGIC)
            if n != len(MAGIC):
                print(f"ОТКАЗ: записано {n} байт вместо {len(MAGIC)}", file=sys.stderr)
                return 1
            try:
                back = fs.verify_at(fd, 0, MAGIC + original[8:16])
            except FSError as error:
                print(f"Не удалось подтвердить результат записи: {error}", file=sys.stderr)
                print("Метка могла быть записана. Общий сценарий попробует откат; "
                      "при прямом запуске утилиты не перезагружайте телефон, "
                      "см. README.md#recovery.", file=sys.stderr)
                return 1
            print("Контрольное чтение (первые 16):", back.hex(" "))
        finally:
            fs.close(fd)
        print("OK: LOOTBFCK записан и подтверждён.")
        if os.environ.get("BALMUNLOCK_CONTROLLER") != "1":
            print("Далее: adb reboot bootloader -> fastboot flashing unlock ->")
            print("выбрать разблокировку кнопками громкости и подтвердить по подсказке экрана "
                  "(на проверенном A101BM — кнопкой питания). См. README.md#usage.")
    finally:
        tr.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
