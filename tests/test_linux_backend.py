"""Unit tests for the Linux sysfs backend.

Every test builds a fake sysfs tree inside a temporary directory, so the whole
enumeration path (tty glob, ``:N.M`` interface parsing, upstream board key,
driver link, audio cards, procfs PCM parsing) is exercised on any machine with
no USB hardware and no root. Symlinks are created at runtime only; nothing
symlink-shaped is committed, and the real ``/sys`` is never touched.
"""

from __future__ import annotations

import os
import platform
import tempfile
import unittest
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from device_io import core
from device_io.backends.base import BackendOptions
from device_io.backends.linux import LinuxBackend
from device_io.models import Snapshot


# ---------------------------------------------------------------------------
# fake sysfs builder
# ---------------------------------------------------------------------------


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8")


def _symlink(link: Path, target: Path) -> None:
    link.parent.mkdir(parents=True, exist_ok=True)
    if link.is_symlink() or link.exists():
        link.unlink()
    os.symlink(str(target), str(link))


def add_usb_board(
    root: Path,
    upstream: str,
    interfaces: List[Tuple[int, str]],
    vid: str,
    pid: str,
    product: Optional[str] = None,
    manufacturer: Optional[str] = None,
    serial: Optional[str] = None,
    driver: Optional[str] = "ch341-uart",
    uevent_only: bool = False,
) -> Path:
    """Create one USB device with one tty per ``(interface_number, tty_name)``.

    Layout mirrors real sysfs:

        devices/usb1/<upstream>/{idVendor,idProduct,uevent}
        devices/usb1/<upstream>/<upstream>:1.N/{uevent,driver,ttyXXX/device}
        class/tty/<tty>                            -> .../<upstream>:1.N/<tty>

    ``uevent_only`` omits the ``idVendor``/``idProduct`` files so the walk-up
    has to fall back to ``PRODUCT=vid/pid`` in ``uevent``.
    """
    usb = root / "devices" / "usb1" / upstream
    usb.mkdir(parents=True, exist_ok=True)
    if not uevent_only:
        _write(usb / "idVendor", vid)
        _write(usb / "idProduct", pid)
    if product is not None:
        _write(usb / "product", product)
    if manufacturer is not None:
        _write(usb / "manufacturer", manufacturer)
    if serial is not None:
        _write(usb / "serial", serial)
    _write(usb / "uevent", "PRODUCT={0}/{1}/0100".format(vid, pid))

    for number, tty in interfaces:
        iface = usb / "{0}:1.{1}".format(upstream, number)
        _write(
            iface / "uevent",
            "DEVTYPE=usb_interface\nbInterfaceNumber={0:02x}".format(number),
        )
        if driver:
            driver_dir = root / "bus" / "usb-serial" / "drivers" / driver
            driver_dir.mkdir(parents=True, exist_ok=True)
            _symlink(iface / "driver", driver_dir)
        tty_dir = iface / tty
        tty_dir.mkdir(parents=True, exist_ok=True)
        _symlink(tty_dir / "device", iface)
        _symlink(root / "class" / "tty" / tty, tty_dir)
    return usb


def add_audio_card(
    root: Path,
    card: int,
    upstream: Optional[str],
    card_id: str,
    interface_number: int = 1,
) -> Path:
    """Create ``class/sound/cardN`` pointing at a USB interface of ``upstream``."""
    if upstream:
        usb = root / "devices" / "usb1" / upstream
        iface = usb / "{0}:1.{1}".format(upstream, interface_number)
        iface.mkdir(parents=True, exist_ok=True)
        device_dir = iface
    else:
        device_dir = root / "devices" / "platform" / "sound" / "card{0}".format(card)
        device_dir.mkdir(parents=True, exist_ok=True)

    card_dir = root / "class" / "sound" / "card{0}".format(card)
    card_dir.mkdir(parents=True, exist_ok=True)
    _write(card_dir / "id", card_id)
    _write(card_dir / "uevent", "DEVNAME=snd/card{0}".format(card))
    _symlink(card_dir / "device", device_dir)
    return card_dir


