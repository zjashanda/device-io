"""Linux backend: sysfs enumeration of serial ports and audio endpoints.

Everything here is a read of files below one sysfs root (``/sys`` by default,
or the value of ``--sysfs-root`` for offline trees). No subprocess, no
``lsusb``/``udevadm``/``pyserial``, no writes and no device opens, so the
backend runs unprivileged and can be verified against a fake tree.

The backend only describes endpoints. Which interfaces belong to the same
physical board is decided by :mod:`device_io.boards` from the ``board_key``
this module sets: ``linux:<upstream usb device dir name>``. Every interface of
one USB device resolves to the same upstream key, so a CH344 quad adapter is
one board with four ``SERIAL-A..D`` interfaces.

Traversal mirrors ``src/usb-tree/scripts/usbTree-linux.py`` (resolve the tty
entry, resolve its ``device`` link, walk up to the directory holding
``idVendor``/``idProduct``, parse the ``:N.M`` interface suffix), adapted to
this package's ``Port`` model.
"""

from __future__ import annotations

import glob
import os
import platform
import re
from typing import Dict, List, Optional, Tuple

from .. import drivers
from ..models import AudioEndpoint, DriverInfo, Port
from . import register
from .base import Backend, BackendOptions, BackendResult

DEFAULT_ROOT = "/sys"
DEFAULT_PROC_ASOUND = "/proc/asound"

_TTY_RE = re.compile(r"^tty(?:USB|ACM)\d+$")
_CARD_RE = re.compile(r"^card(\d+)$")
# ``1-1.2:1.0`` -> interface number 0 (a hub port keeps its ``.N`` suffix).
_IFACE_RE = re.compile(r":\d+\.(\d+)$")
# ``00-00: USB Audio : USB Audio : capture 8``
_PCM_RE = re.compile(r"^(\d+)-(\d+):\s*(.*)$")
# ``playback 1`` / ``capture 8`` inside a /proc/asound/pcm line.
_DIRECTION_RE = re.compile(r"^(playback|capture)\s+(\d+)$")

_CH344_PIDS = ("55d3", "55d4")
_CH348_PIDS = ("55d9",)
_LISTENAI_VID = "8765"

_AUDIO_CAPTURE = "audio-capture"
_AUDIO_RENDER = "audio-render"


# ---------------------------------------------------------------------------
# small file helpers (read-only)
# ---------------------------------------------------------------------------


