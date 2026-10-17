"""Conservative stock-device admission policy, not hardware compatibility proof."""
from datetime import date
import re

# https://source.android.com/docs/security/bulletin/2024-06-01
SPL_FIX = date(2024, 6, 1)


def validate_identity(identity):
    model = identity.get("ro.product.model", "")
    if model not in ("A101BM", "X01A"):
        raise ValueError(f"Unexpected model: {model!r}")
    if identity.get("ro.build.version.sdk") != "31":
        raise ValueError("Only the documented Android 12 stock path (SDK 31) is admitted")
    build = identity.get("ro.build.version.incremental", "")
    match = re.fullmatch(r"1\.(\d{3})(?:(?:PO|MI)\.\d{4}\.[A-Za-z0-9]+)?", build)
    if match is None or not 220 <= int(match[1]) <= 280:
        raise ValueError(f"Build outside the documented stock 1.220–1.280 PO/MI policy: {build!r}")
    spl = identity.get("ro.build.version.security_patch", "")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", spl):
        raise ValueError(f"Missing or malformed security patch level: {spl!r}")
    if date.fromisoformat(spl) >= SPL_FIX:
        raise ValueError("SPL 2024-06-01 or newer: CVE-2024-31317 is covered by the June 2024 fix")
