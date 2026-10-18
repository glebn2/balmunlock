"""Per-device session directories, with read-only discovery of legacy files."""
from __future__ import annotations
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re

SERIAL = re.compile(r"[A-Za-z0-9._:-]{1,128}\Z")
OWNER = "session.json"
ARTIFACTS = ("chkcode_backup.bin", "chkcode_backup.sha256",
             "chkcode_backup.recovery.bin", "chkcode_backup.recovery.sha256",
             "chkcode_backup.device.json", "cve_cleanup.device.json")


def valid_serial(serial):
    return isinstance(serial, str) and SERIAL.fullmatch(serial) is not None


def directory_key(serial):
    if not valid_serial(serial):
        raise ValueError("Invalid device serial; no session directory created")
    label = serial[:32].replace(":", "_")
    digest = hashlib.sha256(serial.encode("ascii")).hexdigest()
    return f"device-{label}-{digest}"


def regular_path(path):
    if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
        raise ValueError(f"Session paths must not be links: {path}")
    if path.resolve() != path.absolute():
        raise ValueError(f"Session path redirects outside its directory: {path}")


@dataclass(frozen=True)
class Session:
    key: str
    root: Path
    serial: str | None
    phase: str
    error: str | None = None


class SessionStore:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.directory = self.root / "sessions"

    @staticmethod
    def artifacts(root):
        return any((root / name).exists() or (root / name).is_symlink() for name in ARTIFACTS)

    def inspect(self, root, key):
        serials = set()
        phase = "new"
        try:
            regular_path(root)
            for name in (OWNER, "chkcode_backup.device.json", "cve_cleanup.device.json"):
                path = root / name
                if not path.exists() and not path.is_symlink():
                    continue
                regular_path(path)
                state = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(state, dict) or not valid_serial(state.get("serial")):
                    raise ValueError(f"Invalid session identity: {path.name}")
                serials.add(state["serial"])
                if state.get("version") != 1:
                    raise ValueError(f"Unsupported session file version: {path.name}")
                if name == "chkcode_backup.device.json":
                    phase = state.get("phase", "unknown")
                    if not isinstance(phase, str):
                        raise ValueError("Invalid saved phase")
                if name == "cve_cleanup.device.json":
                    phase = "cleanup_pending"
            if len(serials) > 1:
                raise ValueError("Session files belong to different phones")
            serial = next(iter(serials), None)
            if key != "legacy" and (not (root / OWNER).is_file() or directory_key(serial) != key):
                raise ValueError("Session directory identity is missing or does not match its name")
            if serial is None:
                phase = "backup_without_receipt"
            return Session(key, root, serial, phase)
        except (OSError, ValueError) as error:
            return Session(key, root, next(iter(serials)) if len(serials) == 1 else None,
                           "invalid", str(error))

    def entries(self):
        result = []
        if self.artifacts(self.root):
            result.append(self.inspect(self.root, "legacy"))
        regular_path(self.directory)
        if self.directory.exists():
            if not self.directory.is_dir():
                raise ValueError("sessions must be a directory; existing file retained")
            for path in sorted(self.directory.iterdir()):
                if path.name.startswith("device-"):
                    result.append(self.inspect(path, path.name))
        return result

    def select(self, key):
        entries = self.entries()
        found = [entry for entry in entries if entry.key == key]
        if not found:
            found = [entry for entry in entries if entry.serial == key]
        if len(found) != 1:
            raise ValueError("Saved session not found or ambiguous. Use --list-sessions and its session key.")
        return found[0]

    def create(self, serial):
        key = directory_key(serial)
        regular_path(self.directory)
        self.directory.mkdir(exist_ok=True)
        root = self.directory / key
        regular_path(root)
        root.mkdir(exist_ok=True)
        path = root / OWNER
        if not path.exists():
            if self.artifacts(root):
                raise ValueError("Unowned session files already exist; they were not overwritten")
            # Exclusive creation: a prior ownership record is never replaced.
            with path.open("x", encoding="utf-8") as stream:
                json.dump({"version": 1, "serial": serial}, stream, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
        session = self.inspect(root, key)
        if session.error:
            raise ValueError(session.error)
        return session
