"""Host-only mocks for the read-only DIAG and original-image validation helpers."""
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import chkcode_backup as backup
import diag_probe
import verify_chkcode


class ProbeTests(unittest.TestCase):
    def run_probe(self, transport=None, error=None):
        class IdentityError(RuntimeError):
            pass
        class Unavailable(RuntimeError):
            pass
        opener = Mock(return_value=transport, side_effect=error)
        module = types.SimpleNamespace(DiagIdentityError=IdentityError, DiagUnavailable=Unavailable, open_transport=opener)
        with patch.dict(sys.modules, {"diag_transport": module}), patch.object(sys, "argv", ["probe"]):
            return diag_probe.main()

    def test_probe_success_closes_transport_without_opening_a_partition(self):
        transport = Mock()
        transport.ping.return_value = True
        self.assertEqual(self.run_probe(transport), 0)
        transport.close.assert_called_once()
        transport.ping.assert_called_once()

    def test_driver_error_is_retryable_without_any_write(self):
        self.assertEqual(self.run_probe(error=ValueError("no langid")), 2)

    def test_unresponsive_diag_is_distinct_from_a_missing_driver(self):
        transport = Mock()
        transport.ping.return_value = False
        self.assertEqual(self.run_probe(transport), 4)
        transport.close.assert_called_once()


class LiveOriginalTests(unittest.TestCase):
    def test_two_identical_full_reads_are_required_and_no_write_method_is_called(self):
        original = bytes(backup.CHKCODE_SIZE)
        with tempfile.TemporaryDirectory(prefix=".test-balmunlock-", dir=Path(__file__).resolve().parents[1]) as temporary:
            image = Path(temporary) / "original.bin"
            backup.save_backup(image, original)
            transport, fs = Mock(), Mock()
            fs.read_file.return_value = original
            with patch.dict(sys.modules, {"diag_transport": types.SimpleNamespace(open_transport=lambda: transport),
                                          "fs_sys_call": types.SimpleNamespace(FSDiag=lambda tr: fs)}), \
                    patch.object(sys, "argv", ["verify", "--backup", str(image)]):
                self.assertEqual(verify_chkcode.main(), 0)
            self.assertEqual(fs.read_file.call_count, 2)
            fs.write.assert_not_called()
            fs.write_file.assert_not_called()
            transport.close.assert_called_once()

    def test_short_or_changed_second_read_refuses_and_closes_transport(self):
        original = bytes(backup.CHKCODE_SIZE)
        with tempfile.TemporaryDirectory(prefix=".test-balmunlock-", dir=Path(__file__).resolve().parents[1]) as temporary:
            image = Path(temporary) / "original.bin"
            backup.save_backup(image, original)
            transport, fs = Mock(), Mock()
            fs.read_file.side_effect = [original, original[:-1]]
            with patch.dict(sys.modules, {"diag_transport": types.SimpleNamespace(open_transport=lambda: transport),
                                          "fs_sys_call": types.SimpleNamespace(FSDiag=lambda tr: fs)}), \
                    patch.object(sys, "argv", ["verify", "--backup", str(image)]), self.assertRaises(ValueError):
                verify_chkcode.main()
            fs.write.assert_not_called()
            transport.close.assert_called_once()

    def test_bad_local_backup_refuses_before_usb_import_or_transport(self):
        with tempfile.TemporaryDirectory(prefix=".test-balmunlock-", dir=Path(__file__).resolve().parents[1]) as temporary:
            image = Path(temporary) / "bad.bin"
            image.write_bytes(b"truncated")
            opener = Mock()
            with patch.dict(sys.modules, {"diag_transport": types.SimpleNamespace(open_transport=opener)}), \
                    patch.object(sys, "argv", ["verify", "--backup", str(image)]), self.assertRaises(ValueError):
                verify_chkcode.main()
            opener.assert_not_called()
