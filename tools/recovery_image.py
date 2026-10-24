"""Pinned, hardware-tested recovery artifact; no device access or downloads."""
import hashlib
from pathlib import Path
import struct
from ui_language import LocalizedValueError

IMAGE_NAME = "TWRP-3.7.1_12-BALMUDA_Phone-20261024.img"
IMAGE_SHA256 = "9d19aeaac7b6f96d00fa4745f7cdc3642b2a06988124c8859d2f59f0d604a6f7"
IMAGE_SIZE = 100663296
TWRP_VERSION = "3.7.1_12-BALMUDA_Phone"
TWRP_MODEL = "Phone"
STOCK_BUILD = "1.280PO.0686.a"
STOCK_FINGERPRINT = f"BALMUDA/A101BM/A101BM:12/SKQ1.220303.001/{STOCK_BUILD}:user/release-keys"
VENDOR_FINGERPRINT = f"BALMUDA/A101BM/A101BM:11/RKQ1.220301.001/{STOCK_BUILD}:user/release-keys"
TWRP_FINGERPRINT = "BALMUDA/twrp_A101BM/A101BM:12/SP2A.220405.004/builder10212025:eng/test-keys"


def file_hash(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_image(path):
    path = Path(path).resolve()
    if path.stat().st_size != IMAGE_SIZE:
        raise LocalizedValueError("Recovery image size mismatch", "Неверный размер образа recovery")
    if file_hash(path) != IMAGE_SHA256:
        raise LocalizedValueError("Recovery image SHA-256 mismatch", "SHA-256 образа recovery не совпадает")
    with path.open("rb") as stream:
        header = stream.read(1660)
        stream.seek(-64, 2)
        footer = stream.read(64)
    if (header[:8] != b"ANDROID!" or struct.unpack_from("<I", header, 36)[0] != 4096
            or struct.unpack_from("<I", header, 40)[0] != 2 or footer[:4] != b"AVBf"):
        raise LocalizedValueError("Invalid recovery structure", "Неверная структура образа recovery")
    return path


def image_path(root, override=None):
    return Path(override) if override else Path(root) / "recovery" / IMAGE_NAME
