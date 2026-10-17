#!/usr/bin/env python3
"""Restore only the original chkcode prefix over a full/partial service marker.

Modified 2026-10-17. No reboot, exploit, slot changes or bootloader relock.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path

from chkcode_backup import CHKCODE_SIZE, MAGIC, load_backup, validate_data

CHKCODE_PATH = "/dev/block/bootdevice/by-name/chkcode"


def restore_prefix(fs, original: bytes) -> bool:
    """Require the unchanged tail and a prefix explainable by our 8-byte write."""
    validate_data(original)
    current = fs.read_file(CHKCODE_PATH, CHKCODE_SIZE)
    if current == original:
        return False
    possible = {MAGIC[:count] + original[count:8] for count in range(1, 9)}
    if len(current) != CHKCODE_SIZE or current[8:] != original[8:] or current[:8] not in possible:
        raise ValueError("Unexpected chkcode contents; automatic DIAG rollback refused")
    fd = fs.open(CHKCODE_PATH, 2)  # O_RDWR, protocol constant; no USB import here.
    try:
        if fs.lseek(fd, 0, 0) != 0:
            raise ValueError("Failed to seek to chkcode offset zero; no write attempted")
        if fs.write(fd, original[:8]) != 8:
            raise ValueError("Short rollback write; do not reboot")
    finally:
        fs.close(fd)
    if fs.read_file(CHKCODE_PATH, CHKCODE_SIZE) != original:
        raise ValueError("Rollback readback mismatch; do not reboot")
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backup", type=Path, required=True)
    parser.add_argument("--yes", action="store_true")
    args = parser.parse_args()
    if not os.environ.get("ANDROID_SERIAL"):
        parser.exit(2, "ANDROID_SERIAL is required; select the original device first.\n")
    original = load_backup(args.backup)
    if not args.yes and input("Restore the original chkcode prefix? [yes/N] ").strip().lower() != "yes":
        return 1
    from diag_transport import open_transport
    from fs_sys_call import FSDiag

    tr = open_transport()
    try:
        changed = restore_prefix(FSDiag(tr), original)
        print("Original chkcode restored and verified" if changed else "Already matches the original chkcode")
    finally:
        tr.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
