"""Compare two full live chkcode reads against an existing clean backup; no writes."""
import argparse
from pathlib import Path

from chkcode_backup import CHKCODE_SIZE, load_backup


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backup", type=Path, required=True)
    args = parser.parse_args()
    original = load_backup(args.backup)
    from diag_transport import open_transport
    from fs_sys_call import FSDiag
    transport = open_transport()
    try:
        fs = FSDiag(transport)
        for _ in range(2):
            if fs.read_file("/dev/block/bootdevice/by-name/chkcode", CHKCODE_SIZE) != original:
                raise ValueError("Live chkcode differs from the saved original. No write attempted.")
    finally:
        transport.close()
    print("Two complete live reads match the saved original. No partition writes.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