def add_proc_pcm(tmp: Path, lines: List[str]) -> str:
    """Write a fake ``/proc/asound/pcm`` and return its directory."""
    proc_asound = tmp / "proc" / "asound"
    proc_asound.mkdir(parents=True, exist_ok=True)
    (proc_asound / "pcm").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(proc_asound)


def run_backend(root: Path, **extra) -> object:
    return LinuxBackend().collect(BackendOptions(sysfs_root=str(root), extra=dict(extra)))


def boards_of(result) -> List[Dict[str, object]]:
    snapshot = Snapshot(
        backend=result.backend,
        os=result.os,
        ports=list(result.ports),
        audio=list(result.audio),
        warnings=list(result.warnings),
    )
    return snapshot.boards()


class FakeSysfsTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.root = self.tmp / "sys"
        self.root.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        self._tmp.cleanup()


# ---------------------------------------------------------------------------
# 1. CH344 quad adapter
# ---------------------------------------------------------------------------


class CH344QuadTest(FakeSysfsTestCase):
    def setUp(self) -> None:
        super().setUp()
        add_usb_board(
            self.root,
            upstream="1-1.2",
            interfaces=[(number, "ttyUSB{0}".format(number)) for number in range(4)],
            vid="1a86",
            pid="55d3",
            product="USB Quad_Serial",
            manufacturer="wch.cn",
            serial="CH344A1",
            driver="ch341-uart",
        )
        self.result = run_backend(self.root)

    def test_four_serial_ports(self) -> None:
        ports = self.result.ports
        self.assertEqual([port.device for port in ports], ["ttyUSB0", "ttyUSB1", "ttyUSB2", "ttyUSB3"])
        self.assertEqual({port.kind for port in ports}, {"serial"})
        self.assertEqual({(port.vid, port.pid) for port in ports}, {("1a86", "55d3")})
        self.assertEqual({port.serial for port in ports}, {"CH344A1"})
        self.assertEqual(self.result.warnings, [])

    def test_interfaces_follow_interface_number(self) -> None:
        self.assertEqual(
            [port.interface for port in self.result.ports],
            ["SERIAL-A", "SERIAL-B", "SERIAL-C", "SERIAL-D"],
        )
        self.assertEqual([port.interface_number for port in self.result.ports], [0, 1, 2, 3])
        self.assertEqual(
            [port.label for port in self.result.ports],
            [
                "CH344 Quad_Serial A",
                "CH344 Quad_Serial B",
                "CH344 Quad_Serial C",
                "CH344 Quad_Serial D",
            ],
        )

    def test_one_board_key_and_one_board(self) -> None:
        keys = {port.board_key for port in self.result.ports}
        self.assertEqual(keys, {"linux:1-1.2"})
        boards = boards_of(self.result)
        self.assertEqual(len(boards), 1)
        self.assertEqual(len(boards[0]["port_ids"]), 4)
        self.assertEqual(boards[0]["key"], "linux:1-1.2")

    def test_sysfs_paths_and_driver(self) -> None:
        first = self.result.ports[0]
        self.assertEqual(first.usb_path, "devices/usb1/1-1.2")
        self.assertEqual(first.location, "1-1.2:1.0")
        self.assertEqual(first.transport, "usb")
        self.assertEqual(first.driver.family, "ch34x")
        self.assertEqual(first.driver.id, "ch341-uart")
        self.assertEqual(first.driver.status, "ok")
        self.assertTrue(first.driver.evidence.endswith("ch341-uart"))


# ---------------------------------------------------------------------------
# 2. ListenAI VCOM: one serial port + one capture endpoint -> one board
# ---------------------------------------------------------------------------


