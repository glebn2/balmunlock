"""Separate persistent session files from bundled, read-only runtime files."""
import os
from pathlib import Path
import sys

VERSION = "0.1.0"
HELPERS = {
    "cve31317.py": "cve31317",
    "kdiag_shell.py": "kdiag_shell",
    "backup_chkcode.py": "backup_chkcode",
    "unlock_chkcode.py": "unlock_chkcode",
    "restore_chkcode.py": "restore_chkcode",
    "chkcode_backup.py": "chkcode_backup",
}
_DLL_HANDLES = []


def frozen():
    return bool(getattr(sys, "frozen", False))


def session_root():
    if frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def tool_command(name, *args, root=None):
    if name not in HELPERS:
        raise ValueError(f"Unknown helper: {name}")
    if frozen():
        return [sys.executable, "--internal-tool", name, *args]
    return [sys.executable, (root or session_root()) / "tools" / name, *args]


def configure_runtime():
    """Configure only this process; never execute ADB or enumerate USB here."""
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    if not frozen():
        return
    root = session_root()
    bundle = Path(sys._MEIPASS)
    platform_tools = root / "platform-tools"
    for name in ("adb.exe", "fastboot.exe", "AdbWinApi.dll", "AdbWinUsbApi.dll",
                 "libwinpthread-1.dll"):
        if not (platform_tools / name).is_file():
            raise RuntimeError(f"Missing bundled platform-tools/{name}. Extract the complete release ZIP.")
    directories = [platform_tools, bundle / "libusb_package", bundle]
    os.environ["PATH"] = os.pathsep.join([*(str(p) for p in directories), os.environ.get("PATH", "")])
    os.environ["PYTHONUTF8"] = "1"
    if os.name == "nt":
        for directory in directories:
            if directory.is_dir():
                _DLL_HANDLES.append(os.add_dll_directory(str(directory)))
    os.chdir(root)
