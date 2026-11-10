"""Windows backend tests: pure normalisation from recorded raw fixtures.

No Windows hardware is involved and no subprocess reaches a real PowerShell:
the collector call is asserted against ``subprocess.run`` patched with a fake.
"""

from __future__ import annotations

import json
import pathlib
import unittest
from unittest import mock

from device_io.backends import windows as windows_backend
from device_io.backends.base import BackendError, BackendOptions, BackendUnavailable

FIXTURE_DIR = pathlib.Path(__file__).resolve().parent / "fixtures"
CONTAINER_CH344 = "{6F2A1C4E-1B7D-4B6A-9D33-5F2C8A1E7B01}"
CONTAINER_VCOM = "{9B4E7D21-3C58-4A90-8F16-2D7C5B9E4A02}"


def load_raw(name):
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


def completed(stdout, returncode=0, stderr=""):
    return mock.Mock(returncode=returncode, stdout=stdout, stderr=stderr)


class Ch344QuadTest(unittest.TestCase):
    """The fixture reproduces the documented COM/interface pairing exactly."""

    def setUp(self):
        self.result = windows_backend.normalize_raw(load_raw("windows_ch344_raw.json"))

    def test_backend_and_os_identity(self):
        self.assertEqual(self.result.backend, "windows")
        self.assertEqual(self.result.os, "windows")

    def test_ports_group_and_sort_by_interface_number(self):
        pairs = [
            (port.device, port.interface, port.interface_number)
            for port in self.result.ports
        ]
        self.assertEqual(
            pairs,
            [
                ("COM13", "SERIAL-A", 0),
                ("COM15", "SERIAL-B", 2),
                ("COM18", "SERIAL-C", 4),
                ("COM12", "SERIAL-D", 6),
            ],
        )

    def test_one_physical_board(self):
        keys = {port.board_key for port in self.result.ports}
        self.assertEqual(keys, {CONTAINER_CH344})

    def test_usb_identity_and_transport(self):
        port = self.result.ports[0]
        self.assertEqual(port.vid, "1a86")
        self.assertEqual(port.pid, "55d3")
        self.assertEqual(port.transport, "usb")
        self.assertEqual(port.kind, "serial")
        self.assertTrue(port.present)

    def test_labels_are_product_aware(self):
        labels = [port.label for port in self.result.ports]
        self.assertEqual(
            labels,
            [
                "CH344 Quad_Serial A",
                "CH344 Quad_Serial B",
                "CH344 Quad_Serial C",
                "CH344 Quad_Serial D",
            ],
        )
        self.assertEqual(self.result.ports[0].product, "CH344 Quad_Serial")

    def test_driver_family_and_status(self):
        for port in self.result.ports:
            self.assertEqual(port.driver.family, "ch34x")
            self.assertEqual(port.driver.status, "ok")
            self.assertEqual(port.driver.id, "CH343SER")

    def test_location_is_usb_path(self):
        port = self.result.ports[0]
        self.assertEqual(port.location, "Port_#0003.Hub_#0005")
        self.assertEqual(port.usb_path, port.location)

    def test_no_audio_endpoints(self):
        self.assertEqual(self.result.audio, [])


class ListenAiVcomTest(unittest.TestCase):
    def setUp(self):
        self.result = windows_backend.normalize_raw(load_raw("windows_listenai_raw.json"))

    def test_single_vcom_port(self):
        self.assertEqual(len(self.result.ports), 1)
        port = self.result.ports[0]
        self.assertEqual(port.device, "COM14")
        self.assertEqual(port.interface, "VCOM")
        self.assertIsNone(port.interface_number)
        self.assertEqual(port.label, "ListenAI VCOM")
        self.assertEqual(port.product, "ListenAI VCOM")
        self.assertEqual(port.vid, "8765")
        self.assertEqual(port.pid, "5678")
        self.assertEqual(port.serial, "9_2A847557_0_0000")

    def test_driver_is_inbox_usbser(self):
        port = self.result.ports[0]
        self.assertEqual(port.driver.family, "listenai-vcom")
        self.assertEqual(port.driver.status, "ok")
        self.assertEqual(port.driver.id, "usbser")

    def test_audio_sorted_and_joined_to_the_board(self):
        self.assertEqual(
            [item.kind for item in self.result.audio],
            ["audio-capture", "audio-render"],
        )
        capture, render = self.result.audio
        self.assertEqual(capture.channels, 8)
        self.assertEqual(render.channels, 2)
        self.assertEqual(capture.board_key, CONTAINER_VCOM)
        self.assertEqual(render.board_key, self.result.ports[0].board_key)
        self.assertEqual((capture.vid, capture.pid), ("8765", "5678"))
        self.assertTrue(capture.endpoint_id.startswith("SWD\\MMDEVAPI\\"))
        self.assertEqual(capture.name, "麦克风 (ListenAI Audio)")
        self.assertEqual(render.name, "扬声器 (ListenAI Audio)")
        self.assertEqual(capture.extra["friendly_name"], "Capture 8ch 麦克风 (ListenAI Audio)")
        self.assertEqual(render.extra["friendly_name"], "Render 2ch 扬声器 (ListenAI Audio)")


