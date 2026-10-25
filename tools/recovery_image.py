"""Bundled recovery reference and structural checks; no device access or downloads."""
import hashlib
from pathlib import Path
import struct
from ui_language import LocalizedValueError

IMAGE_NAME = "TWRP-3.7.1_12-BALMUDA_Phone-20261025.img"
IMAGE_SHA256 = "9e4d0c0707d83a6a3d5ec3fcad4c2f3296996348603817163540ddad7a840702"
IMAGE_SIZE = 100663296
TWRP_VERSION = "3.7.1_12-BALMUDA_Phone"
TWRP_MODEL = "Phone"
STOCK_BUILD = "1.280PO.0686.a"
STOCK_FINGERPRINT = f"BALMUDA/A101BM/A101BM:12/SKQ1.220303.001/{STOCK_BUILD}:user/release-keys"
VENDOR_FINGERPRINT = f"BALMUDA/A101BM/A101BM:11/RKQ1.220301.001/{STOCK_BUILD}:user/release-keys"
TWRP_FINGERPRINT = "BALMUDA/twrp_A101BM/A101BM:12/SP2A.220405.004/builder10212025:eng/test-keys"
STOCK_IDENTITIES = {
    model: {
        "ro.product.model": model,
        "ro.product.vendor.model": model,
        "ro.build.version.sdk": "31",
        "ro.build.version.incremental": build,
        "ro.build.fingerprint": f"BALMUDA/{model}/{model}:12/SKQ1.220303.001/{build}:user/release-keys",
        "ro.vendor.build.fingerprint": f"BALMUDA/{model}/{model}:11/RKQ1.220301.001/{build}:user/release-keys",
    }
    for model, build in (("A101BM", STOCK_BUILD), ("X01A", "1.280MI.0648.a"))
}


def file_hash(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def inspect_image(path):
    path = Path(path).resolve()
    if path.stat().st_size != IMAGE_SIZE:
        raise LocalizedValueError("Recovery image size mismatch", "Неверный размер образа recovery")
    with path.open("rb") as stream:
        header = stream.read(1660)
        stream.seek(-64, 2)
        footer = stream.read(64)
    if (header[:8] != b"ANDROID!" or struct.unpack_from("<I", header, 36)[0] != 4096
            or struct.unpack_from("<I", header, 40)[0] != 2 or footer[:4] != b"AVBf"):
        raise LocalizedValueError("Invalid recovery structure", "Неверная структура образа recovery")
    kernel_size = struct.unpack_from("<I", header, 8)[0]
    ramdisk_size = struct.unpack_from("<I", header, 16)[0]
    second_size = struct.unpack_from("<I", header, 24)[0]
    original_size, vbmeta_offset, vbmeta_size = struct.unpack_from(">QQQ", footer, 12)
    aligned = lambda size: (size + 4095) // 4096 * 4096
    payload_end = 4096 + aligned(kernel_size) + aligned(ramdisk_size) + aligned(second_size)
    if (not kernel_size or not ramdisk_size or payload_end > original_size
            or original_size > vbmeta_offset or vbmeta_size < 256
            or vbmeta_offset + vbmeta_size > IMAGE_SIZE - 64):
        raise LocalizedValueError("Invalid recovery offsets", "Некорректные границы частей образа recovery")
    with path.open("rb") as stream:
        stream.seek(vbmeta_offset)
        if stream.read(4) != b"AVB0":
            raise LocalizedValueError("Invalid recovery AVB metadata", "Некорректные метаданные AVB образа recovery")
    return path


def verify_image(path):
    """Release packaging always requires the exact bundled artifact."""
    path = inspect_image(path)
    if file_hash(path) != IMAGE_SHA256:
        raise LocalizedValueError("Recovery image SHA-256 mismatch", "SHA-256 образа recovery не совпадает")
    return path


def image_path(root, override=None):
    if override is not None:
        return Path(override)
    folder = Path(root) / "recovery"
    candidates = sorted(path for path in folder.iterdir() if path.is_file() and path.suffix.lower() == ".img") if folder.is_dir() else []
    if len(candidates) > 1:
        raise LocalizedValueError(
            "Multiple .img files in recovery. Keep one or select --recovery-image PATH.",
            "В папке recovery несколько .img. Оставьте один или укажите --recovery-image PATH.")
    return candidates[0] if candidates else folder / IMAGE_NAME
