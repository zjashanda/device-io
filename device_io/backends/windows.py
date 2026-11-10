"""Windows backend: PowerShell CIM collector + Python normalisation.

The split matters. ``windows/Collect-DeviceIo.ps1`` is a deliberate
*observation-only* script: it reads CIM/PnP state and prints one raw JSON
document. Everything that decides what a port *means* -- interface letters,
board grouping, driver family -- happens here in pure Python, so the whole
mapping is unit-testable on Linux from recorded fixtures.

``normalize_raw`` is the seam the tests use: it takes the raw document and
returns ``BackendResult`` with no subprocess, no ``platform`` check and no
filesystem access.
"""

from __future__ import annotations

import json
import platform
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .. import drivers, schema
from ..models import AudioEndpoint, DriverInfo, Port
from . import register
from .base import (
    Backend,
    BackendError,
    BackendOptions,
    BackendResult,
    BackendUnavailable,
)

RAW_SCHEMA = "ark.device-io.raw.v1"
COLLECTOR_SCRIPT = Path(__file__).resolve().parents[2] / "windows" / "Collect-DeviceIo.ps1"
COLLECT_TIMEOUT = 120

# ``Unknown`` is what Windows reports for a phantom devnode, so it is the
# strongest single signal that a port is a ghost rather than plugged in.
_INACTIVE_STATUSES = ("unknown", "error", "disabled")
_AUDIO_INACTIVE_STATES = (
    "unknown",
    "error",
    "disabled",
    "inactive",
    "not present",
    "notpresent",
    "unplugged",
)

_VID_PID_RE = re.compile(r"VID_([0-9A-Fa-f]{4})&PID_([0-9A-Fa-f]{4})", re.IGNORECASE)
_MI_RE = re.compile(r"&MI_([0-9A-Fa-f]{2})", re.IGNORECASE)
_COM_RE = re.compile(r"(COM\d+)", re.IGNORECASE)
_CHANNEL_RE = re.compile(r"(\d+)\s*ch\b", re.IGNORECASE)
_CHANNEL_PREFIX_RE = re.compile(r"^(\d+)\s*ch\b\s*", re.IGNORECASE)
_DIRECTION_PREFIX_RE = re.compile(
    r"^(?:capture|render|playback|record)(?![a-z])\s*|^(?:输出|输入|录音|播放)\s*", re.IGNORECASE
)
_TRAILING_COM_RE = re.compile(r"\s*\((COM\d+)\)\s*$", re.IGNORECASE)

# CH344 uses one interface pair per UART (00/02/04/06). CH348 packs eight UARTs
# into even interface numbers 00..0E.
_CH344_INTERFACES = {
    "00": "SERIAL-A",
    "02": "SERIAL-B",
    "04": "SERIAL-C",
    "06": "SERIAL-D",
}
_CH348_INTERFACES = {
    "00": "Ch A",
    "02": "Ch B",
    "04": "Ch C",
    "06": "Ch D",
    "08": "Ch E",
    "0A": "Ch F",
    "0C": "Ch G",
    "0E": "Ch H",
}

# What the family's driver service is normally called on Windows. Used only to
# decide ``ok`` vs ``mismatch``; the family itself comes from drivers.adapter_for
# or the service-name table below.
_EXPECTED_SERVICE = {
    "ch34x": ("ch343ser", "ch341ser"),
    "ch343": ("ch343ser",),
    "cp210x": ("silabser", "cp210x"),
    "ftdi": ("ftdibus",),
    "cdc-acm": ("usbser",),
    "listenai-vcom": ("usbser",),
}

# Fallback family detection when no adapter matches the VID/PID.
_SERVICE_FAMILY = (
    ("ch343ser", "ch34x"),
    ("ch341ser", "ch34x"),
    ("ch34x", "ch34x"),
    ("silabser", "cp210x"),
    ("cp210x", "cp210x"),
    ("ftdibus", "ftdi"),
    ("usbser", "cdc-acm"),
    ("cdc", "cdc-acm"),
)

