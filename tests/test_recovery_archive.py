"""Host-only checks of the fixed release ZIP; no network or phone calls."""
import hashlib
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import warnings
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "build-support"))
import recovery_archive as bundle


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix=".test-balmunlock-", dir=Path(__file__).resolve().parents[1])
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "recovery.zip"
        self.data = b"test-image"

    def make(self, names=None, data=None):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            with zipfile.ZipFile(self.path, "w") as archive:
                for name in names or [bundle.IMAGE_NAME]:
                    archive.writestr(name, self.data if data is None else data)
        self.addCleanup(patch.stopall)
        patch.object(bundle, "ARCHIVE_SHA256", bundle.file_hash(self.path)).start()
        patch.object(bundle, "IMAGE_SIZE", len(self.data)).start()
        patch.object(bundle, "IMAGE_SHA256", hashlib.sha256(self.data).hexdigest()).start()

    def test_valid_archive_reads_only_exact_image_name(self):
        self.make(names=[bundle.IMAGE_NAME, "../../unexpected"])
        self.assertEqual(bundle.read_image(self.path), self.data)
        self.assertFalse((self.path.parent / "unexpected").exists())

    def test_created_archive_contains_only_image_and_is_deterministic(self):
        image = self.path.parent / "source.img"
        image.write_bytes(self.data)
        other = self.path.parent / "second.zip"
        with patch.object(bundle, "image_path", return_value=image), \
                patch.object(bundle, "verify_image", return_value=image):
            bundle.create_archive(self.path)
            bundle.create_archive(other)
        self.assertEqual(self.path.read_bytes(), other.read_bytes())
        with zipfile.ZipFile(self.path) as archive:
            self.assertEqual(archive.namelist(), [bundle.IMAGE_NAME])
            self.assertEqual(archive.read(bundle.IMAGE_NAME), self.data)

    def test_tampered_archive_is_rejected_before_opening(self):
        self.make()
        self.path.write_bytes(self.path.read_bytes() + b"changed")
        with self.assertRaisesRegex(ValueError, "archive SHA-256"):
            bundle.read_image(self.path)

    def test_missing_or_duplicate_image_is_rejected(self):
        for names in (["wrong.img"], [bundle.IMAGE_NAME, bundle.IMAGE_NAME]):
            with self.subTest(names=names):
                self.make(names=names)
                with self.assertRaisesRegex(ValueError, "missing, duplicated"):
                    bundle.read_image(self.path)

    def test_wrong_embedded_size_or_hash_is_rejected(self):
        for data in (b"short", b"bad--image"):
            with self.subTest(data=data):
                self.make(data=data)
                with self.assertRaises(ValueError):
                    bundle.read_image(self.path)
