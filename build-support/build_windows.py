"""Build a portable Windows x64 archive from pinned inputs; never access a phone."""
import argparse
import hashlib
import importlib.metadata
import json
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from runtime_paths import VERSION


def fetch(item, cache):
    path = cache / item["filename"]
    if not path.exists():
        request = urllib.request.Request(item["url"], headers={"User-Agent": "unmuda-build"})
        with urllib.request.urlopen(request, timeout=120) as response:
            data = response.read()
        if hashlib.sha256(data).hexdigest() != item["sha256"]:
            raise ValueError(f"Download hash mismatch: {item['filename']}")
        path.write_bytes(data)
    if hashlib.sha256(path.read_bytes()).hexdigest() != item["sha256"]:
        raise ValueError(f"Cached download hash mismatch: {path}")
    return path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache", type=Path, default=ROOT / ".build" / "downloads")
    args = parser.parse_args()
    if sys.platform != "win32" or platform.machine().lower() not in ("amd64", "x86_64"):
        parser.error("Build on Windows x64 using x64 Python")
    args.cache.mkdir(parents=True, exist_ok=True)
    manifest = json.loads((ROOT / "build-support" / "downloads.json").read_text())
    platform_zip = fetch(manifest["platform_tools"], args.cache)
    sources = [fetch(item, args.cache) for item in manifest["sources"]]
    output = ROOT / "dist" / f"unmuda-v{VERSION}-windows-x64"
    if output.exists():
        raise FileExistsError(f"Refusing to reuse release directory: {output}")
    work = (ROOT / ".build" / "pyinstaller").resolve()
    frozen = (ROOT / ".build" / "frozen").resolve()
    for target in (work, frozen):
        if not target.is_relative_to((ROOT / ".build").resolve()):
            raise ValueError(f"Build path outside intended directory: {target}")
    subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
                    "--workpath", str(work), "--distpath", str(frozen),
                    str(ROOT / "build-support" / "unmuda.spec")], cwd=ROOT, check=True)
    shutil.copytree(frozen / "unmuda", output)
    platform_files = ("adb.exe", "fastboot.exe", "AdbWinApi.dll", "AdbWinUsbApi.dll",
                      "libwinpthread-1.dll", "NOTICE.txt", "source.properties")
    (output / "platform-tools").mkdir()
    with zipfile.ZipFile(platform_zip) as archive:
        for name in platform_files:
            (output / "platform-tools" / name).write_bytes(archive.read("platform-tools/" + name))
    (output / "tools").mkdir()
    shutil.copy2(ROOT / "tools" / "balmuda-fastboot.cfg", output / "tools")
    for name in ("README.md", "README.en.md", "LICENSE"):
        shutil.copy2(ROOT / name, output / name)
    licenses = output / "licenses"
    licenses.mkdir()
    shutil.copy2(Path(sys.base_prefix) / "LICENSE.txt", licenses / "Python-LICENSE.txt")
    for dist in importlib.metadata.distributions():
        destination = licenses / dist.metadata["Name"]
        destination.mkdir()
        for entry in dist.files or ():
            if any(word in entry.name.lower() for word in ("license", "copying", "notice")):
                source = Path(dist.locate_file(entry))
                if source.is_file():
                    parts = list(entry.parts)
                    metadata_index = next((i for i, part in enumerate(parts) if part.endswith(".dist-info")), None)
                    if metadata_index is not None:
                        parts = parts[metadata_index + 1:]
                    if any(part == ".." for part in parts):
                        raise ValueError(f"Unexpected license path: {entry}")
                    target = destination.joinpath(*parts)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, target)
    source_dir = licenses / "libusb-sources"
    source_dir.mkdir()
    for path in sources:
        shutil.copy2(path, source_dir / path.name)
    with zipfile.ZipFile(sources[-1]) as archive:
        copying = next(name for name in archive.namelist() if name.endswith("/COPYING"))
        (licenses / "libusb-COPYING.txt").write_bytes(archive.read(copying))
    metadata = {"version": VERSION, "python": platform.python_version(),
                "commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
                "uncommitted_changes": bool(subprocess.check_output(
                    ["git", "status", "--porcelain"], cwd=ROOT, text=True).strip()),
                "dependencies": {name: importlib.metadata.version(name) for name in
                                 ("pyusb", "libusb-package", "importlib-resources", "pyinstaller")},
                "downloads": manifest,
                "source": "https://github.com/glebn2/unmuda"}
    (output / "BUILD-INFO.json").write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    (licenses / "SOURCES.txt").write_text(
        "unmuda source: https://github.com/glebn2/unmuda (release tag or BUILD-INFO commit).\n"
        "libusb is a replaceable shared DLL under _internal/libusb_package; its corresponding\n"
        "source and wrapper build instructions are included in libusb-sources/.\n"
        "PyUSB: https://github.com/pyusb/pyusb/tree/v1.3.1\n"
        "importlib_resources: https://github.com/python/importlib_resources/tree/v7.1.0\n"
        "Python: https://www.python.org/downloads/source/\n"
        "PyInstaller: https://github.com/pyinstaller/pyinstaller (GPL with bootloader exception).\n"
        "Android Platform-Tools notices: ../platform-tools/NOTICE.txt\n", encoding="utf-8")
    subprocess.run([sys.executable, str(ROOT / "build-support" / "smoke_windows.py"), str(output)],
                   cwd=ROOT, check=True)
    zip_path = Path(shutil.make_archive(str(output), "zip", root_dir=output.parent, base_dir=output.name))
    digest = hashlib.sha256(zip_path.read_bytes()).hexdigest()
    zip_path.with_suffix(".zip.sha256").write_text(f"{digest}  {zip_path.name}\n", encoding="ascii")
    print(f"Release: {zip_path}\nSHA-256: {digest}")


if __name__ == "__main__":
    main()
