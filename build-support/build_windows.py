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
from recovery_image import IMAGE_NAME, IMAGE_SHA256, image_path, verify_image
from recovery_archive import ARCHIVE_NAME, ARCHIVE_URL, ARCHIVE_SHA256, read_image
from runtime_bundle import PLATFORM_FILES, check_runtime_layout


def fetch(item, cache):
    path = cache / item["filename"]
    if not path.exists():
        request = urllib.request.Request(item["url"], headers={"User-Agent": "balmunlock-build"})
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
    recovery = parser.add_mutually_exclusive_group()
    recovery.add_argument("--recovery-image", type=Path, help="Bundle this pinned TWRP image")
    recovery.add_argument("--recovery-url", help="Download the pinned image over HTTPS and bundle it")
    recovery.add_argument("--recovery-archive-url", nargs="?", const=ARCHIVE_URL,
                          help="Download the fixed TWRP ZIP (default: pinned release URL) and bundle its image")
    recovery.add_argument("--without-recovery", action="store_true", help="Explicitly build without the TWRP binary")
    args = parser.parse_args()
    if sys.platform != "win32" or platform.machine().lower() not in ("amd64", "x86_64"):
        parser.error("Build on Windows x64 using x64 Python")
    args.cache.mkdir(parents=True, exist_ok=True)
    recovery_source = None
    if not args.without_recovery:
        if args.recovery_archive_url:
            if not args.recovery_archive_url.startswith("https://"):
                parser.error("--recovery-archive-url must use HTTPS")
            archive_path = fetch({"url": args.recovery_archive_url, "filename": ARCHIVE_NAME,
                                  "sha256": ARCHIVE_SHA256}, args.cache)
            data = read_image(archive_path)
            recovery_source = args.cache / IMAGE_NAME
            recovery_source.write_bytes(data)
        elif args.recovery_url:
            if not args.recovery_url.startswith("https://"):
                parser.error("--recovery-url must use HTTPS")
            recovery_source = fetch({"url": args.recovery_url, "filename": IMAGE_NAME,
                                     "sha256": IMAGE_SHA256}, args.cache)
        else:
            recovery_source = image_path(ROOT, args.recovery_image)
        recovery_source = verify_image(recovery_source)
    manifest = json.loads((ROOT / "build-support" / "downloads.json").read_text())
    platform_zip = fetch(manifest["platform_tools"], args.cache)
    sources = [fetch(item, args.cache) for item in manifest["sources"]]
    output = ROOT / "dist" / f"balmunlock-v{VERSION}-windows-x64"
    if output.exists():
        raise FileExistsError(f"Refusing to reuse release directory: {output}")
    work = (ROOT / ".build" / "pyinstaller").resolve()
    frozen = (ROOT / ".build" / "frozen").resolve()
    for target in (work, frozen):
        if not target.is_relative_to((ROOT / ".build").resolve()):
            raise ValueError(f"Build path outside intended directory: {target}")
    subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
                    "--workpath", str(work), "--distpath", str(frozen),
                    str(ROOT / "build-support" / "balmunlock.spec")], cwd=ROOT, check=True)
    shutil.copytree(frozen / "balmunlock", output)
    (output / "platform-tools").mkdir()
    with zipfile.ZipFile(platform_zip) as archive:
        for name in sorted(PLATFORM_FILES):
            (output / "platform-tools" / name).write_bytes(archive.read("platform-tools/" + name))
    (output / "tools").mkdir()
    shutil.copy2(ROOT / "tools" / "balmuda-fastboot.cfg", output / "tools")
    (output / "recovery").mkdir()
    if recovery_source is not None:
        bundled_image = output / "recovery" / IMAGE_NAME
        shutil.copyfile(recovery_source, bundled_image)
        verify_image(bundled_image)
    else:
        print("Building without the recovery binary; installation requires the pinned image separately.")
    licenses = output / "_internal" / "licenses"
    licenses.mkdir()
    shutil.copy2(ROOT / "LICENSE", licenses / "balmunlock-LICENSE.txt")
    shutil.copy2(Path(sys.base_prefix) / "LICENSE.txt", licenses / "Python-LICENSE.txt")
    # Only notices for redistributed components; no build environment inventory.
    for name in ("importlib-resources", "pyinstaller"):
        dist = importlib.metadata.distribution(name)
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
                "recovery": {"bundled": recovery_source is not None, "filename": IMAGE_NAME,
                             "sha256": IMAGE_SHA256, "archive_url": ARCHIVE_URL,
                             "archive_sha256": ARCHIVE_SHA256},
                "source": "https://github.com/glebn2/balmunlock"}
    metadata_dir = ROOT / ".build" / "metadata"
    metadata_dir.mkdir(parents=True, exist_ok=True)
    (metadata_dir / f"balmunlock-v{VERSION}-BUILD-INFO.json").write_text(
        json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    check_runtime_layout(output)
    subprocess.run([sys.executable, str(ROOT / "build-support" / "smoke_windows.py"), str(output)],
                   cwd=ROOT, check=True)
    zip_path = Path(shutil.make_archive(str(output), "zip", root_dir=output.parent, base_dir=output.name))
    digest = hashlib.sha256(zip_path.read_bytes()).hexdigest()
    zip_path.with_suffix(".zip.sha256").write_text(f"{digest}  {zip_path.name}\n", encoding="ascii")
    print(f"Release: {zip_path}\nSHA-256: {digest}")


if __name__ == "__main__":
    main()
