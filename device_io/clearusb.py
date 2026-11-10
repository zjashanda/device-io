"""Hidden / ghost device cleanup.

Windows keeps device instances for hardware that is no longer plugged in. After
enough re-plugs ``COM13`` may be occupied by a ghost, and a new board shows up
as ``COM27``. ``clearusb`` removes only those non-present instances, and only
when the caller passes ``apply``.

Contract: preview is the default. Linux has no equivalent concept, so the Linux
path returns ``supported: false`` with a reason instead of pretending to work.
"""

from __future__ import annotations

import json
import platform
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import schema

SCRIPT = Path(__file__).resolve().parents[1] / "windows" / "Clear-HiddenDevice.ps1"
DEFAULT_TIMEOUT = 180


class ClearUsbError(RuntimeError):
    """Cleanup could not even be planned."""


def host_os() -> str:
    system = platform.system().lower()
    if system.startswith("win"):
        return "windows"
    if system.startswith("linux"):
        return "linux"
    return system or "unknown"


def _unsupported(reason: str, apply: bool) -> Dict[str, Any]:
    return {
        "schema": schema.SCHEMAS["clear_plan"],
        "generated_at": schema.now_bj(),
        "os": host_os(),
        "supported": False,
        "apply": bool(apply),
        "reason": reason,
        "items": [],
        "summary": {"candidates": 0, "removed": 0},
    }


def powershell() -> Optional[str]:
    for name in ("pwsh", "powershell"):
        found = shutil.which(name)
        if found:
            return found
    return None


def plan(
    apply: bool = False,
    board: Optional[str] = None,
    timeout: int = DEFAULT_TIMEOUT,
    script: Optional[Path] = None,
) -> Dict[str, Any]:
    """Build (and optionally execute) a cleanup plan.

    ``board`` filters to one USB VID:PID, e.g. ``1a86:7523``.
    """
    os_name = host_os()
    if os_name != "windows":
        return _unsupported(
            "ghost device instances are a Windows PnP concept; nothing to clean on {0}".format(
                os_name
            ),
            apply,
        )
    shell = powershell()
    if shell is None:
        return _unsupported("PowerShell not found on PATH (pwsh or powershell)", apply)
    target = Path(script) if script else SCRIPT
    if not target.exists():
        raise ClearUsbError("collector script missing: {0}".format(target))

    command = [shell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(target), "-Json"]
    if apply:
        command.append("-Apply")
    if board:
        command.extend(["-Board", board])
    completed = subprocess.run(
        command, capture_output=True, text=True, timeout=timeout
    )
    if completed.returncode != 0:
        raise ClearUsbError(
            "collector failed (exit {0}): {1}".format(
                completed.returncode, (completed.stderr or completed.stdout or "").strip()[:800]
            )
        )
    try:
        data = json.loads(_last_json(completed.stdout))
    except ValueError as exc:
        raise ClearUsbError("collector returned non-JSON output: {0}".format(exc))
    items: List[Dict[str, Any]] = list(data.get("items") or [])
    removed = sum(1 for item in items if item.get("removed"))
    return {
        "schema": schema.SCHEMAS["clear_plan"],
        "generated_at": schema.now_bj(),
        "os": "windows",
        "supported": True,
        "apply": bool(apply),
        "reason": "",
        "items": items,
        "summary": {
            "candidates": len(items),
            "removed": removed,
            "failed": sum(1 for item in items if item.get("error")),
        },
    }


def _last_json(text: str) -> str:
    """PowerShell may print warnings before JSON; keep the last JSON object."""
    stripped = (text or "").strip()
    if not stripped:
        return stripped
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start < 0 or end <= start:
        return stripped
    return stripped[start : end + 1]


def render(document: Dict[str, Any]) -> str:
    lines: List[str] = []
    lines.append(
        "clearusb {0}  supported={1}  apply={2}".format(
            document.get("generated_at", ""),
            document.get("supported"),
            document.get("apply"),
        )
    )
    if document.get("reason"):
        lines.append("  {0}".format(document["reason"]))
    for item in document.get("items") or []:
        state = "removed" if item.get("removed") else ("error" if item.get("error") else "candidate")
        lines.append(
            "  {0:<9} {1:<8} {2}  ({3})".format(
                state,
                item.get("class") or "?",
                item.get("friendly_name") or item.get("instance_id"),
                item.get("reason") or "",
            )
        )
    summary = document.get("summary") or {}
    lines.append(
        "  候选 {0} / 已移除 {1} / 失败 {2}".format(
            summary.get("candidates", 0), summary.get("removed", 0), summary.get("failed", 0)
        )
    )
    return "\n".join(lines)


__all__ = ["ClearUsbError", "host_os", "plan", "powershell", "render"]
