"""Port role resolution.

``venusahisiburner`` used to match ``COM3``/``COM4`` by name, which breaks the
moment Windows renumbers or a second board is plugged in. This module answers
the question the burn flow actually asks: *which port plays the log / burn /
control role for this board right now?* Five states are possible, and every
non-success state carries a reason the bench operator can act on:

``resolved``  exactly one port matched the role's expectations
``pinned``    the caller pinned a device name (with ``pin``) and it is present
``ambiguous`` several ports matched and nothing could break the tie
``missing``   no port matched
``conflict``  the same port was claimed twice, or a pin contradicts the board

Nothing here opens a port. Resolution is read-only.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from . import schema
from .drivers import driver_family
from .models import Port, Snapshot

_EXPECT_KEYS = ("interface", "driver", "product", "vid", "pid")


class SpecError(ValueError):
    """The roles spec is malformed."""


def load_spec(source: Any) -> Dict[str, Any]:
    """Load and validate a roles spec from a path, dict or JSON string."""
    if isinstance(source, dict):
        data = source
    else:
        text = str(source)
        path = Path(text).expanduser()
        try:
            if path.exists():
                data = json.loads(path.read_text(encoding="utf-8"))
            else:
                data = json.loads(text)
        except (OSError, ValueError) as exc:
            raise SpecError("cannot read roles spec {0}: {1}".format(source, exc))
    return validate_spec(data)


def validate_spec(data: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(data, dict):
        raise SpecError("roles spec must be a JSON object")
    if data.get("schema") != schema.SCHEMAS["roles_spec"]:
        raise SpecError(
            "expected schema {0}, got {1!r}".format(schema.SCHEMAS["roles_spec"], data.get("schema"))
        )
    roles = data.get("roles")
    if not isinstance(roles, list) or not roles:
        raise SpecError("roles spec needs a non-empty 'roles' array")
    seen = set()
    normalised: List[Dict[str, Any]] = []
    for index, role in enumerate(roles):
        if not isinstance(role, dict):
            raise SpecError("roles[{0}] must be an object".format(index))
        name = str(role.get("role") or "").strip()
        if not name:
            raise SpecError("roles[{0}] needs a 'role' name".format(index))
        if name in seen:
            raise SpecError("duplicate role name: {0}".format(name))
        seen.add(name)
        item = dict(role)
        item["role"] = name
        item["required"] = bool(role.get("required", True))
        normalised.append(item)
    out = dict(data)
    out["roles"] = normalised
    out["strict"] = bool(data.get("strict", True))
    return out


def _board_matches(snapshot: Snapshot, port: Port, board: Dict[str, Any]) -> bool:
    if not board:
        return True
    checks = {
        "vid": port.vid,
        "pid": port.pid,
        "serial": port.serial,
        "product": port.product,
        "board_key": port.board_key,
    }
    for key, actual in checks.items():
        expected = board.get(key)
        if expected in (None, ""):
            continue
        if str(expected).strip().lower() != str(actual or "").strip().lower():
            return False
    return True


def _expectations(role: Dict[str, Any]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for key in _EXPECT_KEYS:
        value = role.get(key)
        if value not in (None, ""):
            out[key] = str(value).strip()
    return out


def _matches(port: Port, expect: Dict[str, str]) -> Tuple[bool, List[str]]:
    misses: List[str] = []
    for key, wanted in expect.items():
        if key == "interface":
            actual = port.interface or ""
        elif key == "driver":
            actual = driver_family(port)
        elif key == "product":
            actual = port.product or ""
        else:
            actual = getattr(port, key) or ""
        if wanted.lower() not in str(actual).lower():
            misses.append("{0}={1!r} != {2!r}".format(key, actual, wanted))
    return (not misses, misses)


def _role_entry(role: str, state: str, reason: str) -> Dict[str, Any]:
    return {
        "role": role,
        "state": state,
        "port": None,
        "port_id": None,
        "board_key": None,
        "driver_family": None,
        "reason": reason,
        "candidates": [],
    }


def resolve(
    snapshot: Snapshot,
    spec: Dict[str, Any],
    strict: Optional[bool] = None,
) -> Dict[str, Any]:
    """Resolve roles against a snapshot and return ``ark.device-role-map.v1``."""
    spec = validate_spec(spec)
    if strict is None:
        strict = bool(spec.get("strict", True))
    board = spec.get("board") or {}
    ports = [port for port in snapshot.serial_ports() if port.present]
    scoped = [port for port in ports if _board_matches(snapshot, port, board)]
    warnings: List[str] = []
    if board and not scoped:
        warnings.append(
            "board constraint matched no port: {0}".format(
                ", ".join("{0}={1}".format(k, v) for k, v in sorted(board.items()))
            )
        )

    results: Dict[str, Dict[str, Any]] = {}
    for role in spec["roles"]:
        name = role["role"]
        expect = _expectations(role)
        pin = role.get("pin")
        allow = [str(item).strip().lower() for item in role.get("allow") or []]

        if pin:
            found = [port for port in ports if port.device.strip().lower() == str(pin).strip().lower()]
            if not found:
                entry = _role_entry(name, "missing", "pinned device {0} is not present".format(pin))
            elif not _board_matches(snapshot, found[0], board):
                entry = _role_entry(
                    name,
                    "conflict",
                    "pinned device {0} belongs to another board".format(pin),
                )
                entry["port"] = found[0].device
                entry["port_id"] = found[0].id
            else:
                ok, misses = _matches(found[0], expect)
                entry = _role_entry(
                    name,
                    "pinned",
                    "caller pinned {0}".format(pin)
                    + ("" if ok else "; note: " + "; ".join(misses)),
                )
                entry["port"] = found[0].device
                entry["port_id"] = found[0].id
                entry["board_key"] = found[0].board_key
                entry["driver_family"] = driver_family(found[0])
            entry["expect"] = expect
            results[name] = entry
            continue

        candidates: List[Port] = []
        misses_by_port: Dict[str, List[str]] = {}
        for port in scoped:
            ok, misses = _matches(port, expect)
            if ok:
                candidates.append(port)
            else:
                misses_by_port[port.device] = misses

        if not candidates and allow:
            for port in scoped:
                if port.device.strip().lower() in allow:
                    candidates.append(port)

        if len(candidates) == 1:
            port = candidates[0]
            entry = _role_entry(name, "resolved", _resolved_reason(port, expect))
            entry["port"] = port.device
            entry["port_id"] = port.id
            entry["board_key"] = port.board_key
            entry["driver_family"] = driver_family(port)
        elif not candidates:
            hint = ""
            if scoped:
                nearest = sorted(misses_by_port.items())[0]
                hint = " (closest: {0} - {1})".format(nearest[0], "; ".join(nearest[1]))
            entry = _role_entry(name, "missing", "no port matched " + _expect_text(expect) + hint)
        else:
            entry = _role_entry(
                name,
                "ambiguous",
                "{0} ports matched {1}; pin one or narrow the expectation".format(
                    len(candidates), _expect_text(expect)
                ),
            )
            entry["candidates"] = [port.device for port in candidates]
        entry["expect"] = expect
        results[name] = entry

    # A port claimed by two roles is a conflict for both; better to stop than to
    # let the burn flow talk to the wrong interface.
    claims: Dict[str, List[str]] = {}
    for name, entry in results.items():
        if entry["port"] and entry["state"] in ("resolved", "pinned"):
            claims.setdefault(str(entry["port"]).lower(), []).append(name)
    for device, names in claims.items():
        if len(names) < 2:
            continue
        for name in names:
            entry = results[name]
            entry["state"] = "conflict"
            entry["reason"] = "{0} claimed by roles: {1}".format(device, ", ".join(sorted(names)))

    required_ok = all(
        results[role["role"]]["state"] in ("resolved", "pinned")
        for role in spec["roles"]
        if role.get("required", True)
    )
    state_ok = all(entry["state"] in ("resolved", "pinned") for entry in results.values())
    ok = required_ok if not strict else state_ok
    used = {str(entry["port"]).lower() for entry in results.values() if entry.get("port")}
    unassigned = [port.device for port in scoped if port.device.lower() not in used]

    document: Dict[str, Any] = {
        "schema": schema.SCHEMAS["role_map"],
        "generated_at": schema.now_bj(),
        "spec_schema": schema.SCHEMAS["roles_spec"],
        "backend": snapshot.backend,
        "dry_run": True,
        "ok": ok,
        "roles": results,
        "unassigned_ports": unassigned,
        "warnings": warnings + list(snapshot.warnings),
    }
    return document


def _resolved_reason(port: Port, expect: Dict[str, str]) -> str:
    if not expect:
        return "only serial port on the selected board"
    bits = []
    if port.interface:
        bits.append("interface {0}".format(port.interface))
    if port.product:
        bits.append(port.product)
    return "matched " + _expect_text(expect) + (" via {0}".format(", ".join(bits)) if bits else "")


def _expect_text(expect: Dict[str, str]) -> str:
    if not expect:
        return "any serial port"
    return ", ".join("{0}={1}".format(key, value) for key, value in sorted(expect.items()))


def role_ports(document: Dict[str, Any]) -> Dict[str, Optional[str]]:
    """Convenience for callers: {role: device or None}."""
    return {
        name: (entry.get("port") if entry.get("state") in ("resolved", "pinned") else None)
        for name, entry in (document.get("roles") or {}).items()
    }


__all__ = ["SpecError", "load_spec", "resolve", "role_ports", "validate_spec"]
