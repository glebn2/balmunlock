"""Offline protocol, verification and resource cleanup regression tests."""
import importlib.util
import os
from pathlib import Path
import struct
import subprocess
import sys
import types
import unittest
from unittest.mock import Mock, call, patch

TOOLS = Path(__file__).resolve().parents[1] / "tools"


def load_fs():
    with patch.dict(sys.modules, {"diag_transport": types.SimpleNamespace(open_transport=Mock())}):
        spec = importlib.util.spec_from_file_location("fs_checks", TOOLS / "fs_sys_call.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.module = load_fs()
        self.tr = Mock(last_error=None)
        self.fs = self.module.FSDiag(self.tr)

    def test_oversized_write_refused_before_usb_even_with_optimization(self):
        with self.assertRaises(ValueError):
            self.fs.write(0, b"x" * 1025)
        self.tr.xfer.assert_not_called()
        program = """
import sys, types
sys.path.insert(0, sys.argv[1])
sys.modules['diag_transport'] = types.SimpleNamespace(open_transport=None)
from fs_sys_call import FSDiag
class NoUSB:
    def xfer(self, *args, **kwargs):
        raise RuntimeError('USB must never be called')
try:
    FSDiag(NoUSB()).write(0, bytes(1025))
except ValueError:
    print('blocked; optimize=' + str(sys.flags.optimize))
else:
    raise SystemExit(1)
"""
        result = subprocess.run([sys.executable, "-O", "-c", program, str(TOOLS)],
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("blocked; optimize=1", result.stdout)

    def test_control_read_retries_from_original_offset_without_writing(self):
        expected = b"LOOTBFCK"
        with patch.object(self.fs, "lseek", return_value=0) as seek, \
                patch.object(self.fs, "read", side_effect=[self.module.FSError("USB timeout"), b"short", expected]), \
                patch.object(self.fs, "write") as write, patch.object(self.module.time, "sleep"):
            self.assertEqual(self.fs.verify_at(2, 0, expected), expected)
            self.assertEqual(seek.call_args_list, [call(2, 0, 0)] * 3)
            write.assert_not_called()

    def test_failed_control_reads_stop_after_three_attempts(self):
        with patch.object(self.fs, "lseek", return_value=0) as seek, \
                patch.object(self.fs, "read", return_value=b"wrong") as read, \
                patch.object(self.fs, "write") as write, patch.object(self.module.time, "sleep"):
            with self.assertRaisesRegex(self.module.FSError, "3 попыток"):
                self.fs.verify_at(1, 0, b"LOOTBFCK")
            self.assertEqual(seek.call_count, 3)
            self.assertEqual(read.call_count, 3)
            write.assert_not_called()

    def test_read_does_not_blindly_retry_an_advancing_offset(self):
        self.tr.xfer.return_value = b"\x4b\xfc\x00\x20" + struct.pack("<HHII", 2, 0, 8, 0) + b"LOOTBFCK"
        self.assertEqual(self.fs.read(0, 8), b"LOOTBFCK")
        self.assertEqual(self.tr.xfer.call_args.kwargs, {"tries": 1})

    def test_truncated_protocol_read_is_not_a_success(self):
        self.tr.xfer.return_value = b"\x4b\xfc\x00\x20" + struct.pack("<HHII", 2, 0, 8, 0) + b"short"
        with self.assertRaises(self.module.FSError):
            self.fs.read(0, 8)

    def test_last_transport_error_is_included_in_fs_failure(self):
        self.tr.last_error = "USB device disconnected; errno=19"
        self.tr.xfer.return_value = None
        with self.assertRaisesRegex(self.module.FSError, "USB device disconnected; errno=19"):
            self.fs.read(0, 8)

    def test_zero_or_partial_write_file_stops_without_spinning(self):
        for count in (0, 4):
            with patch.object(self.fs, "open", return_value=1), \
                    patch.object(self.fs, "lseek", return_value=0), \
                    patch.object(self.fs, "write", return_value=count) as write, \
                    patch.object(self.fs, "close") as close:
                with self.assertRaises(self.module.FSError):
                    self.fs.write_file("fake", b"LOOTBFCK")
                write.assert_called_once()
                close.assert_called_once_with(1)

    def test_fs_cli_closes_transport_after_read_failure(self):
        with patch.object(self.module, "open_transport", return_value=self.tr), \
                patch.object(self.module.FSDiag, "read_file", side_effect=self.module.FSError("read failed")), \
                patch.object(sys, "argv", ["fs_sys_call", "fake"]):
            with self.assertRaises(self.module.FSError):
                self.module.main()
        self.tr.close.assert_called_once()

    def test_new_write_file_call_seeks_zero_after_a_partial_previous_call(self):
        positions, starts, closed = {}, [], []
        storage = bytearray(b"ABCDEFGH")
        next_fd = iter((1, 2))
        partial = [True]
        def opened(*args):
            fd = next(next_fd)
            positions[fd] = 0
            return fd
        def seek(fd, offset, whence):
            positions[fd] = offset
            return offset
        def write(fd, data):
            starts.append(positions[fd])
            count = 4 if partial.pop() else len(data)
            storage[positions[fd]:positions[fd] + count] = data[:count]
            positions[fd] += count
            return count
        def close(fd):
            closed.append(fd)
            del positions[fd]
        with patch.object(self.fs, "open", side_effect=opened), \
                patch.object(self.fs, "lseek", side_effect=seek), \
                patch.object(self.fs, "write", side_effect=write), \
                patch.object(self.fs, "close", side_effect=close):
            with self.assertRaises(self.module.FSError):
                self.fs.write_file("fake", b"LOOTBFCK")
            partial.append(False)
            self.assertEqual(self.fs.write_file("fake", b"12345678"), 8)
        self.assertEqual(starts, [0, 0])
        self.assertEqual(storage, b"12345678")
        self.assertEqual(closed, [1, 2])


class FakeUSBError(Exception):
    def __init__(self, message, errno=None):
        super().__init__(message)
        self.errno = errno
        self.backend_error_code = None


class TransportDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        core = types.ModuleType("usb.core")
        core.USBError = FakeUSBError
        core.find = Mock()
        util = types.ModuleType("usb.util")
        util.claim_interface, util.release_interface, util.dispose_resources = Mock(), Mock(), Mock()
        usb = types.ModuleType("usb")
        usb.core, usb.util = core, util
        with patch.dict(sys.modules, {"usb": usb, "usb.core": core, "usb.util": util}):
            spec = importlib.util.spec_from_file_location("transport_checks", TOOLS / "diag_transport.py")
            self.module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(self.module)
        self.core, self.util = core, util
        self.tr = self.module.DiagTransport.__new__(self.module.DiagTransport)
        self.tr.quiet = True
        self.tr.dev, self.tr.ep_in, self.tr.ep_out = Mock(), Mock(), Mock()
        self.tr._use_crc = True
        self.tr.drain = Mock()

    def test_failed_transfer_retains_usb_error(self):
        self.tr.dev.write.side_effect = FakeUSBError("device disconnected", errno=19)
        self.assertIsNone(self.tr.xfer(b"probe", tries=1))
        self.assertIn("device disconnected", self.tr.last_error)
        self.assertIn("errno=19", self.tr.last_error)

    def test_receive_error_is_not_replaced_by_generic_timeout(self):
        self.tr.ep_in.read.side_effect = FakeUSBError("device disappeared during receive", errno=19)
        with patch.object(self.module.time, "monotonic", side_effect=[0, 0, 2]):
            self.assertIsNone(self.tr.xfer(b"probe", timeout=1000, tries=1))
        self.assertIn("device disappeared during receive", self.tr.last_error)

    def test_successful_transfer_clears_previous_error(self):
        self.tr.last_error = "old error"
        self.tr.ep_in.read.return_value = self.module.frame(b"\x00reply")
        with patch.object(self.module.time, "monotonic", side_effect=[0, 0]):
            self.assertEqual(self.tr.xfer(b"probe", tries=1), b"\x00reply")
        self.assertIsNone(self.tr.last_error)

    def test_dispose_runs_even_when_release_interface_fails(self):
        self.util.release_interface.side_effect = FakeUSBError("device gone")
        self.tr.close()
        self.tr.close()
        self.util.release_interface.assert_called_once()
        self.util.dispose_resources.assert_called_once_with(self.tr.dev)

    def test_missing_endpoints_release_claimed_interface(self):
        device = Mock(serial_number="SERIAL")
        device.get_active_configuration.return_value = {(0, 0): []}
        device.is_kernel_driver_active.return_value = False
        self.core.find.return_value = [device]
        with patch.dict(os.environ, {"ANDROID_SERIAL": "SERIAL"}):
            with self.assertRaisesRegex(RuntimeError, "endpoints"):
                self.module.open_transport()
        self.util.release_interface.assert_called_once_with(device, 0)
        self.util.dispose_resources.assert_called_once_with(device)
