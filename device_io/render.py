"""Human and machine renderers.

``json`` is the contract; ``tree`` mirrors what bench operators already know
from usb-tree, so migrating does not cost them the mental model.
"""

from __future__ import annotations

import csv
import io
import json
from typing import Any, Dict, Iterable, List, Optional

from .models import AudioEndpoint, Port, Snapshot

FORMATS = ("tree", "plain", "json", "csv", "md")


def dumps(document: Any) -> str:
    return json.dumps(document, ensure_ascii=False, indent=2, sort_keys=False)


def _port_line(port: Port) -> str:
    bits = [port.device]
    if port.interface:
        bits.append("[{0}]".format(port.interface))
    if port.label:
        bits.append(port.label)
    if port.driver and port.driver.status not in ("", "ok"):
        bits.append("driver={0}".format(port.driver.status))
    return "  ".join(bits)


def render_tree(document: Dict[str, Any]) -> str:
    boards = document.get("boards") or []
    ports = {item["id"]: item for item in document.get("ports") or []}
    audio = {item["id"]: item for item in document.get("audio") or []}
    lines: List[str] = []
    referenced: List[str] = []
    lines.append("物理 USB 板卡（{0} 个）".format(len(boards)))
    for board in boards:
        title = board.get("title") or board.get("key")
        adapter = board.get("adapter") or {}
        suffix = ""
        if adapter:
            suffix = "  <{0}>".format(adapter.get("display") or adapter.get("id"))
        identity = ""
        if board.get("vid"):
            identity = " ({0}:{1})".format(board.get("vid"), board.get("pid") or "????")
        lines.append("├─ {0}{1}{2}".format(title, identity, suffix))
        for port_id in board.get("port_ids") or []:
            port = ports.get(port_id)
            if not port:
                continue
            label = port.get("label") or port.get("product") or "串口"
            lines.append(
                "│   ├─ {0}  {1}  {2}".format(label, port.get("interface") or "-", port.get("device"))
            )
        board_audio = [audio[audio_id] for audio_id in board.get("audio_ids") or [] if audio_id in audio]
        referenced.extend(item["id"] for item in board_audio)
        for position, endpoint in enumerate(board_audio, start=1):
            channels = endpoint.get("channels")
            lines.append(
                "│   {0} {1} {2}{3}  {4}".format(
                    "└─" if position == len(board_audio) else "├─",
                    "Capture" if endpoint.get("kind") == "audio-capture" else "Render",
                    "{0}ch ".format(channels) if channels else "",
                    endpoint.get("name"),
                ).rstrip()
            )
        for warning in board.get("warnings") or []:
            lines.append("│   ! {0}".format(warning))
    if not boards:
        lines.append("└─ （未发现 USB 串口设备）")
    orphans = [endpoint for endpoint in audio.values() if endpoint["id"] not in referenced]
    orphans.sort(key=lambda item: (item.get("kind") or "", item.get("name") or ""))
    if orphans:
        lines.append("未归属音频端点（{0} 个，无 USB 上游信息）".format(len(orphans)))
        for endpoint in orphans:
            channels = endpoint.get("channels")
            lines.append(
                "├─ {0} {1}{2}  {3}".format(
                    "Capture" if endpoint.get("kind") == "audio-capture" else "Render",
                    "{0}ch ".format(channels) if channels else "",
                    endpoint.get("name"),
                ).rstrip()
            )
    for warning in document.get("warnings") or []:
        lines.append("! {0}".format(warning))
    return "\n".join(lines)


def render_plain(document: Dict[str, Any]) -> str:
    rows = sorted(
        document.get("ports") or [],
        key=lambda item: (item.get("board_key") or "zz", item.get("device") or ""),
    )
    lines: List[str] = []
    for port in rows:
        lines.append(
            "\t".join(
                [
                    port.get("device") or "",
                    port.get("interface") or "",
                    port.get("label") or "",
                    port.get("board_key") or "",
                    "0x{0}:0x{1}".format(port.get("vid") or "????", port.get("pid") or "????"),
                    (port.get("driver") or {}).get("family") or "",
                ]
            )
        )
    return "\n".join(lines)


def render_csv(document: Dict[str, Any]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(["kind", "device", "interface", "label", "board_key", "vid", "pid", "serial", "driver", "status"])
    for port in document.get("ports") or []:
        writer.writerow(
            [
                port.get("kind", ""),
                port.get("device", ""),
                port.get("interface", ""),
                port.get("label", ""),
                port.get("board_key", ""),
                port.get("vid", ""),
                port.get("pid", ""),
                port.get("serial", ""),
                (port.get("driver") or {}).get("family", ""),
                (port.get("driver") or {}).get("status", ""),
            ]
        )
    return buffer.getvalue().rstrip("\n")


def render_md(document: Dict[str, Any]) -> str:
    lines: List[str] = []
    lines.append("| 板卡 | 接口 | 设备 | VID:PID | 驱动 | 标签 |")
    lines.append("|---|---|---|---|---|---|")
    ports = {item["id"]: item for item in document.get("ports") or []}
    for board in document.get("boards") or []:
        for port_id in board.get("port_ids") or []:
            port = ports.get(port_id)
            if not port:
                continue
            driver = (port.get("driver") or {})
            lines.append(
                "| {0} | {1} | `{2}` | {3}:{4} | {5} | {6} |".format(
                    board.get("title", ""),
                    port.get("interface") or "-",
                    port.get("device") or "",
                    port.get("vid") or "????",
                    port.get("pid") or "????",
                    driver.get("family") or "-",
                    port.get("label") or "-",
                )
            )
    return "\n".join(lines)


def render_document(document: Dict[str, Any], fmt: str) -> str:
    fmt = (fmt or "tree").lower()
    if fmt == "json":
        return dumps(document)
    if fmt == "tree":
        return render_tree(document)
    if fmt == "plain":
        return render_plain(document)
    if fmt == "csv":
        return render_csv(document)
    if fmt == "md":
        return render_md(document)
    raise ValueError("unknown format {0!r}; expected one of {1}".format(fmt, ", ".join(FORMATS)))


def render_role_map(document: Dict[str, Any], fmt: str = "text") -> str:
    if fmt == "json":
        return dumps(document)
    lines: List[str] = []
    lines.append(
        "角色解析 {0}  backend={1}  结果={2}".format(
            document.get("generated_at", ""),
            document.get("backend", "?"),
            "OK" if document.get("ok") else "FAIL",
        )
    )
    for name, entry in sorted((document.get("roles") or {}).items()):
        lines.append(
            "  {0:<10} {1:<9} {2:<8} {3}".format(
                name,
                entry.get("state", "?"),
                entry.get("port") or "-",
                entry.get("reason") or "",
            )
        )
        if entry.get("candidates"):
            lines.append("             候选: {0}".format(", ".join(entry["candidates"])))
    if document.get("unassigned_ports"):
        lines.append("  未分配端口: {0}".format(", ".join(document["unassigned_ports"])))
    for warning in document.get("warnings") or []:
        lines.append("  ! {0}".format(warning))
    return "\n".join(lines)


__all__ = [
    "FORMATS",
    "dumps",
    "render_csv",
    "render_document",
    "render_md",
    "render_plain",
    "render_role_map",
    "render_tree",
]
