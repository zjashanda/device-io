"""Board grouping shared by every backend.

A *board* is one physical USB device: a CH344 quad adapter is one board with
four serial interfaces, a ListenAI VCOM dock is one board with one serial port
and one audio endpoint. Backends only supply ``board_key`` (the physical
upstream identity: Windows ContainerID/PnP parent, Linux upstream USB path);
the grouping, ordering and titling rules live here so both platforms present
the same tree.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from .drivers import adapter_for
from .models import AudioEndpoint, Port, Snapshot

_INTERFACE_RE = re.compile(r"([A-Za-z]+)[\s\-_#]*([A-Za-z0-9]+)")
_UNOWNED = "unowned"


def interface_sort_key(port: Port) -> Tuple[int, Any]:
    """Order interfaces the way the bench labels them: A, B, C, D / Ch A..H."""
    token = (port.interface or "").strip()
    match = _INTERFACE_RE.search(token)
    if match and match.group(2).isdigit():
        return (0, int(match.group(2)))
    if port.interface_number is not None:
        return (1, port.interface_number)
    return (2, _natural(port.device))


def _natural(value: str) -> Tuple:
    """Natural sort so ttyUSB2 < ttyUSB10 and COM9 < COM13."""
    parts = re.split(r"(\d+)", value or "")
    return tuple((0, int(part)) if part.isdigit() else (1, part.lower()) for part in parts if part)


def port_sort_key(port: Port) -> Tuple:
    return (port.kind != "serial", interface_sort_key(port), _natural(port.device))


def _board_title(index: int, ports: List[Port], audio: List[AudioEndpoint]) -> str:
    sample = ports[0] if ports else None
    product = ""
    if sample and sample.product:
        product = sample.product.strip()
    for endpoint in audio:
        if not product and endpoint.name:
            product = endpoint.name.strip()
    vendor = (sample.manufacturer or "") if sample else ""
    blob = "{0} {1}".format(product, vendor).lower()
    if "listenai" in blob:
        return "ListenAI 板卡 #{0}".format(index)
    if product:
        return "{0} #{1}".format(product, index)
    if sample and sample.vid:
        return "USB {0}:{1} #{2}".format(sample.vid, sample.pid or "????", index)
    return "未识别设备 #{0}".format(index)


def _board_adapter(ports: List[Port]) -> Optional[Dict[str, str]]:
    for port in ports:
        adapter = adapter_for(port.vid, port.pid, port.product)
        if adapter is not None:
            return {
                "id": adapter.id,
                "display": adapter.display,
                "confidence": adapter.confidence,
            }
    return None


def build_boards(snapshot: Snapshot) -> List[Dict[str, Any]]:
    grouped: Dict[str, Dict[str, List[Any]]] = {}
    for port in snapshot.serial_ports():
        key = port.board_key or "{0}:{1}".format(_UNOWNED, port.device)
        grouped.setdefault(key, {"ports": [], "audio": []})["ports"].append(port)
    for endpoint in snapshot.audio:
        key = endpoint.board_key
        if not key:
            continue
        grouped.setdefault(key, {"ports": [], "audio": []})["audio"].append(endpoint)

    boards: List[Dict[str, Any]] = []
    for index, key in enumerate(
        sorted(grouped, key=lambda item: (item.startswith(_UNOWNED), item)), start=1
    ):
        ports = sorted(grouped[key]["ports"], key=port_sort_key)  # type: ignore[arg-type]
        audio = sorted(grouped[key]["audio"], key=lambda item: (item.kind, item.name))  # type: ignore[attr-defined]
        sample = ports[0] if ports else None
        warnings: List[str] = []
        if key.startswith(_UNOWNED):
            warnings.append("没有 USB 上游信息，无法确认物理板卡归属")
        entry: Dict[str, Any] = {
            "key": key,
            "title": _board_title(index, ports, audio),
            "vid": sample.vid if sample else (audio[0].vid if audio else None),
            "pid": sample.pid if sample else (audio[0].pid if audio else None),
            "serial": sample.serial if sample else None,
            "product": sample.product if sample else None,
            "transport": sample.transport if sample else None,
            "port_ids": [port.id for port in ports],
            "audio_ids": [endpoint.id for endpoint in audio],
            "warnings": warnings,
        }
        adapter = _board_adapter(ports)
        if adapter:
            entry["adapter"] = adapter
        boards.append(entry)
    return boards


def unassigned_audio(snapshot: Snapshot) -> List[AudioEndpoint]:
    return [endpoint for endpoint in snapshot.audio if not endpoint.board_key]


def boards_schema_version() -> str:
    from . import schema

    return schema.SCHEMAS["boards"]


__all__ = ["build_boards", "unassigned_audio", "interface_sort_key", "port_sort_key"]
