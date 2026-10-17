"""Check the frozen app without connecting to ADB, fastboot devices or USB."""
import argparse
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    scratch = ROOT / ".build"
    scratch.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="smoke spaces Кириллица ", dir=scratch) as temporary:
        temporary = Path(temporary)
        app = temporary / "portable"
        shutil.copytree(args.directory, app)
        caller = temporary / "unrelated caller"
        caller.mkdir()
        env = os.environ.copy()
        windows = os.environ["SystemRoot"]
        env["PATH"] = os.pathsep.join([str(Path(windows) / "System32"), windows])
        exe = app / "unmuda.exe"

        def run(*command, expected=0, input=None):
            result = subprocess.run([str(x) for x in command], cwd=caller, env=env,
                                    input=input, capture_output=True, text=True,
                                    encoding="utf-8", errors="replace", timeout=40)
            if result.returncode != expected:
                raise AssertionError(f"{command}: exit={result.returncode}\n{result.stdout}\n{result.stderr}")
            return result.stdout + result.stderr

        assert "unmuda 0.1.0" in run(exe, "--version")
        assert "--recover" in run(exe, "--help")
        preflight = run(exe, "--preflight", "--lang", "en")
        assert "libusb: available" in preflight and str(app / "platform-tools") in preflight, preflight
        for helper in ("cve31317.py", "kdiag_shell.py", "backup_chkcode.py", "unlock_chkcode.py",
                       "restore_chkcode.py", "chkcode_backup.py"):
            run(exe, "--internal-tool", helper, "--help")
        run(exe, "--internal-tool", "unknown.py", expected=2)
        assert "printf" in run(exe, "--internal-tool", "cve31317.py", "--encode", "diag,modem,adb")
        # Exercise the controller's real child-process path with a host-only helper.
        import runtime_paths
        import unlock
        with patch.object(runtime_paths, "frozen", return_value=True), \
                patch.object(runtime_paths.sys, "executable", str(exe)), \
                patch.object(unlock, "ROOT", app):
            unlock.Workflow("en").tool("cve31317.py", "--encode", "diag,modem,adb")
        image = caller / "sample.bin"
        image.write_bytes(bytes(524288))
        image.with_suffix(".sha256").write_text(
            f"{hashlib.sha256(image.read_bytes()).hexdigest()}  {image.name}\n", encoding="ascii")
        run(exe, "--internal-tool", "chkcode_backup.py", image)
        image.write_bytes(b"truncated")
        run(exe, "--internal-tool", "unlock_chkcode.py", "--backup", image, "--yes", expected=2)
        run(exe, input="no\n")
        assert "Android Debug Bridge" in run(app / "platform-tools" / "adb.exe", "version")
        assert "fastboot version" in run(app / "platform-tools" / "fastboot.exe", "--version")
        assert not list(app.glob("chkcode_backup*")) and not (app / "cve_cleanup.device.json").exists()
        print("Frozen smoke checks passed: isolated PATH, Unicode paths, helpers, libusb and no device operations.")


if __name__ == "__main__":
    main()
