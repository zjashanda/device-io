"""Versioned contracts for device-io.

Everything another skill depends on is frozen here: schema identifiers, the
Beijing wall-clock time contract, and the JSON Schemas that ``device-io
schema`` prints. Two rules are hard:

1. Schema identifiers never change meaning inside a major version. A breaking
   change means a new ``...v2`` identifier, never a silent edit of ``v1``.
2. Every timestamp written by this tool is Beijing wall-clock time in the
   workspace format ``YYYY-MM-DD HH:MM:SS``, e.g. ``2026-11-10 21:05:00``.
   ``T``, ``Z``, ``+08:00`` and RFC3339 are all contract violations. An
   unmodified external timestamp is only allowed in a field whose name starts
   with ``raw_``.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict

SCHEMA_VERSION = "1"

SCHEMAS: Dict[str, str] = {
    "snapshot": "ark.device-io.v1",
    "boards": "ark.device-boards.v1",
    "roles_spec": "ark.device-roles-spec.v1",
    "role_map": "ark.device-role-map.v1",
    "clear_plan": "ark.device-clear-plan.v1",
    "driver_check": "ark.device-driver-check.v1",
    "pdu_status": "ark.device-pdu-status.v1",
    "pdu_plan": "ark.device-pdu-plan.v1",
}

BJ = timezone(timedelta(hours=8), "Asia/Shanghai")

TIME_CONTRACT: Dict[str, str] = {
    "zone": "Asia/Shanghai",
    "offset_in_string": "none",
    "format": "YYYY-MM-DD HH:MM:SS",
    "example": "2026-11-10 21:05:00",
    "forbidden": "T separator, Z suffix, +08:00 offset, RFC3339",
    "raw_exception": "fields named raw_* may carry an unmodified external timestamp",
}

TIME_PATTERN = r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$"
RAW_PREFIX = "raw_"

_TIME_RE = re.compile(TIME_PATTERN)
_TIME_FMT = "%Y-%m-%d %H:%M:%S"


def is_raw_field(name: str) -> bool:
    """True for fields exempt from the timestamp contract."""
    return str(name).lower().startswith(RAW_PREFIX)


def raw_fields(record: Dict[str, Any]) -> Dict[str, Any]:
    """Copy ``raw_*`` entries verbatim; they keep their original time format."""
    return {key: value for key, value in record.items() if is_raw_field(key)}

# Port kinds. ``serial`` is a COM/tty device; audio kinds carry an endpoint
# identity instead of a port name.
PORT_KINDS = ("serial", "audio-capture", "audio-render", "hid", "unknown")

# Driver families device-io can name without third-party code.
DRIVER_FAMILIES = (
    "ch34x",
    "ch343",
    "cp210x",
    "ftdi",
    "cdc-acm",
    "listenai-vcom",
    "usb-serial-generic",
    "unknown",
)

# Role resolution states. ``resolved`` means exactly one port matched the
# expectation; the other four always carry a reason.
ROLE_STATES = ("resolved", "pinned", "ambiguous", "missing", "conflict")

BACKENDS = ("auto", "linux", "windows", "fixture")


class SchemaError(ValueError):
    """Raised when a document violates a device-io contract."""


def now_bj() -> str:
    """Current Beijing wall-clock time, second precision, no offset."""
    return datetime.now(BJ).strftime(_TIME_FMT)


def is_bj_timestamp(value: Any) -> bool:
    return isinstance(value, str) and bool(_TIME_RE.match(value))


def require_bj_timestamp(value: Any, where: str) -> str:
    if not is_bj_timestamp(value):
        raise SchemaError(
            "{0}: {1!r} is not a Beijing no-offset timestamp ({2})".format(
                where, value, TIME_CONTRACT["format"]
            )
        )
    return value


def _port_schema() -> Dict[str, Any]:
    return {
        "type": "object",
        "required": ["id", "kind", "device", "os"],
        "properties": {
            "id": {
                "type": "string",
                "description": "Stable identifier inside one snapshot: <board_key>:<device or endpoint>",
            },
            "kind": {"type": "string", "enum": list(PORT_KINDS)},
            "device": {
                "type": "string",
                "description": "COM13 / /dev/ttyUSB0 / endpoint identity",
            },
            "os": {"type": "string", "enum": ["windows", "linux"]},
            "transport": {"type": "string", "description": "usb, pci, virtual, ..."},
            "label": {"type": "string", "description": "Human label, e.g. 'CH344 Quad_Serial B'"},
            "interface": {
                "type": ["string", "null"],
                "description": "Interface token, e.g. 'SERIAL-B' or 'Ch C'",
            },
            "interface_number": {"type": ["integer", "null"]},
            "vid": {"type": ["string", "null"], "description": "lowercase hex, no 0x"},
            "pid": {"type": ["string", "null"]},
            "serial": {"type": ["string", "null"]},
            "manufacturer": {"type": ["string", "null"]},
            "product": {"type": ["string", "null"]},
            "board_key": {"type": ["string", "null"]},
            "usb_path": {"type": ["string", "null"]},
            "location": {"type": ["string", "null"]},
            "present": {"type": "boolean"},
            "driver": {"$ref": "#/definitions/driver"},
            "extra": {"type": "object"},
        },
    }


def _driver_schema() -> Dict[str, Any]:
    return {
        "type": "object",
        "default": {},
        "properties": {
            "family": {"type": "string", "enum": list(DRIVER_FAMILIES)},
            "id": {"type": "string"},
            "display": {"type": "string"},
            "status": {
                "type": "string",
                "enum": ["ok", "missing", "mismatch", "unknown"],
            },
            "evidence": {"type": "string"},
            "requires": {"type": "array", "items": {"type": "string"}},
        },
    }


def _audio_schema() -> Dict[str, Any]:
    return {
        "type": "object",
        "required": ["id", "kind", "name", "board_key"],
        "properties": {
            "id": {"type": "string"},
            "kind": {"type": "string", "enum": ["audio-capture", "audio-render"]},
            "name": {"type": "string"},
            "channels": {"type": ["integer", "null"]},
            "vid": {"type": ["string", "null"]},
            "pid": {"type": ["string", "null"]},
            "board_key": {"type": ["string", "null"]},
            "endpoint_id": {"type": ["string", "null"]},
            "present": {"type": "boolean"},
            "extra": {"type": "object"},
        },
    }


def _board_schema() -> Dict[str, Any]:
    return {
        "type": "object",
        "required": ["key", "title", "port_ids"],
        "properties": {
            "key": {"type": "string"},
            "title": {"type": "string", "description": "e.g. 'ListenAI 板卡 #1'"},
            "vid": {"type": ["string", "null"]},
            "pid": {"type": ["string", "null"]},
            "serial": {"type": ["string", "null"]},
            "product": {"type": ["string", "null"]},
            "transport": {"type": ["string", "null"]},
            "port_ids": {"type": "array", "items": {"type": "string"}},
            "audio_ids": {"type": "array", "items": {"type": "string"}},
            "adapter": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "display": {"type": "string"},
                    "confidence": {
                        "type": "string",
                        "enum": ["certain", "likely", "guess"],
                    },
                },
            },
            "warnings": {"type": "array", "items": {"type": "string"}},
        },
    }


def snapshot_schema() -> Dict[str, Any]:
    """JSON Schema for ``ark.device-io.v1``."""
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": SCHEMAS["snapshot"],
        "title": "ark device-io snapshot",
        "type": "object",
        "required": ["schema", "generated_at", "backend", "os", "ports", "audio", "warnings"],
        "definitions": {
            "driver": _driver_schema(),
            "port": _port_schema(),
            "audio": _audio_schema(),
        },
        "properties": {
            "schema": {"const": SCHEMAS["snapshot"]},
            "generated_at": {
                "type": "string",
                "pattern": TIME_PATTERN,
                "description": "Beijing wall-clock YYYY-MM-DD HH:MM:SS, no offset",
            },
            "backend": {"type": "string", "enum": list(BACKENDS)},
            "os": {"type": "string", "enum": ["windows", "linux", "unknown"]},
            "host": {"type": "string"},
            "dry_run": {"type": "boolean"},
            "ports": {"type": "array", "items": {"$ref": "#/definitions/port"}},
            "audio": {"type": "array", "items": {"$ref": "#/definitions/audio"}},
            "boards": {"type": "array", "items": _board_schema()},
            "warnings": {"type": "array", "items": {"type": "string"}},
            "sources": {"type": "array", "items": {"type": "string"}},
        },
    }


def role_map_schema() -> Dict[str, Any]:
    """JSON Schema for ``ark.device-role-map.v1``."""
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": SCHEMAS["role_map"],
        "title": "ark device role map",
        "type": "object",
        "required": ["schema", "generated_at", "spec_schema", "roles", "ok"],
        "properties": {
            "schema": {"const": SCHEMAS["role_map"]},
            "generated_at": {
                "type": "string",
                "pattern": TIME_PATTERN,
                "description": "Beijing wall-clock YYYY-MM-DD HH:MM:SS, no offset",
            },
            "spec_schema": {"const": SCHEMAS["roles_spec"]},
            "backend": {"type": "string"},
            "dry_run": {"type": "boolean"},
            "ok": {
                "type": "boolean",
                "description": "true only when every role is resolved or pinned",
            },
            "roles": {
                "type": "object",
                "additionalProperties": {
                    "type": "object",
                    "required": ["role", "state", "port"],
                    "properties": {
                        "role": {"type": "string"},
                        "state": {"type": "string", "enum": list(ROLE_STATES)},
                        "port": {"type": ["string", "null"]},
                        "port_id": {"type": ["string", "null"]},
                        "board_key": {"type": ["string", "null"]},
                        "driver_family": {"type": ["string", "null"]},
                        "reason": {"type": "string"},
                        "candidates": {"type": "array", "items": {"type": "string"}},
                        "expect": {"type": "object"},
                    },
                },
            },
            "unassigned_ports": {"type": "array", "items": {"type": "string"}},
            "warnings": {"type": "array", "items": {"type": "string"}},
        },
    }


def roles_spec_schema() -> Dict[str, Any]:
    """JSON Schema for ``ark.device-roles-spec.v1`` (the caller's input)."""
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": SCHEMAS["roles_spec"],
        "title": "ark device roles spec",
        "type": "object",
        "required": ["schema", "roles"],
        "properties": {
            "schema": {"const": SCHEMAS["roles_spec"]},
            "board": {
                "type": "object",
                "description": "Restrict resolution to one physical board",
                "properties": {
                    "vid": {"type": "string"},
                    "pid": {"type": "string"},
                    "serial": {"type": "string"},
                    "product": {"type": "string"},
                    "board_key": {"type": "string"},
                },
            },
            "roles": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["role"],
                    "properties": {
                        "role": {"type": "string"},
                        "pin": {
                            "type": ["string", "null"],
                            "description": "Pinned device, e.g. COM3. Skips auto-detection.",
                        },
                        "interface": {"type": ["string", "null"]},
                        "driver": {"type": ["string", "null"]},
                        "product": {"type": ["string", "null"]},
                        "vid": {"type": ["string", "null"]},
                        "pid": {"type": ["string", "null"]},
                        "baud": {"type": ["integer", "null"]},
                        "required": {"type": "boolean", "default": True},
                        "allow": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Extra device names accepted when unambiguous",
                        },
                    },
                },
            },
            "strict": {"type": "boolean", "default": True},
        },
    }


