"""Create and read the fixed TWRP release ZIP. Never extracts arbitrary paths."""
import argparse
import hashlib
from pathlib import Path
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from recovery_image import IMAGE_NAME, IMAGE_SHA256, IMAGE_SIZE, image_path, verify_image, file_hash

ARCHIVE_NAME = IMAGE_NAME.removesuffix(".img") + ".zip"
ARCHIVE_URL = f"https://github.com/glebn2/balmunlock/releases/download/v0.5.0/{ARCHIVE_NAME}"
ARCHIVE_SHA256 = "ca82e56feb5d3e835d318cf06940e50fb3408aefaac8fa49299bec7294d5f1d2"


def read_image(archive_path):
    if file_hash(archive_path) != ARCHIVE_SHA256:
        raise ValueError("Recovery archive SHA-256 mismatch")
    with zipfile.ZipFile(archive_path) as archive:
        images = [entry for entry in archive.infolist() if entry.filename == IMAGE_NAME]
        if len(images) != 1 or images[0].file_size != IMAGE_SIZE:
            raise ValueError("Recovery archive image missing, duplicated or wrong size")
        data = archive.read(images[0])
    if hashlib.sha256(data).hexdigest() != IMAGE_SHA256:
        raise ValueError("Archived recovery image SHA-256 mismatch")
    return data


def create_archive(output):
    source = verify_image(image_path(ROOT))
    entries = [(IMAGE_NAME, source.read_bytes()),
               ("SHA256SUMS", f"{IMAGE_SHA256}  {IMAGE_NAME}\n".encode("ascii")),
               ("INSTALL.md", (ROOT / "recovery" / "INSTALL.md").read_bytes().replace(b"\r\n", b"\n")),
               ("CHANGELOG-20261024.md", (ROOT / "recovery" / "CHANGELOG-20261024.md").read_bytes().replace(b"\r\n", b"\n"))]
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name, data in entries:
            entry = zipfile.ZipInfo(name, date_time=(2026, 10, 24, 0, 0, 0))
            entry.create_system = 3
            entry.external_attr = 0o100644 << 16
            entry.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(entry, data, compresslevel=9)
    digest = file_hash(output)
    output.with_suffix(".zip.sha256").write_text(f"{digest}  {output.name}\n", encoding="ascii")
    print(f"Recovery archive: {output}\nSHA-256: {digest}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "dist" / ARCHIVE_NAME)
    create_archive(parser.parse_args().output)
