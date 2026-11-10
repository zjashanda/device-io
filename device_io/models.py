"""Data model for device-io.

The model is deliberately plain: dataclasses that serialise to the dictionaries
defined by :mod:`device_io.schema`. Backends fill ``Port`` and ``AudioEndpoint``
objects; board grouping (:mod:`device_io.boards`) is shared by every backend so
Windows and Linux produce the same shape.
"""

from __future__ import annotations

import platform
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from . import schema


def _normalise_hex(value: Optional[str]) -> Optional[str]:
    """'0x1A86' / '1a86 ' -> '1a86'. Returns None for empty input."""
    if value is None:
        return None
    text = str(value).strip().lower()
    if not text:
        return None
    if text.startswith("0x"):
        text = text[2:]
    return text or None


def _token(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


@dataclass
class DriverInfo:
    family: str = "unknown"
    id: str = ""
    display: str = ""
    status: str = "unknown"
    evidence: str = ""
    requires: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "family": self.family or "unknown",
            "status": self.status or "unknown",
        }
        if self.id:
            data["id"] = self.id
        if self.display:
            data["display"] = self.display
        if self.evidence:
            data["evidence"] = self.evidence
        if self.requires:
            data["requires"] = list(self.requires)
        return data


@dataclass
class Port:
    """One I/O endpoint a skill can open (usually a serial device)."""

    device: str
    os: str
    kind: str = "serial"
    label: str = ""
    transport: Optional[str] = None
    interface: Optional[str] = None
    interface_number: Optional[int] = None
    vid: Optional[str] = None
    pid: Optional[str] = None
    serial: Optional[str] = None
    manufacturer: Optional[str] = None
    product: Optional[str] = None
    board_key: Optional[str] = None
    usb_path: Optional[str] = None
    location: Optional[str] = None
    present: bool = True
    driver: DriverInfo = field(default_factory=DriverInfo)
    extra: Dict[str, Any] = field(default_factory=dict)
    id: str = ""

    def __post_init__(self) -> None:
        self.os = (self.os or "unknown").lower()
        self.kind = self.kind or "serial"
        self.vid = _normalise_hex(self.vid)
        self.pid = _normalise_hex(self.pid)
        self.serial = _token(self.serial)
        self.manufacturer = _token(self.manufacturer)
        self.product = _token(self.product)
        self.label = _token(self.label) or ""
        self.interface = _token(self.interface)
        self.board_key = _token(self.board_key)
        if not self.id:
            self.id = "{0}:{1}".format(self.board_key or "unowned", self.device)

    def to_dict(self) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "id": self.id,
            "kind": self.kind,
            "device": self.device,
            "os": self.os,
            "present": bool(self.present),
            "driver": self.driver.to_dict(),
        }
        for key in (
            "label",
            "transport",
            "interface",
            "interface_number",
            "vid",
            "pid",
            "serial",
            "manufacturer",
            "product",
            "board_key",
            "usb_path",
            "location",
        ):
            value = getattr(self, key)
            if value not in (None, ""):
                data[key] = value
        if self.extra:
            data["extra"] = dict(self.extra)
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Port":
        driver = data.get("driver") or {}
        port = cls(
            device=data.get("device", ""),
            os=data.get("os", "unknown"),
            kind=data.get("kind", "serial"),
            label=data.get("label") or "",
            transport=data.get("transport"),
            interface=data.get("interface"),
            interface_number=data.get("interface_number"),
            vid=data.get("vid"),
            pid=data.get("pid"),
            serial=data.get("serial"),
            manufacturer=data.get("manufacturer"),
            product=data.get("product"),
            board_key=data.get("board_key"),
            usb_path=data.get("usb_path"),
            location=data.get("location"),
            present=bool(data.get("present", True)),
            driver=DriverInfo(
                family=driver.get("family", "unknown"),
                id=driver.get("id", ""),
                display=driver.get("display", ""),
                status=driver.get("status", "unknown"),
                evidence=driver.get("evidence", ""),
                requires=list(driver.get("requires") or []),
            ),
            extra=dict(data.get("extra") or {}),
            id=data.get("id", ""),
        )
        return port