def clear_plan_schema() -> Dict[str, Any]:
    """JSON Schema for ``ark.device-clear-plan.v1``."""
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": SCHEMAS["clear_plan"],
        "title": "ark device clear plan",
        "type": "object",
        "required": ["schema", "generated_at", "os", "supported", "apply", "items"],
        "properties": {
            "schema": {"const": SCHEMAS["clear_plan"]},
            "generated_at": {
                "type": "string",
                "pattern": TIME_PATTERN,
                "description": "Beijing wall-clock YYYY-MM-DD HH:MM:SS, no offset",
            },
            "os": {"type": "string"},
            "supported": {"type": "boolean"},
            "apply": {"type": "boolean", "description": "false = preview only"},
            "reason": {"type": "string"},
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["instance_id", "class", "reason"],
                    "properties": {
                        "instance_id": {"type": "string"},
                        "class": {"type": "string"},
                        "friendly_name": {"type": ["string", "null"]},
                        "parent": {"type": ["string", "null"]},
                        "reason": {"type": "string"},
                        "removed": {"type": "boolean"},
                        "error": {"type": ["string", "null"]},
                    },
                },
            },
            "summary": {"type": "object"},
        },
    }


def driver_check_schema() -> Dict[str, Any]:
    """JSON Schema for ``ark.device-driver-check.v1``."""
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": SCHEMAS["driver_check"],
        "title": "ark device driver check",
        "type": "object",
        "required": ["schema", "generated_at", "os", "ok", "drivers"],
        "properties": {
            "schema": {"const": SCHEMAS["driver_check"]},
            "generated_at": {
                "type": "string",
                "pattern": TIME_PATTERN,
                "description": "Beijing wall-clock YYYY-MM-DD HH:MM:SS, no offset",
            },
            "os": {"type": "string"},
            "ok": {"type": "boolean"},
            "dry_run": {"type": "boolean"},
            "drivers": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["family", "required", "status"],
                    "properties": {
                        "family": {"type": "string"},
                        "display": {"type": "string"},
                        "required": {"type": "boolean"},
                        "status": {
                            "type": "string",
                            "enum": ["ok", "missing", "mismatch", "unknown", "not-applicable"],
                        },
                        "evidence": {"type": "string"},
                        "advice": {"type": "string"},
                        "ports": {"type": "array", "items": {"type": "string"}},
                    },
                },
            },
            "warnings": {"type": "array", "items": {"type": "string"}},
        },
    }