class ListenAIComboTest(FakeSysfsTestCase):
    def setUp(self) -> None:
        super().setUp()
        add_usb_board(
            self.root,
            upstream="1-1.4",
            interfaces=[(0, "ttyUSB4")],
            vid="8765",
            pid="5678",
            product="ListenAI VCOM Dock",
            manufacturer="ListenAI",
            serial="LA0001",
            driver="cdc_acm",
        )
        add_audio_card(self.root, card=0, upstream="1-1.4", card_id="VCOM")
        proc_asound = add_proc_pcm(
            self.tmp, ["00-00: USB Audio : USB Audio : capture 8"]
        )
        self.result = run_backend(self.root, proc_asound=proc_asound)

    def test_serial_port(self) -> None:
        self.assertEqual(len(self.result.ports), 1)
        port = self.result.ports[0]
        self.assertEqual(port.device, "ttyUSB4")
        self.assertEqual(port.interface, "VCOM")
        self.assertEqual(port.label, "ListenAI VCOM")
        self.assertEqual((port.vid, port.pid), ("8765", "5678"))

    def test_capture_endpoint_with_channels(self) -> None:
        self.assertEqual(len(self.result.audio), 1)
        endpoint = self.result.audio[0]
        self.assertEqual(endpoint.kind, "audio-capture")
        self.assertEqual(endpoint.channels, 8)
        self.assertEqual(endpoint.name, "USB Audio")
        self.assertEqual(endpoint.board_key, "linux:1-1.4")
        self.assertEqual(endpoint.vid, "8765")
        self.assertEqual(endpoint.board_key, self.result.ports[0].board_key)

    def test_one_board_joins_serial_and_audio(self) -> None:
        boards = boards_of(self.result)
        self.assertEqual(len(boards), 1)
        self.assertEqual(len(boards[0]["port_ids"]), 1)
        self.assertEqual(len(boards[0]["audio_ids"]), 1)


# ---------------------------------------------------------------------------
# 3. two identical boards
# ---------------------------------------------------------------------------


class TwoIdenticalBoardsTest(FakeSysfsTestCase):
    def setUp(self) -> None:
        super().setUp()
        for upstream, serial, tty in (
            ("1-2.2", "SN-ONE", "ttyUSB0"),
            ("1-2.3", "SN-TWO", "ttyUSB1"),
        ):
            add_usb_board(
                self.root,
                upstream=upstream,
                interfaces=[(0, tty)],
                vid="1a86",
                pid="7523",
                product="USB Serial",
                manufacturer="wch.cn",
                serial=serial,
            )
        self.result = run_backend(self.root)

    def test_two_distinct_boards(self) -> None:
        keys = sorted(port.board_key for port in self.result.ports)
        self.assertEqual(keys, ["linux:1-2.2", "linux:1-2.3"])
        boards = boards_of(self.result)
        self.assertEqual(len(boards), 2)
        self.assertEqual(sorted(board["title"] for board in boards), ["USB Serial #1", "USB Serial #2"])

    def test_serial_numbers_stay_separate(self) -> None:
        self.assertEqual(
            sorted(port.serial for port in self.result.ports), ["SN-ONE", "SN-TWO"]
        )


# ---------------------------------------------------------------------------
# 4. empty sysfs
# ---------------------------------------------------------------------------


class EmptySysfsTest(FakeSysfsTestCase):
    def test_empty_tree_succeeds(self) -> None:
        result = run_backend(self.root)
        self.assertEqual(result.ports, [])
        self.assertEqual(result.audio, [])
        self.assertEqual(result.backend, "linux")
        self.assertEqual(result.os, "linux")
        self.assertEqual(boards_of(result), [])


# ---------------------------------------------------------------------------
# 5. --sysfs-root plumbing through core.collect_snapshot
# ---------------------------------------------------------------------------


