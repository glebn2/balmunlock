"""Owned cleanup receipt for the temporary Android settings used by cve31317.

Modified 2026-10-17. No exploit, partition access or bootloader operations.
Modified 2026-10-18: keep the receipt beside the packaged executable.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import time
from runtime_paths import session_root

STATE_NAME = "cve_cleanup.device.json"
DEFAULT_STATE = session_root() / STATE_NAME
PAYLOAD_PATH = "/data/local/tmp/p31317.txt"
HIDDEN_GET = "settings get global hidden_api_blacklist_exemptions"
USAP_GET = "device_config get activity_manager native_usap_pool_enabled"


class CleanupError(RuntimeError):
    pass


def command(adb, args, timeout=5):
    result = subprocess.run([*adb, *args], capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=timeout)
    if result.returncode:
        raise CleanupError(f"ADB cleanup/snapshot failed: {result.stderr.strip()}")
    return result.stdout.rstrip("\r\n")


def load_state(path):
    state = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(state, dict) or state.get("version") != 1:
        raise CleanupError("Unsupported CVE cleanup receipt")
    serial = state.get("serial")
    if not isinstance(serial, str) or not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", serial):
        raise CleanupError("Invalid CVE cleanup serial")
    hidden = state.get("hidden_api")
    if hidden is not None and (not isinstance(hidden, str) or "\n" in hidden or "\r" in hidden):
        raise CleanupError("Refusing to restore a multiline/unknown hidden API value")
    if state.get("usap") not in (None, "true", "false"):
        raise CleanupError("Unsupported saved USAP value")
    if "hidden_api" not in state or "usap" not in state:
        raise CleanupError("Incomplete CVE cleanup receipt")
    return state


def cleanup_pending(path=DEFAULT_STATE, adb_exe="adb", expected_serial=None, timeout=30, child_options=None):
    path = Path(path)
    if not path.exists():
        return False
    state = load_state(path)
    if expected_serial is not None and state["serial"] != expected_serial:
        raise CleanupError("Pending CVE cleanup belongs to another serial; no commands sent")
    adb = [adb_exe, "-s", state["serial"]]
    hidden, usap = state["hidden_api"], state["usap"]
    commands = [
        "settings delete global hidden_api_blacklist_exemptions" if hidden is None else
        "settings put global hidden_api_blacklist_exemptions " + shlex.quote(hidden),
        "device_config delete activity_manager native_usap_pool_enabled" if usap is None else
        "device_config put activity_manager native_usap_pool_enabled " + shlex.quote(usap),
        f"rm -f {PAYLOAD_PATH}",
    ]
    deadline = time.monotonic() + timeout
    last = "ADB unavailable"
    while time.monotonic() < deadline:
        try:
            def run(shell_command):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise CleanupError("CVE cleanup deadline expired")
                result = subprocess.run([*adb, "shell", shell_command], capture_output=True, text=True,
                                        encoding="utf-8", errors="replace", timeout=min(5, remaining),
                                        **(child_options or {}))
                if result.returncode:
                    raise CleanupError(result.stderr.strip() or "ADB command failed")
                return result.stdout.rstrip("\r\n")
            for shell_command in commands:
                run(shell_command)
            if run(HIDDEN_GET) != (hidden if hidden is not None else "null"):
                raise CleanupError("hidden_api setting restore not confirmed")
            if run(USAP_GET) != (usap if usap is not None else "null"):
                raise CleanupError("USAP restore not confirmed")
            run(f"test ! -e {PAYLOAD_PATH}")
            path.unlink()
            return True
        except (CleanupError, OSError, subprocess.TimeoutExpired) as error:
            last = str(error)
        time.sleep(min(1, max(0, deadline - time.monotonic())))
    raise CleanupError(f"CVE cleanup pending for {state['serial']}: {last}. "
                       "Keep cve_cleanup.device.json; reconnect the original device and use --recover.")


class InjectionCleanup:
    def __init__(self, adb_exe="adb", path=DEFAULT_STATE):
        self.adb_exe = adb_exe
        self.path = Path(path)

    def __enter__(self):
        if self.path.exists():
            raise CleanupError("Previous CVE cleanup is pending; use --cleanup or workflow --recover first")
        serial = os.environ.get("ANDROID_SERIAL") or command([self.adb_exe], ["get-serialno"])
        if not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", serial) or serial == "unknown":
            raise CleanupError("An unambiguous device serial is required before mutation")
        self.serial = serial
        adb = [self.adb_exe, "-s", serial]
        hidden = command(adb, ["shell", HIDDEN_GET])
        usap = command(adb, ["shell", USAP_GET])
        # Never save/re-arm a leftover multiline injection as the 'original'.
        if "\n" in hidden or "\r" in hidden or usap not in ("null", "true", "false"):
            raise CleanupError("Unknown/multiline initial Android settings; no mutation attempted")
        state = {"version": 1, "serial": serial,
                 "hidden_api": None if hidden == "null" else hidden,
                 "usap": None if usap == "null" else usap}
        with self.path.open("x", encoding="utf-8") as stream:
            json.dump(state, stream)
            stream.flush()
            os.fsync(stream.fileno())
        return self

    def __exit__(self, *args):
        cleanup_pending(self.path, self.adb_exe)