_RENDER_HINTS = ("speaker", "render", "headphone", "headset", "output", "输出", "扬声器")


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _as_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [_text(item) for item in value if _text(item)]
    text = _text(value)
    return [text] if text else []


def _device_index(raw: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Instance id -> device record. Ids compare case-insensitively."""
    index: Dict[str, Dict[str, Any]] = {}
    for item in raw.get("devices") or []:
        if not isinstance(item, dict):
            continue
        key = _text(item.get("instance_id")).upper()
        if key:
            index[key] = item
    return index


def _lookup(
    index: Dict[str, Dict[str, Any]], instance_id: str
) -> Dict[str, Any]:
    if not instance_id:
        return {}
    return index.get(instance_id.upper(), {})


def _hardware_ids(device: Dict[str, Any], instance_id: str) -> List[str]:
    ids = _as_list(device.get("hardware_ids"))
    if not ids and instance_id:
        ids = [instance_id]
    return ids


def _vid_pid(
    device: Dict[str, Any], instance_id: str
) -> Tuple[Optional[str], Optional[str]]:
    """VID/PID as lowercase hex without ``0x``; ``(None, None)`` when absent.

    Hardware ids are authoritative. Audio endpoints carry no USB ids of their
    own, so the PnP parent devnode is used as the last resort.
    """
    for blob in _hardware_ids(device, instance_id):
        match = _VID_PID_RE.search(blob or "")
        if match:
            return match.group(1).lower(), match.group(2).lower()
    for blob in (instance_id or "", _text(device.get("parent"))):
        match = _VID_PID_RE.search(blob)
        if match:
            return match.group(1).lower(), match.group(2).lower()
    return None, None


def _serial_from_instance(instance_id: str) -> Optional[str]:
    """Windows puts a real serial in the last segment; bus ids contain ``&``."""
    if not instance_id:
        return None
    head, _, tail = (instance_id or "").rpartition("\\")
    if not head or not tail:
        return None
    if "&" in tail:
        return None
    return tail or None


def _interface_for(
    instance_id: str, vid: Optional[str], pid: Optional[str]
) -> Tuple[Optional[str], Optional[int]]:
    """Resolve (interface token, interface number) from ``&MI_XX``."""
    match = _MI_RE.search(instance_id or "")
    token = match.group(1).upper() if match else None
    number = int(token, 16) if token else None

    if vid == "8765":
        # ListenAI bench dock exposes a single CDC-ACM virtual COM.
        return "VCOM", number
    if token is None:
        return None, None
    if vid == "1a86" and pid in ("55d3", "55d4"):
        return _CH344_INTERFACES.get(token, "MI_{0}".format(token)), number
    if vid == "1a86" and pid == "55d9":
        return _CH348_INTERFACES.get(token, "MI_{0}".format(token)), number
    return "MI_{0}".format(token), number


def _is_ch344(vid: Optional[str], pid: Optional[str], blob: str) -> bool:
    if vid == "1a86" and pid in ("55d3", "55d4"):
        return True
    low = (blob or "").lower()
    return "ch344" in low or "quad_serial" in low


def _is_ch348(vid: Optional[str], pid: Optional[str], blob: str) -> bool:
    if vid == "1a86" and pid == "55d9":
        return True
    return "ch348" in (blob or "").lower()


def _product_name(
    vid: Optional[str], pid: Optional[str], friendly_name: str
) -> str:
    blob = friendly_name or ""
    if _is_ch344(vid, pid, blob):
        return "CH344 Quad_Serial"
    if vid == "8765":
        return "ListenAI VCOM"
    if _is_ch348(vid, pid, blob):
        return "CH348"
    stripped = _TRAILING_COM_RE.sub("", blob).strip()
    return stripped or blob


def _label_for(
    vid: Optional[str],
    pid: Optional[str],
    friendly_name: str,
    product: str,
    interface: Optional[str],
) -> str:
    if _is_ch344(vid, pid, friendly_name or product):
        letter = ""
        if interface and interface.startswith("SERIAL-"):
            letter = interface.split("-", 1)[1]
        return ("CH344 Quad_Serial {0}".format(letter)).strip()
    if vid == "8765":
        return "ListenAI VCOM"
    if _is_ch348(vid, pid, friendly_name or product) and interface:
        return "CH348 {0}".format(interface)
    return friendly_name or product


def _board_key_for(
    container_id: str,
    parent: str,
    vid: Optional[str],
    pid: Optional[str],
    serial: Optional[str],
    instance_id: str,
) -> Optional[str]:
    """Upstream physical identity, identical for every interface of a board.

    Order: ContainerID, then the PnP parent devnode, then a synthetic key built
    from VID/PID plus either the serial or the instance prefix before ``&MI_``.
    """
    if container_id:
        return container_id
    if parent:
        return parent
    if vid and pid:
        if serial:
            return "win:{0}:{1}:{2}".format(vid, pid, serial)
        return "win:{0}:{1}:{2}".format(vid, pid, _instance_prefix(instance_id))
    return None


def _instance_prefix(instance_id: str) -> str:
    text = instance_id or ""
    match = _MI_RE.search(text)
    if match:
        return text[: match.start()]
    head, sep, _tail = text.rpartition("\\")
    return head if sep else text


def _com_number(device: str) -> int:
    match = _COM_RE.search(device or "")
    if not match:
        return 9999
    return int(match.group(1)[3:])


def _service_family(service: str, description: str) -> Optional[str]:
    blob = "{0} {1}".format(service or "", description or "").lower()
    for token, family in _SERVICE_FAMILY:
        if token in blob:
            return family
    return None


def _driver_info(
    vid: Optional[str],
    pid: Optional[str],
    product: str,
    interface: Optional[str],
    service: str,
    device_status: str,
    friendly_name: str,
) -> DriverInfo:
    adapter = drivers.adapter_for(vid, pid, product, interface)
    family = adapter.family if adapter is not None else None
    if not family or family == "unknown":
        family = _service_family(service, product) or "unknown"

    expected = _EXPECTED_SERVICE.get(family, ())
    service_low = (service or "").lower()
    active = (device_status or "").lower() not in _INACTIVE_STATUSES

    if not service:
        status = "missing"
    elif not active:
        status = "unknown"
    elif expected and service_low not in expected:
        status = "mismatch"
    else:
        status = "ok"

    display = ""
    requires: List[str] = []
    if adapter is not None:
        display = adapter.display
        requires = list(adapter.requires)
    if not display:
        display = service or friendly_name

    evidence = "service={0}; device status={1}".format(
        service or "(none)", device_status or "(unknown)"
    )
    return DriverInfo(
        family=family or "unknown",
        id=service or "",
        display=display,
        status=status,
        evidence=evidence,
        requires=requires,
    )


# ---------------------------------------------------------------------------
# normalisation
# ---------------------------------------------------------------------------


def _build_port(
    record: Dict[str, Any],
    index: Dict[str, Dict[str, Any]],
    include_disabled: bool,
) -> Optional[Port]:
    name = _text(record.get("name"))
    instance_id = _text(record.get("instance_id"))
    if not name:
        return None
    device = _lookup(index, instance_id)

    status = _text(device.get("status")) or _text(record.get("status"))
    active = status.lower() not in _INACTIVE_STATUSES
    if not active and not include_disabled:
        return None

    vid, pid = _vid_pid(device, instance_id)
    hardware_ids = _hardware_ids(device, instance_id)
    blob = " ".join(hardware_ids)
    friendly_name = _text(device.get("friendly_name")) or _text(record.get("device_desc"))
    product = _product_name(vid, pid, friendly_name)
    interface, interface_number = _interface_for(instance_id, vid, pid)
    serial = _serial_from_instance(instance_id)
    container_id = _text(device.get("container_id"))
    parent = _text(device.get("parent"))
    location = _text(device.get("location"))
    service = _text(device.get("service"))
    manufacturer = _text(device.get("manufacturer")) or None
    board_key = _board_key_for(
        container_id, parent, vid, pid, serial, instance_id
    )

    return Port(
        device=name,
        os="windows",
        kind="serial",
        extra=schema.raw_fields(device),
        label=_label_for(vid, pid, friendly_name, product, interface),
        transport="usb",
        interface=interface,
        interface_number=interface_number,
        vid=vid,
        pid=pid,
        serial=serial,
        manufacturer=manufacturer,
        product=product,
        board_key=board_key,
        usb_path=location or None,
        location=location or None,
        present=active,
        driver=_driver_info(
            vid, pid, product, interface, service, status, friendly_name
        ),
    )


def _audio_kind(name: str) -> str:
    low = (name or "").lower()
    if any(token in low for token in _RENDER_HINTS):
        return "audio-render"
    return "audio-capture"


def _audio_channels(name: str) -> Optional[int]:
    match = _CHANNEL_RE.search(name or "")
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:  # pragma: no cover - regex already guarantees digits
        return None


def _audio_display_name(name: str) -> str:
    stripped = _DIRECTION_PREFIX_RE.sub("", name or "")
    stripped = _CHANNEL_PREFIX_RE.sub("", stripped)
    return stripped.strip() or name


def _audio_board_key(
    container_id: str,
    vid: Optional[str],
    pid: Optional[str],
    ports: Sequence[Port],
) -> Optional[str]:
    keys = {port.board_key for port in ports if port.board_key}
    if container_id and container_id in keys:
        return container_id
    if vid and pid:
        matching = {
            port.board_key
            for port in ports
            if port.vid == vid and port.pid == pid and port.board_key
        }
        if len(matching) == 1:
            return matching.pop()
    return None


def _build_audio(
    record: Dict[str, Any],
    index: Dict[str, Dict[str, Any]],
    ports: Sequence[Port],
) -> Optional[AudioEndpoint]:
    endpoint_id = _text(record.get("instance_id"))
    name = _text(record.get("name")) or endpoint_id
    if not name:
        return None
    device = _lookup(index, endpoint_id)
    container_id = _text(device.get("container_id"))
    vid, pid = _vid_pid(device, endpoint_id)
    if not vid:
        vid, pid = _vid_pid({}, name)

    state = _text(record.get("state")) or _text(device.get("status"))
    present = (state or "").lower() not in _AUDIO_INACTIVE_STATES

    return AudioEndpoint(
        kind=_audio_kind(name),
        name=_audio_display_name(name),
        board_key=_audio_board_key(container_id, vid, pid, ports),
        channels=_audio_channels(name),
        vid=vid,
        pid=pid,
        endpoint_id=endpoint_id or None,
        present=present,
        extra={"friendly_name": name, **schema.raw_fields(record)},
    )


def normalize_raw(
    raw: Dict[str, Any],
    options: Optional[BackendOptions] = None,
    sources: Optional[Sequence[str]] = None,
) -> BackendResult:
    """Turn one ``ark.device-io.raw.v1`` document into a ``BackendResult``.

    Pure: no subprocess, no OS probe, no file reads. Tests call this directly.
    """
    if not isinstance(raw, dict):
        raise BackendError("raw collector output must be a JSON object")
    if raw.get("schema") != RAW_SCHEMA:
        raise BackendError(
            "expected raw schema {0}, got {1!r}".format(
                RAW_SCHEMA, raw.get("schema")
            )
        )

    opts = options or BackendOptions()
    index = _device_index(raw)
    warnings: List[str] = [
        _text(item) for item in raw.get("warnings") or [] if _text(item)
    ]

    ports: List[Port] = []
    for record in raw.get("serial_ports") or []:
        if not isinstance(record, dict):
            continue
        port = _build_port(record, index, bool(opts.include_disabled))
        if port is not None:
            ports.append(port)

    if not ports:
        warnings.append("Windows collector reported no serial ports")

    audio: List[AudioEndpoint] = []
    if opts.include_audio:
        for record in raw.get("audio") or []:
            if not isinstance(record, dict):
                continue
            endpoint = _build_audio(record, index, ports)
            if endpoint is not None:
                audio.append(endpoint)

    ports.sort(
        key=lambda port: (
            port.board_key or "zz",
            port.interface_number if port.interface_number is not None else 99,
            _com_number(port.device),
        )
    )
    audio.sort(key=lambda item: (item.kind, item.name))

    return BackendResult(
        backend="windows",
        os="windows",
        ports=ports,
        audio=audio,
        warnings=warnings,
        sources=list(sources or []),
    )


# ---------------------------------------------------------------------------
# backend
# ---------------------------------------------------------------------------


def powershell() -> Optional[str]:
    """Path to ``pwsh`` or ``powershell``; ``None`` when neither is installed."""
    for name in ("pwsh", "powershell"):
        found = shutil.which(name)
        if found:
            return found
    return None


def _last_json_object(text: str) -> Dict[str, Any]:
    """Last JSON object in stdout; PowerShell warnings are allowed before it."""
    stripped = (text or "").strip()
    if not stripped:
        raise BackendError("collector produced no output")
    try:
        data = json.loads(stripped)
        if isinstance(data, dict):
            return data
    except ValueError:
        pass

    decoder = json.JSONDecoder()
    fallback: Optional[Dict[str, Any]] = None
    positions = [index for index, char in enumerate(stripped) if char == "{"]
    for start in reversed(positions):
        try:
            candidate, _end = decoder.raw_decode(stripped, start)
        except ValueError:
            continue
        if not isinstance(candidate, dict):
            continue
        if candidate.get("schema") == RAW_SCHEMA:
            return candidate
        if fallback is None:
            fallback = candidate
    if fallback is not None:
        return fallback
    raise BackendError(
        "collector output contained no JSON object: {0}".format(stripped[:200])
    )


@register
class WindowsBackend(Backend):
    name = "windows"
    os_name = "windows"

    def available(self) -> tuple:
        try:
            system = platform.system() or ""
            if not system.lower().startswith("win"):
                return False, "requires Windows; current platform is {0}".format(
                    system or "unknown"
                )
            shell = powershell()
            if shell is None:
                return False, "PowerShell (pwsh or powershell) not found on PATH"
            return True, "PowerShell CIM collector available ({0})".format(shell)
        except Exception as exc:  # pragma: no cover - defensive
            return False, "probe failed: {0}".format(exc)

    def collect(self, options: BackendOptions) -> BackendResult:
        ok, reason = self.available()
        if not ok:
            raise BackendUnavailable(reason)
        shell = powershell()
        if shell is None:  # pragma: no cover - available() already checked
            raise BackendUnavailable("PowerShell (pwsh or powershell) not found on PATH")
        if not COLLECTOR_SCRIPT.exists():
            raise BackendError(
                "collector script missing: {0}".format(COLLECTOR_SCRIPT)
            )

        command = [
            shell,
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(COLLECTOR_SCRIPT),
            "-Json",
        ]
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=COLLECT_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            raise BackendError(
                "collector timed out after {0}s".format(COLLECT_TIMEOUT)
            )
        except OSError as exc:
            raise BackendError("cannot start collector: {0}".format(exc))

        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "").strip()
            raise BackendError(
                "collector failed (exit {0}): {1}".format(
                    completed.returncode, detail[:800] or "no output"
                )
            )

        raw = _last_json_object(completed.stdout)
        return normalize_raw(
            raw,
            options=options,
            sources=["script:{0}".format(COLLECTOR_SCRIPT)],
        )


__all__ = ["COLLECTOR_SCRIPT", "RAW_SCHEMA", "WindowsBackend", "normalize_raw", "powershell"]
