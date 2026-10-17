# Console-only portable build. No session files or local reports are collected.
from pathlib import Path
import sys
from PyInstaller.utils.hooks import collect_dynamic_libs, collect_submodules, copy_metadata

root = Path(SPECPATH).parent
sys.path.insert(0, str(root / "tools"))
from runtime_paths import HELPERS

a = Analysis(
    [str(root / "tools" / "launcher.py")],
    pathex=[str(root / "tools")],
    binaries=collect_dynamic_libs("libusb_package"),
    datas=copy_metadata("pyusb") + copy_metadata("libusb-package"),
    hiddenimports=list(HELPERS.values()) + collect_submodules("usb") + ["libusb_package"],
    hookspath=[], runtime_hooks=[], excludes=["tkinter", "unittest"], noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [], exclude_binaries=True, name="unmuda", console=True,
    debug=False, strip=False, upx=False, disable_windowed_traceback=False,
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="unmuda")