class SysfsRootPlumbingTest(FakeSysfsTestCase):
    def setUp(self) -> None:
        super().setUp()
        add_usb_board(
            self.root,
            upstream="1-3.1",
            interfaces=[(0, "ttyACM0")],
            vid="8765",
            pid="5678",
            product="ListenAI VCOM Dock",
            manufacturer="ListenAI",
            serial="LA0002",
            driver="cdc_acm",
        )

    def test_collect_snapshot_forwards_sysfs_root(self) -> None:
        direct = run_backend(self.root)
        snapshot = core.collect_snapshot(backend="linux", sysfs_root=str(self.root))
        self.assertEqual(snapshot.backend, "linux")
        self.assertEqual(snapshot.os, "linux")
        self.assertEqual(
            [(port.id, port.device) for port in snapshot.ports],
            [(port.id, port.device) for port in direct.ports],
        )
        self.assertEqual(len(snapshot.ports), 1)


# ---------------------------------------------------------------------------
# extra coverage: uevent-only identity, unresolvable tty, no-procfs audio
# ---------------------------------------------------------------------------


class UeventOnlyIdentityTest(FakeSysfsTestCase):
    def test_usb_device_identified_from_uevent_product(self) -> None:
        usb = self.root / "devices" / "usb1" / "1-4.2"
        # No ":N.M" suffix here, so the interface number has to come from the
        # bInterfaceNumber key of the interface uevent.
        iface = usb / "interface"
        tty_dir = iface / "ttyUSB7"
        _write(usb / "uevent", "PRODUCT=1a86/55d9/0100")
        tty_dir.mkdir(parents=True, exist_ok=True)
        _symlink(tty_dir / "device", iface)
        _symlink(self.root / "class" / "tty" / "ttyUSB7", tty_dir)
        _write(iface / "uevent", "bInterfaceNumber=07")

        result = run_backend(self.root)
        self.assertEqual(len(result.ports), 1)
        port = result.ports[0]
        self.assertEqual((port.vid, port.pid), ("1a86", "55d9"))
        self.assertEqual(port.interface_number, 7)
        self.assertEqual(port.interface, "Ch H")
        self.assertEqual(port.location, "interface")
        self.assertEqual(port.board_key, "linux:1-4.2")
        self.assertEqual(port.driver.status, "missing")
        self.assertEqual(port.driver.family, "ch34x")


class UnresolvableTtyTest(FakeSysfsTestCase):
    def test_tty_without_usb_parent_is_ignored_with_warning(self) -> None:
        tty_dir = self.root / "class" / "tty" / "ttyUSB9"
        tty_dir.mkdir(parents=True, exist_ok=True)
        (tty_dir / "device").mkdir(parents=True, exist_ok=True)

        result = run_backend(self.root)
        self.assertEqual(result.ports, [])
        self.assertEqual(len(result.warnings), 1)
        self.assertIn("ttyUSB9", result.warnings[0])


class AudioWithoutProcfsTest(FakeSysfsTestCase):
    def test_channels_unknown_and_warning_when_proc_skipped(self) -> None:
        add_audio_card(self.root, card=0, upstream=None, card_id="Dock")
        result = run_backend(self.root)
        self.assertEqual(len(result.audio), 1)
        endpoint = result.audio[0]
        self.assertIsNone(endpoint.channels)
        self.assertEqual(endpoint.name, "Dock")
        self.assertIsNone(endpoint.board_key)
        self.assertEqual(endpoint.extra.get("source"), "sysfs")
        self.assertTrue(any("procfs" in warning for warning in result.warnings))

    def test_allow_proc_false_skips_procfs(self) -> None:
        add_audio_card(self.root, card=0, upstream=None, card_id="Dock")
        proc_asound = add_proc_pcm(self.tmp, ["00-00: USB Audio : USB Audio : capture 8"])
        result = run_backend(self.root, proc_asound=proc_asound, allow_proc=False)
        self.assertIsNone(result.audio[0].channels)
        self.assertTrue(any("allow_proc=false" in warning for warning in result.warnings))


class AvailabilityTest(unittest.TestCase):
    def test_available_never_raises_and_matches_host(self) -> None:
        ok, reason = LinuxBackend().available()
        self.assertIsInstance(ok, bool)
        self.assertIsInstance(reason, str)
        if platform.system() == "Linux" and os.path.isdir("/sys/class/tty"):
            self.assertTrue(ok)
            self.assertIn("sysfs", reason)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