_TS = {
    "type": "string",
    "pattern": TIME_PATTERN,
    "description": "Beijing wall-clock YYYY-MM-DD HH:MM:SS, no offset",
}


def pdu_status_schema() -> Dict[str, Any]:
    """JSON Schema for ``ark.device-pdu-status.v1`` (read-only PDU snapshot)."""
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": SCHEMAS["pdu_status"],
        "title": "ark device pdu status",
        "type": "object",
        "required": ["schema", "generated_at", "backend", "target", "outlets", "encoding"],
        "properties": {
            "schema": {"const": SCHEMAS["pdu_status"]},
            "generated_at": _TS,
            "backend": {"type": "string", "enum": ["snmp", "modbus-rtu"]},
            "transport": {"type": "string"},
            "target": {
                "type": "object",
                "required": ["host", "port"],
                "properties": {
                    "host": {"type": "string"},
                    "port": {"type": "integer"},
                    "outlet": {"type": ["integer", "null"], "minimum": 1, "maximum": 8},
                },
            },
            "apply": {"const": False},
            "supported": {"type": "boolean"},
            "http_probe": {"type": "object"},
            "device": {"type": "object"},
            "outlets": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["outlet", "state"],
                    "properties": {
                        "outlet": {"type": "integer", "minimum": 1, "maximum": 8},
                        "name": {"type": ["string", "null"]},
                        "state": {"type": "string", "enum": ["on", "off", "unknown"]},
                        "read_code": {"type": "integer", "enum": [1, 2]},
                        "current_a": {"type": ["number", "null"]},
                        "error": {"type": "string"},
                    },
                },
            },
            "totals": {"type": "object"},
            "summary": {"type": "object"},
            "encoding": {
                "type": "object",
                "required": ["read", "write", "inverted"],
                "properties": {
                    "read": {"type": "object", "description": "1=off 2=on"},
                    "write": {"type": "object", "description": "1=on 2=off"},
                    "inverted": {"const": True},
                },
            },
            "credential": {"type": "object"},
            "verified": {"type": "boolean"},
        },
    }