@dataclass
class AudioEndpoint:
    kind: str
    name: str
    board_key: Optional[str] = None
    channels: Optional[int] = None
    vid: Optional[str] = None
    pid: Optional[str] = None
    endpoint_id: Optional[str] = None
    present: bool = True
    extra: Dict[str, Any] = field(default_factory=dict)
    id: str = ""

    def __post_init__(self) -> None:
        self.vid = _normalise_hex(self.vid)
        self.pid = _normalise_hex(self.pid)
        self.board_key = _token(self.board_key)
        if not self.id:
            self.id = "{0}:{1}".format(
                self.board_key or "unowned", self.endpoint_id or self.name
            )

    def to_dict(self) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "id": self.id,
            "kind": self.kind,
            "name": self.name,
            "present": bool(self.present),
        }
        for key in ("board_key", "channels", "vid", "pid", "endpoint_id"):
            value = getattr(self, key)
            if value not in (None, ""):
                data[key] = value
        if self.extra:
            data["extra"] = dict(self.extra)
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AudioEndpoint":
        return cls(
            kind=data.get("kind", "audio-capture"),
            name=data.get("name", ""),
            board_key=data.get("board_key"),
            channels=data.get("channels"),
            vid=data.get("vid"),
            pid=data.get("pid"),
            endpoint_id=data.get("endpoint_id"),
            present=bool(data.get("present", True)),
            extra=dict(data.get("extra") or {}),
            id=data.get("id", ""),
        )


@dataclass
class Snapshot:
    """One observation of the machine's device tree."""

    backend: str = "auto"
    os: str = "unknown"
    ports: List[Port] = field(default_factory=list)
    audio: List[AudioEndpoint] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    sources: List[str] = field(default_factory=list)
    host: str = ""
    dry_run: bool = False
    generated_at: str = ""

    def __post_init__(self) -> None:
        if not self.generated_at:
            self.generated_at = schema.now_bj()
        if not self.host:
            self.host = platform.node()

    # -- lookups ---------------------------------------------------------
    def port_by_device(self, device: str) -> Optional[Port]:
        wanted = device.strip().lower()
        for port in self.ports:
            if port.device.strip().lower() == wanted:
                return port
        return None

    def serial_ports(self) -> List[Port]:
        return [port for port in self.ports if port.kind == "serial"]

    def boards(self) -> List[Dict[str, Any]]:
        from .boards import build_boards

        return build_boards(self)

    def to_dict(self, include_boards: bool = True) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "schema": schema.SCHEMAS["snapshot"],
            "generated_at": schema.require_bj_timestamp(self.generated_at, "snapshot"),
            "backend": self.backend,
            "os": self.os,
            "host": self.host,
            "dry_run": bool(self.dry_run),
            "ports": [port.to_dict() for port in self.ports],
            "audio": [item.to_dict() for item in self.audio],
            "warnings": list(self.warnings),
            "sources": list(self.sources),
        }
        if include_boards:
            data["boards"] = self.boards()
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Snapshot":
        if data.get("schema") != schema.SCHEMAS["snapshot"]:
            raise schema.SchemaError(
                "expected schema {0}, got {1!r}".format(
                    schema.SCHEMAS["snapshot"], data.get("schema")
                )
            )
        snapshot = cls(
            backend=data.get("backend", "fixture"),
            os=data.get("os", "unknown"),
            ports=[Port.from_dict(item) for item in data.get("ports") or []],
            audio=[AudioEndpoint.from_dict(item) for item in data.get("audio") or []],
            warnings=list(data.get("warnings") or []),
            sources=list(data.get("sources") or []),
            host=data.get("host", ""),
            dry_run=bool(data.get("dry_run", False)),
            generated_at=data.get("generated_at", ""),
        )
        return snapshot
