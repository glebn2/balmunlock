"""Host-only chkcode validation; importing this module never opens USB."""
from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path

CHKCODE_SIZE = 0x80000
MAGIC = b"LOOTBFCK"


def validate_data(data: bytes) -> None:
    if len(data) != CHKCODE_SIZE:
        raise ValueError(f"chkcode: {len(data)} bytes, expected {CHKCODE_SIZE}")
    if data[:len(MAGIC)] == MAGIC:
        raise ValueError("chkcode contains LOOTBFCK; this is not a clean restore image")


def checksum_path(path: Path) -> Path:
    # Keep the upstream naming: chkcode_backup.bin -> chkcode_backup.sha256.
    return path.with_suffix(".sha256")


def require_new_backup(path: Path) -> None:
    for candidate in (path, checksum_path(path)):
        if candidate.exists() or candidate.is_symlink():
            raise FileExistsError(f"Will not overwrite {candidate}. See README.md#recovery.")


def save_backup(path: Path, data: bytes) -> str:
    validate_data(data)
    require_new_backup(path)
    digest = hashlib.sha256(data).hexdigest()
    # Exclusive creation also protects against a second process racing this one.
    # An interrupted pair is retained for inspection and rejected by load_backup.
    with path.open("xb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    with checksum_path(path).open("x", encoding="utf-8") as stream:
        stream.write(f"{digest}  {path.name}\n")
        stream.flush()
        os.fsync(stream.fileno())
    load_backup(path)
    return digest


def load_backup(path: Path) -> bytes:
    data = path.read_bytes()
    validate_data(data)
    expected = checksum_path(path).read_text(encoding="utf-8").strip()
    actual = f"{hashlib.sha256(data).hexdigest()}  {path.name}"
    if expected != actual:
        raise ValueError(f"SHA-256 mismatch or invalid checksum file: {path}")
    return data


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("backup", type=Path)
    args = parser.parse_args()
    try:
        data = load_backup(args.backup)
    except (OSError, ValueError) as error:
        parser.exit(1, f"ERROR: {error}\n")
    print(f"OK: {len(data)} bytes; clean prefix; SHA-256 matches")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
