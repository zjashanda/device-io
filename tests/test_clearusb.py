"""clearusb facade tests.

Every PowerShell call is faked: ``device_io.clearusb.host_os`` and
``device_io.clearusb.subprocess.run`` are patched, so these tests prove the
argument contract and the safety defaults without touching Windows.
"""

from __future__ import annotations

import json
import unittest
from unittest import mock

from device_io import clearusb

PAYLOAD = {
    "items": [
        {
            "instance_id": "USB\\VID_1A86&PID_55D3&MI_00\\7&1C8E5B0D&0&0000",
            "class": "Ports",
            "friendly_name": "USB-SERIAL CH344 (COM7)",
            "parent": "USB\\VID_1A86&PID_55D3\\5&2B4D5C3D&0&2",
            "reason": "hidden_usb_port",
            "removed": False,
            "error": None,
        }
    ],
    "summary": {"candidates": 1, "removed": 0, "failed": 0},
}


def completed(payload=PAYLOAD, returncode=0, stderr=""):
    return mock.Mock(
        returncode=returncode,
        stdout=json.dumps(payload),
        stderr=stderr,
    )


def plan_with(host_os="windows", shell="/usr/bin/pwsh", **kwargs):
    """Run plan() with the OS probe and the subprocess faked."""
    payload = kwargs.pop("payload", PAYLOAD)
    returncode = kwargs.pop("returncode", 0)
    with mock.patch.object(clearusb, "host_os", return_value=host_os):
        with mock.patch.object(clearusb, "powershell", return_value=shell):
            with mock.patch.object(
                clearusb.subprocess,
                "run",
                return_value=completed(payload, returncode=returncode),
            ) as run:
                document = clearusb.plan(**kwargs)
    return document, run


def command_of(run):
    return run.call_args[0][0]


class PreviewDefaultTest(unittest.TestCase):
    def test_preview_is_the_default(self):
        document, run = plan_with()
        self.assertTrue(document["supported"])
        self.assertFalse(document["apply"])
        self.assertEqual(document["os"], "windows")
        command = command_of(run)
        self.assertIn("-Json", command)
        self.assertNotIn("-Apply", command)
        self.assertEqual(run.call_args[1]["timeout"], clearusb.DEFAULT_TIMEOUT)

    def test_preview_reports_candidates_without_removal(self):
        document, _run = plan_with()
        self.assertEqual(document["summary"]["candidates"], 1)
        self.assertEqual(document["summary"]["removed"], 0)
        self.assertEqual(document["summary"]["failed"], 0)

    def test_powershell_invocation_flags(self):
        _document, run = plan_with()
        command = command_of(run)
        self.assertEqual(command[0], "/usr/bin/pwsh")
        self.assertIn("-NoProfile", command)
        self.assertIn("-NonInteractive", command)
        self.assertIn("-ExecutionPolicy", command)
        self.assertIn("Bypass", command)
        self.assertIn("-File", command)
        self.assertTrue(command[-2].endswith("Clear-HiddenDevice.ps1"))


class ApplyPropagationTest(unittest.TestCase):
    def test_apply_flag_is_forwarded(self):
        document, run = plan_with(apply=True)
        self.assertTrue(document["apply"])
        self.assertIn("-Apply", command_of(run))

    def test_board_filter_is_forwarded(self):
        document, run = plan_with(board="1a86:7523")
        self.assertTrue(document["supported"])
        command = command_of(run)
        index = command.index("-Board")
        self.assertEqual(command[index + 1], "1a86:7523")

    def test_apply_and_board_together(self):
        _document, run = plan_with(apply=True, board="1a86:55d3")
        command = command_of(run)
        self.assertIn("-Apply", command)
        index = command.index("-Board")
        self.assertEqual(command[index + 1], "1a86:55d3")
        self.assertLess(command.index("-Apply"), index)


class UnsupportedPlatformTest(unittest.TestCase):
    def test_linux_reports_unsupported_without_running_powershell(self):
        with mock.patch.object(clearusb, "host_os", return_value="linux"):
            with mock.patch.object(clearusb.subprocess, "run") as run:
                document = clearusb.plan(apply=True)
        self.assertFalse(document["supported"])
        self.assertTrue(document["apply"])
        self.assertIn("Windows", document["reason"])
        self.assertEqual(document["items"], [])
        run.assert_not_called()

    def test_missing_powershell_reports_unsupported(self):
        with mock.patch.object(clearusb, "host_os", return_value="windows"):
            with mock.patch.object(clearusb, "powershell", return_value=None):
                with mock.patch.object(clearusb.subprocess, "run") as run:
                    document = clearusb.plan()
        self.assertFalse(document["supported"])
        self.assertIn("PowerShell", document["reason"])
        self.assertEqual(document["os"], "windows")
        run.assert_not_called()


class FailureHandlingTest(unittest.TestCase):
    def test_non_zero_exit_raises(self):
        with self.assertRaises(clearusb.ClearUsbError):
            plan_with(returncode=1)

    def test_failed_items_are_counted(self):
        payload = {
            "items": [
                dict(PAYLOAD["items"][0], error="pnputil failed", removed=False),
                dict(PAYLOAD["items"][0], instance_id="USB\\X", removed=True),
            ],
            "summary": {"candidates": 2, "removed": 1, "failed": 1},
        }
        document, _run = plan_with(payload=payload, apply=True)
        self.assertEqual(document["summary"]["failed"], 1)
        self.assertEqual(document["summary"]["removed"], 1)


if __name__ == "__main__":
    unittest.main()
