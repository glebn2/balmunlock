"""Protect download contents from accidental inclusion of local project material."""
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "build-support"))
from runtime_bundle import check_runtime_layout, is_runtime_file


class RuntimeBundleTests(unittest.TestCase):
    def test_extraneous_docs_backups_and_source_archives_are_rejected(self):
        for name in ("README.md", "README.en.md", "BUILD-INFO.json", "recovery/INSTALL.md",
                     "recovery/CHANGELOG-20261025.md", "licenses/libusb-sources/libusb-source.zip",
                     "sessions/backup.img", "_internal/.local-notes/report.txt", "../balmunlock.exe",
                     "/balmunlock.exe", "platform-tools/source.properties"):
            with self.subTest(name=name):
                self.assertFalse(is_runtime_file(name))

    def test_dependency_resources_notices_and_recovery_are_retained(self):
        for name in ("balmunlock.exe", "unmuda.exe", "_internal/python313.dll",
                     "_internal/base_library.zip", "_internal/licenses/Python-LICENSE.txt",
                     "_internal/pyusb-1.3.1.dist-info/METADATA", "platform-tools/adb.exe",
                     "platform-tools/fastboot.exe", "platform-tools/NOTICE.txt",
                     "tools/balmuda-fastboot.cfg", "recovery/TWRP.img"):
            with self.subTest(name=name):
                self.assertTrue(is_runtime_file(name))

    def test_an_unexpected_file_prevents_packaging(self):
        with tempfile.TemporaryDirectory(prefix=".test-balmunlock-", dir=ROOT) as temporary:
            directory = Path(temporary)
            (directory / "balmunlock.exe").write_bytes(b"executable")
            check_runtime_layout(directory)
            (directory / "PRIVATE_CHANGELOG.md").write_text("local project links")
            with self.assertRaisesRegex(ValueError, "PRIVATE_CHANGELOG"):
                check_runtime_layout(directory)
