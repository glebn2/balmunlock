"""Conservative stock-device admission policy, not hardware compatibility proof."""
from datetime import date
import re

# https://source.android.com/docs/security/bulletin/2024-06-01
SPL_FIX = date(2024, 6, 1)


def validate_identity(identity, lang="en"):
    def reject(ru, en):
        raise ValueError(ru if lang == "ru" else en)

    model = identity.get("ro.product.model", "")
    if model not in ("A101BM", "X01A"):
        reject(f"Модель {model!r} не поддерживается. Нужен BALMUDA Phone A101BM или X01A.", f"Unexpected model: {model!r}")
    if identity.get("ro.build.version.sdk") != "31":
        reject("Нужен Android 12. Эта система не поддерживается данным способом анлока.",
               "Only the documented Android 12 stock path (SDK 31) is admitted")
    build = identity.get("ro.build.version.incremental", "")
    match = re.fullmatch(r"1\.(\d{3})(?:(?:PO|MI)\.\d{4}\.[A-Za-z0-9]+)?", build)
    if match is None or not 220 <= int(match[1]) <= 280:
        reject(f"Прошивка {build!r} вне поддерживаемого диапазона 1.220–1.280 PO/MI.",
               f"Build outside the documented stock 1.220–1.280 PO/MI policy: {build!r}")
    spl = identity.get("ro.build.version.security_patch", "")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", spl):
        reject(f"Не удалось определить дату патча безопасности: {spl!r}.", f"Missing or malformed security patch level: {spl!r}")
    if date.fromisoformat(spl) >= SPL_FIX:
        reject("Патч безопасности от 2024-06-01 или новее: необходимая уязвимость исправлена. Анлок не начинается.",
               "SPL 2024-06-01 or newer: CVE-2024-31317 is covered by the June 2024 fix")
