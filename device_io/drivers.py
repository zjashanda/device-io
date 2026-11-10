"""Driver adapters.

Two jobs:

* name the driver family behind a port from evidence the backend already
  collected (USB VID/PID, Windows driver service name, Linux kernel module);
* tell a caller whether the driver it needs is actually there, so a bench
  failure is reported as "driver missing" instead of "port missing".

Third-party boards are supported without patching this file: ``load_plugin``
reads an ``ark.device-adapter.v1`` JSON file and adds adapters to the registry.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from . import schema
from .models import Port, Snapshot

PLUGIN_SCHEMA = "ark.device-adapter.v1"

_FAMILY_BY_SERVICE = (
    ("ch343", "ch343"),
    ("ch341", "ch34x"),
    ("ch34x", "ch34x"),
    ("wch", "ch34x"),
    ("cp210", "cp210x"),
    ("silabser", "cp210x"),
    ("ftdibus", "ftdi"),
    ("ftdi", "ftdi"),
    ("usbser", "cdc-acm"),
    ("cdc", "cdc-acm"),
)

_LINUX_MODULE = {
    "ch34x": "ch341",
    "ch343": "ch343",
    "cp210x": "cp210x",
    "ftdi": "ftdi_sio",
    "cdc-acm": "cdc_acm",
    "listenai-vcom": "cdc_acm",
    "usb-serial-generic": "usbserial",
}

_DRIVER_PACKAGE = {
    "ch34x": "WCH CH341SER/CH343SER driver",
    "ch343": "WCH CH343SER driver",
    "cp210x": "Silicon Labs CP210x VCP driver",
    "ftdi": "FTDI CDM driver (FTDIBUS)",
    "cdc-acm": "inbox usbser.sys (Windows 10+)",
    "listenai-vcom": "inbox usbser.sys (CDC-ACM)",
    "usb-serial-generic": "vendor USB-serial driver",
}


@dataclass(frozen=True)
class DriverAdapter:
    id: str
    display: str
    family: str
    vid: Tuple[str, ...] = ()
    pid: Tuple[str, ...] = ()
    product_contains: Tuple[str, ...] = ()
    interfaces: Tuple[str, ...] = ()
    platforms: Tuple[str, ...] = ("windows", "linux")
    confidence: str = "likely"
    requires: Tuple[str, ...] = ()
    notes: str = ""

    def matches(
        self,
        vid: Optional[str],
        pid: Optional[str],
        product: Optional[str] = None,
        interface: Optional[str] = None,
    ) -> bool:
        if self.vid:
            if not vid or vid.lower() not in self.vid:
                return False
        if self.pid:
            if not pid or pid.lower() not in self.pid:
                return False
        if self.product_contains:
            blob = (product or "").lower()
            if not any(token in blob for token in self.product_contains):
                return False
        if self.interfaces:
            blob = (interface or "").lower()
            if not any(token in blob for token in self.interfaces):
                return False
        return True

    def to_dict(self) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "id": self.id,
            "display": self.display,
            "family": self.family,
            "confidence": self.confidence,
            "platforms": list(self.platforms),
        }
        if self.vid:
            data["vid"] = list(self.vid)
        if self.pid:
            data["pid"] = list(self.pid)
        if self.product_contains:
            data["product_contains"] = list(self.product_contains)
        if self.interfaces:
            data["interfaces"] = list(self.interfaces)
        if self.requires:
            data["requires"] = list(self.requires)
        if self.notes:
            data["notes"] = self.notes
        return data


BUILTIN_ADAPTERS: Tuple[DriverAdapter, ...] = (
    DriverAdapter(
        id="listenai-vcom",
        display="ListenAI VCOM (CDC-ACM)",
        family="listenai-vcom",
        vid=("8765",),
        confidence="certain",
        requires=("usbser",),
        notes="ListenAI bench dock virtual COM, single interface",
    ),
    DriverAdapter(
        id="ch344-quad",
        display="WCH CH344 Quad_Serial (4 路)",
        family="ch34x",
        vid=("1a86",),
        pid=("55d3", "55d4"),
        confidence="certain",
        requires=("CH343SER",),
        notes="interfaces SERIAL-A..D",
    ),
    DriverAdapter(
        id="ch348-octa",
        display="WCH CH348 (8 路)",
        family="ch34x",
        vid=("1a86",),
        pid=("55d9",),
        confidence="certain",
        requires=("CH343SER",),
        notes="interfaces Ch A..H",
    ),
    DriverAdapter(
        id="ch343-single",
        display="WCH CH343 single UART",
        family="ch343",
        vid=("1a86",),
        pid=("55d2",),
        requires=("CH343SER",),
    ),
    DriverAdapter(
        id="ch340-single",
        display="WCH CH340/CH341 single UART",
        family="ch34x",
        vid=("1a86",),
        pid=("7523", "5523"),
        requires=("CH341SER",),
    ),
    DriverAdapter(
        id="cp210x-bridge",
        display="Silicon Labs CP210x bridge",
        family="cp210x",
        vid=("10c4",),
        pid=("ea60", "ea70", "ea71"),
        requires=("silabser",),
    ),
    DriverAdapter(
        id="ftdi-serial",
        display="FTDI FT232/FT2232 serial",
        family="ftdi",
        vid=("0403",),
        pid=("6001", "6010", "6011", "6014", "6015"),
        requires=("FTDIBUS",),
    ),
    DriverAdapter(
        id="cdc-acm-generic",
        display="USB CDC-ACM serial",
        family="cdc-acm",
        confidence="guess",
        requires=("usbser",),
        notes="class-level match (interface class 02/0a); identity unknown",
    ),
)

_REGISTRY: List[DriverAdapter] = list(BUILTIN_ADAPTERS)


def register_adapter(adapter: DriverAdapter, replace: bool = False) -> None:
    global _REGISTRY
    existing = [item for item in _REGISTRY if item.id == adapter.id]
    if existing and not replace:
        raise ValueError("adapter id already registered: {0}".format(adapter.id))
    _REGISTRY = [item for item in _REGISTRY if item.id != adapter.id] + [adapter]


def adapters() -> List[DriverAdapter]:
    return list(_REGISTRY)


def reset_registry() -> None:
    global _REGISTRY
    _REGISTRY = list(BUILTIN_ADAPTERS)


def adapter_for(
    vid: Optional[str], pid: Optional[str], product: Optional[str] = None, interface: Optional[str] = None
) -> Optional[DriverAdapter]:
    best: Optional[DriverAdapter] = None
    for adapter in _REGISTRY:
        if not adapter.matches(vid, pid, product, interface):
            continue
        if best is None or _specificity(adapter) > _specificity(best):
            best = adapter
    return best


def _specificity(adapter: DriverAdapter) -> int:
    score = 0
    if adapter.vid:
        score += 1
    if adapter.pid:
        score += 2
    if adapter.product_contains:
        score += 1
    if adapter.confidence == "certain":
        score += 1
    return score


def driver_family(port: Port) -> str:
    """Best-effort family for one port; never raises."""
    if port.driver and port.driver.family and port.driver.family != "unknown":
        return port.driver.family
    service = " ".join(
        str(port.driver.id or "").lower().split()
        + [str(port.driver.display or "").lower()]
    ) if port.driver else ""
    for token, family in _FAMILY_BY_SERVICE:
        if token in service:
            return family
    adapter = adapter_for(port.vid, port.pid, port.product, port.interface)
    if adapter is not None:
        return adapter.family
    if port.vid in ("1a86",):
        return "ch34x"
    if (port.product or "").lower().find("cdc") >= 0:
        return "cdc-acm"
    return "unknown"


def linux_module_loaded(module: str, root: str = "/sys/module") -> bool:
    return bool(module) and os.path.isdir(os.path.join(root, module))


def load_plugin(path: str, replace: bool = False) -> List[str]:
    """Load adapters from a JSON plugin file. Returns human-readable notices."""
    file_path = Path(path).expanduser()
    data = json.loads(file_path.read_text(encoding="utf-8"))
    if data.get("schema") != PLUGIN_SCHEMA:
        raise schema.SchemaError(
            "{0}: expected schema {1}, got {2!r}".format(file_path.name, PLUGIN_SCHEMA, data.get("schema"))
        )
    notices: List[str] = []
    for entry in data.get("adapters") or []:
        adapter = DriverAdapter(
            id=str(entry["id"]),
            display=str(entry.get("display") or entry["id"]),
            family=str(entry.get("family") or "unknown"),
            vid=tuple(str(item).lower() for item in entry.get("vid") or ()),
            pid=tuple(str(item).lower() for item in entry.get("pid") or ()),
            product_contains=tuple(str(item).lower() for item in entry.get("product_contains") or ()),
            interfaces=tuple(str(item).lower() for item in entry.get("interfaces") or ()),
            platforms=tuple(entry.get("platforms") or ("windows", "linux")),
            confidence=str(entry.get("confidence") or "likely"),
            requires=tuple(entry.get("requires") or ()),
            notes=str(entry.get("notes") or ""),
        )
        try:
            register_adapter(adapter, replace=replace)
            notices.append("loaded adapter {0} from {1}".format(adapter.id, file_path.name))
        except ValueError as exc:
            notices.append("skipped: {0}".format(exc))
    return notices


def _package_hint(family: str, os_name: str) -> str:
    if os_name == "windows":
        return _DRIVER_PACKAGE.get(family, "vendor USB-serial driver")
    module = _LINUX_MODULE.get(family)
    return "kernel module {0}".format(module) if module else "vendor USB-serial module"


def check_drivers(
    snapshot: Snapshot,
    required: Optional[Sequence[str]] = None,
    os_name: Optional[str] = None,
    dry_run: bool = False,
    module_root: str = "/sys/module",
) -> Dict[str, Any]:
    """Build an ``ark.device-driver-check.v1`` document."""
    system = (os_name or snapshot.os or "unknown").lower()
    wanted: List[str] = []
    for family in required or ():
        family = str(family).strip().lower()
        if family and family not in wanted:
            wanted.append(family)
    present: Dict[str, List[Port]] = {}
    for port in snapshot.serial_ports():
        family = driver_family(port)
        present.setdefault(family, []).append(port)

    entries: List[Dict[str, Any]] = []
    for family in wanted:
        ports = present.get(family) or []
        entry: Dict[str, Any] = {
            "family": family,
            "display": _display_for_family(family),
            "required": True,
            "ports": [port.device for port in ports],
        }
        if not ports:
            entry["status"] = "missing"
            entry["evidence"] = "no port with this driver family is present"
            entry["advice"] = "connect the board, or check the cable/port before installing anything"
        elif system == "linux":
            module = _LINUX_MODULE.get(family, "")
            loaded = linux_module_loaded(module, module_root) if module else False
            entry["status"] = "ok" if loaded else "unknown"
            entry["evidence"] = "module {0} {1}".format(module or "?", "loaded" if loaded else "not found in /sys/module")
            if not loaded and not dry_run:
                entry["advice"] = "port exists but module not loaded; check dmesg before loading it"
        else:
            statuses = {port.driver.status if port.driver else "unknown" for port in ports}
            if statuses & {"ok"}:
                entry["status"] = "ok"
            elif statuses & {"mismatch"}:
                entry["status"] = "mismatch"
            else:
                entry["status"] = "unknown"
            entry["evidence"] = "service status: {0}".format(", ".join(sorted(statuses)))
            if entry["status"] != "ok":
                entry["advice"] = _package_hint(family, system)
        entries.append(entry)

    unexpected = [
        {"family": family, "ports": [port.device for port in ports]}
        for family, ports in sorted(present.items())
        if family not in wanted and family != "unknown"
    ]
    ok = all(entry["status"] == "ok" for entry in entries)
    document: Dict[str, Any] = {
        "schema": schema.SCHEMAS["driver_check"],
        "generated_at": schema.now_bj(),
        "os": system,
        "dry_run": bool(dry_run),
        "ok": ok,
        "drivers": entries,
        "warnings": [],
    }
    if unexpected:
        document["warnings"].append(
            "drivers present but not required: {0}".format(
                ", ".join("{0}({1})".format(item["family"], ",".join(item["ports"])) for item in unexpected)
            )
        )
    return document


def _display_for_family(family: str) -> str:
    for adapter in _REGISTRY:
        if adapter.family == family:
            return adapter.display
    return family


__all__ = [
    "BUILTIN_ADAPTERS",
    "DriverAdapter",
    "PLUGIN_SCHEMA",
    "adapter_for",
    "adapters",
    "check_drivers",
    "driver_family",
    "linux_module_loaded",
    "load_plugin",
    "register_adapter",
    "reset_registry",
]
