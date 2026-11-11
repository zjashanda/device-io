"""Generic PDU power control (SNMP v2c), adapted from ``pudCorntrol.rar``.

    from device_io.pdu import PduAdapter, build_client, resolve_community

    adapter = PduAdapter(build_client("10.0.0.9", community=resolve_community()), "10.0.0.9", outlet=3)
    adapter.status()                       # read-only
    adapter.set_state("off", apply=True)   # writes, then verifies by reading back

The module exists so that powering a socket is a reviewed contract instead of a
copied vendor script: the outlet is mandatory, all-sockets writes are refused,
and no write is ever reported as successful without a read-back.

Nothing here runs at import time and nothing here is imported by
:mod:`device_io.cli` unless a ``pdu`` command is used, so the rest of device-io
keeps its read-only, dependency-free footprint.
"""

from __future__ import annotations

from .adapter import (
    ACTIONS,
    DEFAULT_HOLD_SECONDS,
    PduAdapter,
    PduVerificationError,
    backend_report,
    build_client,
    parse_simulated_states,
    simulated_transport,
)
from .contract import (
    ALL_SOCKETS_CODE,
    MEASUREMENT_DIVISORS,
    OutletState,
    PduContractError,
    PduEncodingError,
    PduError,
    SCHEMA_PLAN,
    SCHEMA_STATUS,
    SOCKET_MAX,
    SOCKET_MIN,
    STATE_OFF,
    STATE_ON,
    current_oid,
    name_oid,
    oid_table,
    read_code,
    socket_oid,
    state_oid,
    validate_host,
    validate_outlet,
    write_code,
)
from .credentials import (
    ENV_VAR,
    Community,
    CommunityError,
    describe_sources,
    resolve_community,
)

__all__ = [
    "ACTIONS",
    "ALL_SOCKETS_CODE",
    "Community",
    "CommunityError",
    "DEFAULT_HOLD_SECONDS",
    "ENV_VAR",
    "MEASUREMENT_DIVISORS",
    "OutletState",
    "PduAdapter",
    "PduContractError",
    "PduEncodingError",
    "PduError",
    "PduVerificationError",
    "SCHEMA_PLAN",
    "SCHEMA_STATUS",
    "SOCKET_MAX",
    "SOCKET_MIN",
    "STATE_OFF",
    "STATE_ON",
    "backend_report",
    "build_client",
    "current_oid",
    "describe_sources",
    "name_oid",
    "oid_table",
    "parse_simulated_states",
    "read_code",
    "resolve_community",
    "simulated_transport",
    "socket_oid",
    "state_oid",
    "validate_host",
    "validate_outlet",
    "write_code",
]
