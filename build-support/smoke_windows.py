"""Check the frozen app without connecting to ADB, fastboot devices or USB."""
import argparse
import hashlib
import json
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
        exe = app / "balmunlock.exe"

        def run(*command, expected=0, input=None):
            result = subprocess.run([str(x) for x in command], cwd=caller, env=env,
                                    input=input, capture_output=True, text=True,
                                    encoding="utf-8", errors="replace", timeout=40)
            if result.returncode != expected:
                raise AssertionError(f"{command}: exit={result.returncode}\n{result.stdout}\n{result.stderr}")
            return result.stdout + result.stderr

        from runtime_paths import VERSION
        assert f"balmunlock {VERSION}" in run(exe, "--version")
        assert "--recover" in run(exe, "--help")
        assert "--verbose" in run(exe, "--help", "--lang", "en")
        preflight = run(exe, "--preflight", "--lang", "en")
        assert "libusb: available" in preflight and str(app / "platform-tools") in preflight, preflight
        for helper in ("cve31317.py", "kdiag_shell.py", "backup_chkcode.py", "unlock_chkcode.py",
                       "restore_chkcode.py", "chkcode_backup.py", "diag_probe.py", "verify_chkcode.py"):
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
        run(exe, "--list-sessions", input="")
        run(exe, "--guided", "--session", "legacy", input="", expected=1)
        assert "Android Debug Bridge" in run(app / "platform-tools" / "adb.exe", "version")
        assert "fastboot version" in run(app / "platform-tools" / "fastboot.exe", "--version")
        assert not list(app.glob("chkcode_backup*")) and not (app / "cve_cleanup.device.json").exists()
        # Saved v1 receipts are checked before device discovery, even in the real EXE.
        original = bytes(524288)
        digest = hashlib.sha256(original).hexdigest()
        for name in ("chkcode_backup.bin", "chkcode_backup.recovery.bin"):
            path = app / name
            path.write_bytes(original)
            path.with_suffix(".sha256").write_text(f"{digest}  {name}\n", encoding="ascii")
        receipt = app / "chkcode_backup.device.json"
        state = {"version": 1, "serial": "OFFLINE_TEST", "phase": "reboot_submitted",
                 "marker_possible": False, "marker_verified": True, "reboot_pending": False,
                 "sha256": digest, "ro.product.model": "A101BM", "ro.build.version.sdk": "31",
                 "ro.build.version.incremental": "1.280PO.0686.a", "ro.build.version.security_patch": "2023-09-01"}
        receipt.write_text(json.dumps(state), encoding="utf-8")
        completed = run(exe, "--guided", "--session", "legacy", "--lang", "en", input="")
        assert "already finished" in completed
        assert completed.count("balmunlock " + VERSION) == 1
        assert completed.count("Backups and logs:") == 1
        state.update(phase="prepared", marker_verified=False)
        receipt.write_text(json.dumps(state), encoding="utf-8")
        assert "saved preparation" in run(exe, "--guided", "--session", "legacy", "--lang", "en", input="no\n")
        receipt.write_text("corrupted receipt", encoding="utf-8")
        run(exe, "--guided", "--session", "legacy", "--lang", "en", input="no\n", expected=1)
        assert receipt.read_text() == "corrupted receipt"
        assert (app / "chkcode_backup.bin").read_bytes() == original
        # Two device sessions coexist in one real portable directory. Explicit
        # selection exercises routing without querying attached phones.
        from session_store import SessionStore
        store = SessionStore(app)
        for serial in ("FIRST_TEST", "SECOND_TEST"):
            session = store.create(serial)
            for name in ("chkcode_backup.bin", "chkcode_backup.recovery.bin"):
                path = session.root / name
                path.write_bytes(original)
                path.with_suffix(".sha256").write_text(f"{digest}  {name}\n", encoding="ascii")
            state.update(serial=serial, phase="reboot_submitted", marker_verified=True)
            (session.root / "chkcode_backup.device.json").write_text(json.dumps(state), encoding="utf-8")
            assert "already finished" in run(exe, "--guided", "--session", serial, "--lang", "en", input="")
        listing = run(exe, "--list-sessions", "--lang", "en")
        assert "FIRST_TEST" in listing and "SECOND_TEST" in listing and "legacy" in listing
        print("Frozen smoke checks passed: isolated PATH, Unicode paths, helpers, libusb and no device operations.")


if __name__ == "__main__":
    main()
