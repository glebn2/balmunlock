#!/usr/bin/env python3
# Modified 2026-10-17: verified double-read backups; never overwrite originals.
"""backup_chkcode.py — дамп раздела chkcode через fs_sys_call (БЕЗ анлока).

Обязательный первый шаг перед любой записью в chkcode.

Раздел: /dev/block/bootdevice/by-name/chkcode -> /dev/block/sda16,
размер 0x80000 (512 КиБ). SELinux домена fs_sys_call_diag разрешает
blk_file open/read/write именно для chkcode_block_device — это единственный
блочный раздел, читаемый этим путём без разблокировки загрузчика.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from chkcode_backup import CHKCODE_SIZE, require_new_backup, save_backup, validate_data

CHKCODE_PATH = "/dev/block/bootdevice/by-name/chkcode"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-o", "--out", default="chkcode_backup.bin")
    a = ap.parse_args()
    out = Path(a.out)
    require_new_backup(out)
    from diag_transport import open_transport
    from fs_sys_call import FSDiag

    tr = open_transport()
    try:
        fs = FSDiag(tr)
        print(f"Двойное чтение {CHKCODE_PATH} ({CHKCODE_SIZE} байт)...")
        data = fs.read_file(CHKCODE_PATH, CHKCODE_SIZE)
        validate_data(data)
        second = fs.read_file(CHKCODE_PATH, CHKCODE_SIZE)
        validate_data(second)
        if data != second:
            raise ValueError("Повторное чтение chkcode не совпало. Запись запрещена.")
    finally:
        tr.close()
    digest = save_backup(out, data)
    print(f"Сохранено: {out} ({len(data)} байт)")
    print(f"SHA-256: {digest}")
    print(f"Первые 16 байт: {data[:16].hex(' ')}")
    if set(data[:8]) <= {0x00, 0xFF}:
        print("Раздел выглядит чистым (нули/FF) — бэкап всё равно сохранён.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