def pdu_plan_schema() -> Dict[str, Any]:
    """JSON Schema for ``ark.device-pdu-plan.v1`` (plan and verified result)."""
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": SCHEMAS["pdu_plan"],
        "title": "ark device pdu plan",
        "type": "object",
        "required": ["schema", "generated_at", "action", "apply", "verified", "target", "encoding"],
        "properties": {
            "schema": {"const": SCHEMAS["pdu_plan"]},
            "generated_at": _TS,
            "action": {"type": "string", "enum": ["power-on", "power-off", "power-cycle"]},
            "apply": {"type": "boolean", "description": "false = dry-run, nothing was written"},
            "executed": {"type": "boolean"},
            "verified": {"type": "boolean", "description": "true only after a read-back confirmed the state"},
            "verified_by": {"type": "string"},
            "backend": {"type": "string"},
            "transport": {"type": "string"},
            "target": {
                "type": "object",
                "required": ["host", "port", "outlet"],
                "properties": {
                    "host": {"type": "string"},
                    "port": {"type": "integer"},
                    "outlet": {"type": "integer", "minimum": 1, "maximum": 8},
                },
            },
            "current_state": {"type": "string"},
            "before": {"type": "object"},
            "after": {"type": "object"},
            "steps": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["step", "op"],
                    "properties": {
                        "step": {"type": "integer"},
                        "op": {"type": "string", "enum": ["read", "set", "verify", "wait"]},
                        "oid": {"type": "string"},
                        "value": {"type": "integer"},
                        "meaning": {"type": "string"},
                        "encoding": {"type": "string"},
                        "seconds": {"type": "number"},
                        "why": {"type": "string"},
                    },
                },
            },
            "phases": {"type": "array"},
            "hold_s": {"type": "number"},
            "failure": {"type": "object"},
            "preconditions": {"type": "array", "items": {"type": "string"}},
            "refusals_guaranteed": {"type": "array", "items": {"type": "string"}},
            "encoding": {"type": "object"},
            "credential": {"type": "object"},
            "note": {"type": "string"},
        },
    }


def all_schemas() -> Dict[str, Dict[str, Any]]:
    return {
        "snapshot": snapshot_schema(),
        "boards": snapshot_schema(),
        "roles-spec": roles_spec_schema(),
        "role-map": role_map_schema(),
        "clear-plan": clear_plan_schema(),
        "driver-check": driver_check_schema(),
        "pdu-status": pdu_status_schema(),
        "pdu-plan": pdu_plan_schema(),
    }
