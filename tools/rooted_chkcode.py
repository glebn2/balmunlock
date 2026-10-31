"""Fixed chkcode operations through an already-rooted, serial-bound ADB daemon.

No privilege escalation, DIAG setup, reboot or unlock command occurs here.
Binary data never passes through a terminal or the controller's text logger.
"""
import argparse
import os
from pathlib import Path
import re
import subprocess

from chkcode_backup import CHKCODE_SIZE, MAGIC, load_backup, save_backup, validate_data
from restore_chkcode import restore_prefix

CHKCODE_PATH = "/dev/block/bootdevice/by-name/chkcode"


class RootBlock:
    def __init__(self, serial):
        if not isinstance(serial, str) or not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", serial):
            raise ValueError("A valid ANDROID_SERIAL is required")
        self.adb = ["adb", "-s", serial]
        self.opened = False
        if self.command("shell", "id", "-u").strip() != b"0":
            raise ValueError("ADB daemon is not root; enable Rooted debugging first")
        size = self.command("shell", "blockdev", "--getsize64", CHKCODE_PATH).strip()
        if size != str(CHKCODE_SIZE).encode("ascii"):
            raise ValueError(f"Unexpected chkcode size: {size!r}")

    def command(self, *args, data=None, writing=False):
        reply = subprocess.run([*self.adb, *args], input=data, capture_output=True,
                               timeout=None if writing else 30, check=False)
        if reply.returncode:
            raise ValueError(f"Root ADB operation failed: {reply.stderr.decode('utf-8', 'replace').strip()}")
        return reply.stdout

    def read_file(self, path, size):
        if path != CHKCODE_PATH or size != CHKCODE_SIZE:
            raise ValueError("Only the complete fixed-size chkcode partition may be read")
        # shell v2 without a PTY keeps dd's stderr/status separate from its
        # binary stdout. exec-out merges remote stderr into the raw stream.
        data = self.command("shell", "-T", "dd", f"if={CHKCODE_PATH}",
                            f"bs={CHKCODE_SIZE}", "count=1")
        if len(data) != CHKCODE_SIZE:
            raise ValueError(f"Unexpected chkcode read size: {len(data)} bytes, expected {CHKCODE_SIZE}")
        return data

    def open(self, path, mode):
        if path != CHKCODE_PATH or mode != 2 or self.opened:
            raise ValueError("Only the chkcode prefix may be opened for writing")
        self.opened = True
        return CHKCODE_PATH

    def lseek(self, fd, offset, whence):
        if not self.opened or fd != CHKCODE_PATH or offset != 0 or whence != 0:
            raise ValueError("Only offset zero is supported")
        return 0

    def write(self, fd, data):
        if not self.opened or fd != CHKCODE_PATH or len(data) != 8:
            raise ValueError("Only the eight-byte chkcode prefix may be written")
        self.command("shell", "-T", "dd", f"of={CHKCODE_PATH}", "bs=8", "count=1",
                     "conv=notrunc,fsync", data=data, writing=True)
        return len(data)

    def close(self, fd):
        self.opened = False


def operate(fs, action, path):
    if action == "backup":
        # Validate both reads before creating either local file.
        from chkcode_backup import require_new_backup
        require_new_backup(path)
        first = fs.read_file(CHKCODE_PATH, CHKCODE_SIZE)
        validate_data(first)
        second = fs.read_file(CHKCODE_PATH, CHKCODE_SIZE)
        validate_data(second)
        if first != second:
            raise ValueError("Two complete chkcode reads disagree; no write permitted")
        save_backup(path, first)
        return
    original = load_backup(path)
    if action == "restore":
        restore_prefix(fs, original)
        return
    if action == "verify":
        for _ in range(2):
            if fs.read_file(CHKCODE_PATH, CHKCODE_SIZE) != original:
                raise ValueError("Live chkcode differs from the saved original")
        return
    if action != "mark":
        raise ValueError("Unknown chkcode operation")
    if fs.read_file(CHKCODE_PATH, CHKCODE_SIZE) != original:
        raise ValueError("Live chkcode differs from the original; no write attempted")
    fd = fs.open(CHKCODE_PATH, 2)
    try:
        fs.lseek(fd, 0, 0)
        if fs.write(fd, MAGIC) != len(MAGIC):
            raise ValueError("Short marker write; recovery is required")
    finally:
        fs.close(fd)
    # Full readback also proves the untouched tail was preserved.
    if fs.read_file(CHKCODE_PATH, CHKCODE_SIZE) != MAGIC + original[len(MAGIC):]:
        raise ValueError("Marker readback mismatch; recovery is required")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("backup", "verify", "mark", "restore"))
    parser.add_argument("--backup", type=Path, required=True)
    args = parser.parse_args()
    # Reject missing/corrupt originals before any ADB command.
    if args.action != "backup":
        load_backup(args.backup)
    else:
        from chkcode_backup import require_new_backup
        require_new_backup(args.backup)
    operate(RootBlock(os.environ.get("ANDROID_SERIAL")), args.action, args.backup)
    print(f"Root ADB chkcode {args.action}: verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