def _read_text(path: str) -> str:
    """Return the trimmed content of ``path`` or '' when it is unreadable."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read().strip()
    except OSError:
        return ""


def _read_uevent(path: str) -> Dict[str, str]:
    """Parse a sysfs ``uevent`` file into a key -> value dict."""
    values: Dict[str, str] = {}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                key, _, value = line.strip().partition("=")
                if key and _:
                    values[key] = value
    except OSError:
        return {}
    return values


def _hex_value(raw: Optional[str]) -> Optional[str]:
    """``'0x1A86'`` / ``'1a86 '`` -> ``'1a86'``; '' -> None."""
    if raw is None:
        return None
    text = str(raw).strip().lower()
    if text.startswith("0x"):
        text = text[2:]
    return text or None


def _usb_identity(directory: str) -> Optional[Tuple[str, str]]:
    """Return ``(vid, pid)`` when ``directory`` is a USB device directory.

    Prefers ``idVendor``/``idProduct`` files; falls back to ``PRODUCT=vid/pid``
    in the directory ``uevent`` so a tree that only records uevent files still
    resolves. Interface directories carry neither, which is how the walk-up
    knows where to stop.
    """
    vendor = _hex_value(_read_text(os.path.join(directory, "idVendor")))
    product = _hex_value(_read_text(os.path.join(directory, "idProduct")))
    if vendor and product:
        return vendor, product
    uevent = _read_uevent(os.path.join(directory, "uevent"))
    raw = uevent.get("PRODUCT")
    if raw:
        parts = raw.split("/")
        if len(parts) >= 2:
            vendor = _hex_value(parts[0])
            product = _hex_value(parts[1])
            if vendor and product:
                return vendor, product
    return None


def _ancestors(start: str, root: str) -> List[str]:
    """``start`` and every parent up to (and including) ``root``."""
    current = os.path.abspath(start)
    stop = os.path.abspath(root)
    chain: List[str] = []
    while True:
        chain.append(current)
        if os.path.normpath(current) == os.path.normpath(stop):
            break
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    return chain


def _relative(path: str, root: str) -> str:
    """Path relative to the sysfs root, with forward slashes."""
    try:
        rel = os.path.relpath(path, os.path.abspath(root))
    except ValueError:  # pragma: no cover - different drives (Windows host)
        return path
    if rel.startswith(".."):
        return path
    return rel.replace(os.sep, "/")


# ---------------------------------------------------------------------------
# interface description
# ---------------------------------------------------------------------------


def _letter(number: Optional[int]) -> Optional[str]:
    """0 -> 'A', 1 -> 'B', ... Used by the CH344/CH348 interface labels."""
    if number is None or number < 0:
        return None
    return chr(ord("A") + number) if number < 26 else None


def _interface_info(
    device_dir: str, root: str
) -> Tuple[Optional[str], Optional[str], Optional[int]]:
    """Locate the interface directory behind a tty's ``device`` link.

    Returns ``(path, name, interface_number)``. The number comes from the
    ``:N.M`` suffix of the interface directory name; when the tree does not use
    that naming, the ``bInterfaceNumber`` key of an ancestor ``uevent`` is used
    instead.
    """
    ancestors = _ancestors(device_dir, root)
    for path in ancestors:
        match = _IFACE_RE.search(os.path.basename(path))
        if match:
            return path, os.path.basename(path), int(match.group(1))
    for path in ancestors:
        raw = _read_uevent(os.path.join(path, "uevent")).get("bInterfaceNumber")
        if not raw:
            continue
        text = raw.strip().lower()
        number: Optional[int] = None
        try:
            number = int(text, 16)
        except ValueError:
            try:
                number = int(text, 10)
            except ValueError:
                number = None
        if number is not None:
            return path, os.path.basename(path) or None, number
    return None, None, None


def _interface_label(
    vid: Optional[str], pid: Optional[str], number: Optional[int]
) -> Optional[str]:
    """Human interface token: SERIAL-B / Ch C / VCOM / MI_2."""
    if vid == "1a86" and pid in _CH344_PIDS:
        letter = _letter(number)
        return "SERIAL-{0}".format(letter) if letter else None
    if vid == "1a86" and pid in _CH348_PIDS:
        letter = _letter(number)
        return "Ch {0}".format(letter) if letter else None
    if vid == _LISTENAI_VID:
        return "VCOM"
    if number is not None:
        return "MI_{0}".format(number)
    return None


def _port_label(
    vid: Optional[str],
    pid: Optional[str],
    number: Optional[int],
    product: Optional[str],
    device: str,
) -> str:
    """Label the operator sees; falls back to product, then the tty name."""
    if vid == "1a86" and pid in _CH344_PIDS:
        letter = _letter(number)
        return "CH344 Quad_Serial {0}".format(letter) if letter else "CH344 Quad_Serial"
    if vid == _LISTENAI_VID:
        return "ListenAI VCOM"
    return product or device


def _driver_fields(iface_dir: Optional[str]) -> Tuple[str, str, str]:
    """Return ``(kernel_driver, status, evidence)`` for one interface."""
    if not iface_dir:
        return "", "missing", ""
    link = os.path.join(iface_dir, "driver")
    if not os.path.lexists(link):
        return "", "missing", ""
    kernel = os.path.basename(os.path.realpath(link).rstrip(os.sep))
    evidence = os.path.realpath(link)
    status = "ok" if os.path.isdir(link) else "missing"
    return kernel, status, evidence


def _family(vid: Optional[str], pid: Optional[str], product: Optional[str], interface: Optional[str], kernel: str) -> str:
    """Driver family: a specific adapter wins, else the kernel module name."""
    adapter = drivers.adapter_for(vid, pid, product, interface)
    if adapter is not None and (adapter.vid or adapter.pid or adapter.product_contains):
        return adapter.family
    probe = Port(
        device="probe",
        os="linux",
        vid=vid,
        pid=pid,
        product=product,
        interface=interface,
        driver=DriverInfo(family="unknown", id=kernel, display=kernel),
    )
    return drivers.driver_family(probe)


# ---------------------------------------------------------------------------
# audio
# ---------------------------------------------------------------------------


def _parse_proc_pcm(path: str) -> List[Dict[str, object]]:
    """Parse ``/proc/asound/pcm`` into one record per PCM device."""
    records: List[Dict[str, object]] = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            lines = handle.readlines()
    except OSError:
        return records
    for line in lines:
        match = _PCM_RE.match(line.strip())
        if not match:
            continue
        card = int(match.group(1))
        device = int(match.group(2))
        parts = [part.strip() for part in match.group(3).split(" : ") if part.strip()]
        name = parts[0] if parts else "pcm{0:02d}-{1:02d}".format(card, device)
        playback = 0
        capture = 0
        for part in parts[1:]:
            direction = _DIRECTION_RE.match(part)
            if not direction:
                continue
            count = int(direction.group(2))
            if direction.group(1) == "capture":
                capture = count
            else:
                playback = count
        records.append(
            {
                "card": card,
                "device": device,
                "name": name,
                "playback": playback,
                "capture": capture,
            }
        )
    return records


def _read_proc_records(
    root: str, options: BackendOptions, warnings: List[str]
) -> Tuple[List[Dict[str, object]], List[str]]:
    """Read procfs PCM records when it actually describes ``root``.

    ``/proc/asound`` always describes the live machine, never a fake tree, so
    it is only trusted when the sysfs root is the real ``/sys`` or the caller
    injects the matching proc root via ``options.extra['proc_asound']``.
    Skipping it is reported, because the channel counts then stay unknown.
    """
    if not bool(options.extra.get("allow_proc", True)):
        warnings.append(
            "audio: procfs was not consulted (allow_proc=false); channel counts are unknown"
        )
        return [], []
    injected = options.extra.get("proc_asound")
    injected_root = str(injected) if injected else ""
    proc_root = injected_root or DEFAULT_PROC_ASOUND
    if not injected_root and os.path.abspath(root) != os.path.abspath(DEFAULT_ROOT):
        warnings.append(
            "audio: procfs was not consulted (sysfs root {0} has no matching /proc/asound); "
            "channel counts are unknown".format(root)
        )
        return [], []
    pcm_path = os.path.join(proc_root, "pcm")
    if not os.path.isfile(pcm_path):
        warnings.append(
            "audio: procfs was not consulted ({0} not found); channel counts are unknown".format(
                pcm_path
            )
        )
        return [], []
    return _parse_proc_pcm(pcm_path), ["procfs:{0}".format(pcm_path)]


# ---------------------------------------------------------------------------
# backend
# ---------------------------------------------------------------------------


@register
class LinuxBackend(Backend):
    name = "linux"
    os_name = "linux"

    def available(self) -> tuple:
        """True on Linux with a readable sysfs tty class. Never raises."""
        try:
            system = platform.system()
            if system != "Linux":
                return False, "host is {0}, not Linux".format(system or "unknown")
            tty_class = os.path.join(DEFAULT_ROOT, "class", "tty")
            if not os.path.isdir(tty_class):
                return False, "{0} is missing (is sysfs mounted?)".format(tty_class)
            return True, "sysfs tty class at {0}".format(tty_class)
        except Exception as exc:  # pragma: no cover - defensive
            return False, "probe failed: {0}".format(exc)

    def collect(self, options: BackendOptions) -> BackendResult:
        root = os.path.abspath(options.sysfs_root or DEFAULT_ROOT)
        warnings: List[str] = []
        sources: List[str] = ["linux:sysfs:{0}".format(root)]

        ports = self._collect_ports(root, warnings)
        audio: List[AudioEndpoint] = []
        if options.include_audio:
            audio, audio_sources = self._collect_audio(root, options, warnings)
            sources.extend(audio_sources)

        ports.sort(
            key=lambda port: (
                port.board_key or "",
                port.interface_number if port.interface_number is not None else 99,
                port.device,
            )
        )
        audio.sort(key=lambda endpoint: (endpoint.kind, endpoint.name))

        return BackendResult(
            backend=self.name,
            os=self.os_name,
            ports=ports,
            audio=audio,
            warnings=warnings,
            sources=sources,
        )

    # -- serial ----------------------------------------------------------
    def _collect_ports(self, root: str, warnings: List[str]) -> List[Port]:
        entries: List[str] = []
        for pattern in ("ttyUSB*", "ttyACM*"):
            entries.extend(glob.glob(os.path.join(root, "class", "tty", pattern)))
        ports: List[Port] = []
        for entry in sorted(set(entries)):
            name = os.path.basename(entry)
            if not _TTY_RE.match(name):
                continue
            port = self._port_from_tty(entry, name, root, warnings)
            if port is not None:
                ports.append(port)
        return ports

    def _port_from_tty(
        self, entry: str, name: str, root: str, warnings: List[str]
    ) -> Optional[Port]:
        device_dir = os.path.realpath(os.path.join(entry, "device"))
        if not os.path.isdir(device_dir):
            # Some virtual ttys expose no device link at all; they are not USB.
            device_dir = os.path.realpath(entry)

        usb_dir: Optional[str] = None
        identity: Optional[Tuple[str, str]] = None
        for candidate in _ancestors(device_dir, root):
            found = _usb_identity(candidate)
            if found is not None:
                usb_dir, identity = candidate, found
                break
        if usb_dir is None or identity is None:
            warnings.append(
                "linux: ignored {0}: no USB parent with idVendor/idProduct".format(name)
            )
            return None

        vid, pid = identity
        iface_dir, iface_name, number = _interface_info(device_dir, root)
        product = _read_text(os.path.join(usb_dir, "product")) or None
        manufacturer = _read_text(os.path.join(usb_dir, "manufacturer")) or None
        serial = _read_text(os.path.join(usb_dir, "serial")) or None
        interface = _interface_label(vid, pid, number)
        kernel, status, evidence = _driver_fields(iface_dir or device_dir)

        port = Port(
            device=name,
            os=self.os_name,
            kind="serial",
            label=_port_label(vid, pid, number, product, name),
            transport="usb",
            interface=interface,
            interface_number=number,
            vid=vid,
            pid=pid,
            serial=serial,
            manufacturer=manufacturer,
            product=product,
            board_key="linux:{0}".format(os.path.basename(usb_dir)),
            usb_path=_relative(usb_dir, root),
            location=iface_name,
            driver=DriverInfo(
                family="unknown",
                id=kernel,
                display=kernel,
                status=status,
                evidence=evidence,
            ),
        )
        port.driver.family = _family(vid, pid, product, interface, kernel)
        return port

    # -- audio -----------------------------------------------------------
    def _collect_audio(
        self, root: str, options: BackendOptions, warnings: List[str]
    ) -> Tuple[List[AudioEndpoint], List[str]]:
        cards: List[Tuple[int, str, str]] = []
        for entry in glob.glob(os.path.join(root, "class", "sound", "card*")):
            match = _CARD_RE.match(os.path.basename(entry))
            if match:
                cards.append((int(match.group(1)), os.path.basename(entry), entry))
        if not cards:
            return [], []
        cards.sort()

        records, sources = _read_proc_records(root, options, warnings)
        by_card: Dict[int, List[Dict[str, object]]] = {}
        for record in records:
            by_card.setdefault(int(record["card"]), []).append(record)

        endpoints: List[AudioEndpoint] = []
        for number, card_name, card_path in cards:
            endpoints.extend(
                self._card_endpoints(
                    number,
                    card_name,
                    card_path,
                    by_card.get(number) or [],
                    bool(records),
                    root,
                    warnings,
                )
            )
        return endpoints, sources

    def _card_endpoints(
        self,
        number: int,
        card_name: str,
        card_path: str,
        records: List[Dict[str, object]],
        proc_consulted: bool,
        root: str,
        warnings: List[str],
    ) -> List[AudioEndpoint]:
        device_link = os.path.join(card_path, "device")
        device_dir = os.path.realpath(device_link) if os.path.lexists(device_link) else ""

        usb_dir: Optional[str] = None
        identity: Optional[Tuple[str, str]] = None
        if device_dir:
            for candidate in _ancestors(device_dir, root):
                found = _usb_identity(candidate)
                if found is not None:
                    usb_dir, identity = candidate, found
                    break
        vid, pid = identity if identity is not None else (None, None)
        board_key = "linux:{0}".format(os.path.basename(usb_dir)) if usb_dir else None
        if board_key is None:
            warnings.append(
                "audio: {0} has no resolvable USB parent; the endpoint has no board_key".format(
                    card_name
                )
            )

        card_id = _read_text(os.path.join(card_path, "id")) or card_name
        devname = _read_uevent(os.path.join(card_path, "uevent")).get("DEVNAME", "")

        if proc_consulted and records:
            endpoints: List[AudioEndpoint] = []
            for record in records:
                pcm_id = "pcm{0:02d}-{1:02d}".format(int(record["card"]), int(record["device"]))
                extra: Dict[str, object] = {"card": card_name}
                if devname:
                    extra["devname"] = devname
                for kind, key in ((_AUDIO_RENDER, "playback"), (_AUDIO_CAPTURE, "capture")):
                    channels = int(record[key])  # type: ignore[arg-type]
                    if channels <= 0:
                        continue
                    direction = "render" if kind == _AUDIO_RENDER else "capture"
                    endpoints.append(
                        AudioEndpoint(
                            kind=kind,
                            name=str(record["name"]),
                            board_key=board_key,
                            channels=channels,
                            vid=vid,
                            pid=pid,
                            endpoint_id="{0}:{1}".format(pcm_id, direction),
                            extra=dict(extra),
                        )
                    )
            if endpoints:
                return endpoints

        # No procfs: sysfs alone cannot tell playback from capture, so the
        # direction is recorded as unknown instead of being invented.
        extra = {"card": card_name, "direction": "unknown", "source": "sysfs"}
        if devname:
            extra["devname"] = devname
        return [
            AudioEndpoint(
                kind=_AUDIO_CAPTURE,
                name=card_id,
                board_key=board_key,
                channels=None,
                vid=vid,
                pid=pid,
                endpoint_id=card_name,
                extra=extra,
            )
        ]


__all__ = ["LinuxBackend"]