class GhostFixtureTest(unittest.TestCase):
    def test_ghost_is_excluded_by_default(self):
        result = windows_backend.normalize_raw(load_raw("windows_ghost_raw.json"))
        self.assertEqual([port.device for port in result.ports], ["COM14"])
        self.assertTrue(result.ports[0].present)

    def test_include_disabled_keeps_ghost_marked_absent(self):
        result = windows_backend.normalize_raw(
            load_raw("windows_ghost_raw.json"),
            options=BackendOptions(include_disabled=True),
        )
        by_device = {port.device: port for port in result.ports}
        self.assertEqual(set(by_device), {"COM14", "COM7"})
        self.assertFalse(by_device["COM7"].present)
        self.assertTrue(by_device["COM14"].present)
        self.assertTrue(by_device["COM7"].interface.startswith("SERIAL-"))

    def test_board_keys_differ_across_boards(self):
        result = windows_backend.normalize_raw(
            load_raw("windows_ghost_raw.json"),
            options=BackendOptions(include_disabled=True),
        )
        keys = {port.board_key for port in result.ports}
        self.assertEqual(keys, {CONTAINER_CH344, CONTAINER_VCOM})


class NoUsbFixtureTest(unittest.TestCase):
    def test_no_ports_and_a_clear_warning(self):
        result = windows_backend.normalize_raw(load_raw("windows_no_usb_raw.json"))
        self.assertEqual(result.ports, [])
        self.assertEqual(result.audio, [])
        self.assertTrue(any("no serial ports" in warning for warning in result.warnings))


class DriverStatusTest(unittest.TestCase):
    def _with_service(self, service):
        raw = load_raw("windows_ch344_raw.json")
        for device in raw["devices"]:
            if device["instance_id"].endswith("&MI_00\\7&1C8E5B0D&0&0000"):
                device["service"] = service
        return windows_backend.normalize_raw(raw)

    def test_empty_service_is_missing(self):
        result = self._with_service("")
        port = result.ports[0]
        self.assertEqual(port.driver.status, "missing")
        self.assertEqual(port.driver.family, "ch34x")

    def test_wrong_service_for_the_family_is_mismatch(self):
        result = self._with_service("usbser")
        port = result.ports[0]
        self.assertEqual(port.driver.family, "ch34x")
        self.assertEqual(port.driver.status, "mismatch")
        self.assertIn("service=usbser", port.driver.evidence)


class AudioBoardKeyTest(unittest.TestCase):
    def test_audio_without_a_matching_board_has_no_board_key(self):
        raw = {
            "schema": "ark.device-io.raw.v1",
            "os": "windows",
            "generated_at": "2026-11-10 21:00:00",
            "devices": [
                {
                    "instance_id": "SWD\\MMDEVAPI\\{DEADBEEF-0000-4000-8000-000000000009}",
                    "class": "AudioEndpoint",
                    "friendly_name": "Capture 2ch Onboard Microphone",
                    "status": "OK",
                    "present": True,
                    "service": "",
                    "parent": "HDAUDIO\\FUNC_01&VEN_10EC&DEV_0256\\4&2A3B4C5D&0&0001",
                    "container_id": "{DEADBEEF-0000-4000-8000-000000000001}",
                    "location": "",
                    "hardware_ids": ["SWD\\MMDEVAPI"],
                    "manufacturer": "Microsoft",
                    "device_desc": "Audio Endpoint",
                }
            ],
            "serial_ports": [],
            "audio": [
                {
                    "instance_id": "SWD\\MMDEVAPI\\{DEADBEEF-0000-4000-8000-000000000009}",
                    "name": "Capture 2ch Onboard Microphone",
                    "state": "OK",
                    "class": "AudioEndpoint",
                }
            ],
            "warnings": [],
        }
        result = windows_backend.normalize_raw(raw)
        self.assertEqual(len(result.audio), 1)
        self.assertIsNone(result.audio[0].board_key)
        self.assertEqual(result.audio[0].channels, 2)
        self.assertEqual(result.audio[0].kind, "audio-capture")

    def test_include_audio_false_drops_endpoints(self):
        result = windows_backend.normalize_raw(
            load_raw("windows_listenai_raw.json"),
            options=BackendOptions(include_audio=False),
        )
        self.assertEqual(result.audio, [])
        self.assertEqual(len(result.ports), 1)


