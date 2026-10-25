"""Portable downloads contain the runtime, USB preset and recovery images only."""
from pathlib import PurePosixPath

PLATFORM_FILES = frozenset(("adb.exe", "fastboot.exe", "AdbWinApi.dll",
                            "AdbWinUsbApi.dll", "libwinpthread-1.dll", "NOTICE.txt"))


def is_runtime_file(name):
    if "\\" in name:
        return False
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        return False
    parts = path.parts
    if parts[0] == "_internal" and len(parts) > 1:
        return (not any(part in (".local-notes", "sessions", "recovery-sessions", ".git") for part in parts)
                and not path.name.lower().startswith(("readme", "changelog", "build-info"))
                and path.name.lower() not in ("install.md", "sources.txt")
                and not path.name.lower().endswith("-source.zip"))
    if len(parts) == 1:
        return name in ("balmunlock.exe", "unmuda.exe")
    if len(parts) == 2 and parts[0] == "platform-tools":
        return parts[1] in PLATFORM_FILES
    if len(parts) == 2 and parts[0] == "tools":
        return parts[1] == "balmuda-fastboot.cfg"
    return len(parts) == 2 and parts[0] == "recovery" and path.suffix.lower() == ".img"


def check_runtime_layout(directory):
    unexpected = [path.relative_to(directory).as_posix() for path in directory.rglob("*")
                  if path.is_file() and not is_runtime_file(path.relative_to(directory).as_posix())]
    if unexpected:
        raise ValueError("Non-runtime files in portable package: " + ", ".join(unexpected))