class NormalizeContractTest(unittest.TestCase):
    def test_rejects_wrong_raw_schema(self):
        with self.assertRaises(BackendError):
            windows_backend.normalize_raw({"schema": "something-else"})

    def test_rejects_non_object(self):
        with self.assertRaises(BackendError):
            windows_backend.normalize_raw([])  # type: ignore[arg-type]


class AvailabilityTest(unittest.TestCase):
    def test_not_available_off_windows_and_never_raises(self):
        backend = windows_backend.WindowsBackend()
        with mock.patch.object(windows_backend.platform, "system", return_value="Linux"):
            ok, reason = backend.available()
        self.assertFalse(ok)
        self.assertIn("Linux", reason)

    def test_windows_without_powershell_is_unavailable(self):
        backend = windows_backend.WindowsBackend()
        with mock.patch.object(windows_backend.platform, "system", return_value="Windows"):
            with mock.patch.object(windows_backend, "powershell", return_value=None):
                ok, reason = backend.available()
        self.assertFalse(ok)
        self.assertIn("PowerShell", reason)

    def test_windows_with_powershell_is_available(self):
        backend = windows_backend.WindowsBackend()
        with mock.patch.object(windows_backend.platform, "system", return_value="Windows"):
            with mock.patch.object(
                windows_backend, "powershell", return_value="/usr/bin/pwsh"
            ):
                ok, reason = backend.available()
        self.assertTrue(ok)
        self.assertIn("/usr/bin/pwsh", reason)

    def test_collect_raises_unavailable_off_windows(self):
        backend = windows_backend.WindowsBackend()
        with mock.patch.object(windows_backend.platform, "system", return_value="Linux"):
            with self.assertRaises(BackendUnavailable):
                backend.collect(BackendOptions())


class CollectOutputTest(unittest.TestCase):
    """The subprocess is faked; this only proves parsing and the CLI contract."""

    def test_parses_last_json_object_after_warnings(self):
        raw = load_raw("windows_listenai_raw.json")
        noise = "WARNING: PnP cache slow\nGet-PnpDevice: one instance skipped\n"
        fake = completed(noise + json.dumps(raw, ensure_ascii=False))
        backend = windows_backend.WindowsBackend()
        with mock.patch.object(windows_backend.platform, "system", return_value="Windows"):
            with mock.patch.object(
                windows_backend, "powershell", return_value="/usr/bin/pwsh"
            ):
                with mock.patch.object(
                    windows_backend.subprocess, "run", return_value=fake
                ) as run:
                    result = backend.collect(BackendOptions())
        self.assertEqual(len(result.ports), 1)
        self.assertEqual(result.backend, "windows")
        self.assertEqual(len(result.sources), 1)
        self.assertTrue(result.sources[0].startswith("script:"))
        self.assertTrue(result.sources[0].endswith("Collect-DeviceIo.ps1"))

        command = run.call_args[0][0]
        self.assertEqual(command[0], "/usr/bin/pwsh")
        self.assertIn("-NoProfile", command)
        self.assertIn("-NonInteractive", command)
        self.assertIn("-ExecutionPolicy", command)
        self.assertIn("Bypass", command)
        self.assertIn("-File", command)
        self.assertIn("-Json", command)
        self.assertIn("Collect-DeviceIo.ps1", command[-2])
        self.assertEqual(run.call_args[1]["timeout"], 120)

    def test_non_zero_exit_is_a_backend_error(self):
        backend = windows_backend.WindowsBackend()
        fake = completed("", returncode=3, stderr="collector exploded")
        with mock.patch.object(windows_backend.platform, "system", return_value="Windows"):
            with mock.patch.object(
                windows_backend, "powershell", return_value="/usr/bin/pwsh"
            ):
                with mock.patch.object(
                    windows_backend.subprocess, "run", return_value=fake
                ):
                    with self.assertRaises(BackendError) as caught:
                        backend.collect(BackendOptions())
        self.assertIn("collector exploded", str(caught.exception))

    def test_unparseable_output_is_a_backend_error(self):
        backend = windows_backend.WindowsBackend()
        fake = completed("not json at all")
        with mock.patch.object(windows_backend.platform, "system", return_value="Windows"):
            with mock.patch.object(
                windows_backend, "powershell", return_value="/usr/bin/pwsh"
            ):
                with mock.patch.object(
                    windows_backend.subprocess, "run", return_value=fake
                ):
                    with self.assertRaises(BackendError):
                        backend.collect(BackendOptions())


if __name__ == "__main__":
    unittest.main()
