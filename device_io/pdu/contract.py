"""PDU power-control contract: addressing, OIDs and the value encodings.

Everything here is lifted verbatim from the NOAH-58 inventory of
``pudCorntrol.rar`` (SHA-256 ``76fe3443…a673``), section ``control_surface``.
Nothing is guessed: values the inventory marks ``unknown_from_inventory`` stay
unknown and are reported as :data:`UNKNOWN`, never defaulted.

The single highest-severity trap in this hardware is that the **write OID and
the read OID use opposite encodings** for on/off:

===========  =====  =====
value        read   write
===========  =====  =====
off            1      2
on             2      1
===========  =====  =====

A naive copy of a read value into a write request therefore flips the socket.
:func:`read_code` and :func:`write_code` are the only two places allowed to
know the mapping; every caller goes through :class:`OutletState`.
"""

from __future__ import annotations

import ipaddress
import re
from typing import Any, Dict, Optional

SCHEMA_STATUS = "ark.device-pdu-status.v1"
SCHEMA_PLAN = "ark.device-pdu-plan.v1"

#: Vendor enterprise numbers. ``23280`` carries switch/state/measurements,
#: ``23273`` carries socket names and the legacy single/all-socket control.
ENTERPRISE_PDU = "1.3.6.1.4.1.23280"
ENTERPRISE_SOCKET_META = "1.3.6.1.4.1.23273"

#: Per-socket switch (write). Integer, 1 = on, 2 = off.
SOCKET_SWITCH_WRITE = ENTERPRISE_PDU + ".9.1.2.{n}"
#: Per-socket state (read). Integer, 1 = off, 2 = on.
SOCKET_STATE_READ = ENTERPRISE_PDU + ".8.1.2.{n}"
#: Per-socket current in amperes = raw / 100.
SOCKET_CURRENT_READ = ENTERPRISE_PDU + ".8.1.4.{n}"

TOTAL_VOLTAGE_READ = ENTERPRISE_PDU + ".6.1.2.1"       # raw / 10   -> V
TOTAL_CURRENT_READ = ENTERPRISE_PDU + ".6.1.3.1"       # raw / 100  -> A
TOTAL_POWER_READ = ENTERPRISE_PDU + ".6.1.4.1"         # raw / 1000 -> W
TOTAL_ENERGY_READ = ENTERPRISE_PDU + ".6.1.8.1"        # raw / 1000 -> kWh

DEVICE_NAME_READ = "1.3.6.1.2.1.1.1.0"                 # sysDescr
SOCKET_NAME_READ = ENTERPRISE_SOCKET_META + ".4.{idx}.1"  # idx = 7 + n

#: Legacy control block. ``255`` means "all sockets", which is exactly the
#: blast radius this adapter refuses by default.
ALL_SOCKETS_CODE = 255

#: Divisor per measurement OID (``raw / divisor``).
MEASUREMENT_DIVISORS: Dict[str, int] = {
    SOCKET_CURRENT_READ: 100,
    TOTAL_VOLTAGE_READ: 10,
    TOTAL_CURRENT_READ: 100,
    TOTAL_POWER_READ: 1000,
    TOTAL_ENERGY_READ: 1000,
}

SOCKET_MIN = 1
SOCKET_MAX = 8

READ_OFF = 1
READ_ON = 2
WRITE_ON = 1
WRITE_OFF = 2

UNKNOWN = "unknown_from_inventory"

#: Modbus RTU frames recorded by the vendor note. The serial line parameters
#: (baud / parity / stop bits) are NOT in the inventory, so the backend is
#: reported as blocked instead of guessing a line rate.
MODBUS_SLAVE_ADDRESS = 0x02
MODBUS_FRAME_READ_STATE = "02 03 00 30 00 01 84 36"
MODBUS_FRAME_WRITE_ALL = "02 06 00 30 00 {value:02X}"
MODBUS_REGISTER_TOTAL_VOLTAGE = 0x0003
MODBUS_REGISTER_TOTAL_CURRENT = 0x0005
MODBUS_MIN_INTERVAL_MS = 500
MODBUS_SERIAL_PARAMETERS = UNKNOWN


class PduError(RuntimeError):
    """Base class for every PDU failure. Messages never carry a credential."""


class PduContractError(PduError, ValueError):
    """The caller asked for something the contract forbids."""


class PduEncodingError(PduContractError):
    """A state value did not match either encoding of the OID family."""


class OutletState(object):
    """Normalised socket state with the two encodings kept apart.

    Comparing states is always done on :attr:`name` (``"on"`` / ``"off"``),
    never on the raw integer, so a read value can never be mistaken for a
    write value.
    """

    __slots__ = ("name",)

    def __init__(self, name: str) -> None:
        text = str(name).strip().lower()
        if text in ("on", "off"):
            self.name = text
        elif text in ("true", "yes", "enable", "enabled"):
            self.name = "on"
        elif text in ("false", "no", "disable", "disabled"):
            self.name = "off"
        else:
            raise PduContractError(
                "socket state must be 'on' or 'off', got {0!r}".format(name)
            )

    # -- encodings ---------------------------------------------------------
    @classmethod
    def from_read_code(cls, code: Any) -> "OutletState":
        """Decode the *read* OID value (1 = off, 2 = on)."""
        value = _as_int(code)
        if value == READ_ON:
            return cls("on")
        if value == READ_OFF:
            return cls("off")
        raise PduEncodingError(
            "read code {0!r} is neither 1 (off) nor 2 (on)".format(code)
        )

    @classmethod
    def from_write_code(cls, code: Any) -> "OutletState":
        """Decode the *write* OID value (1 = on, 2 = off). Inverse of read."""
        value = _as_int(code)
        if value == WRITE_ON:
            return cls("on")
        if value == WRITE_OFF:
            return cls("off")
        raise PduEncodingError(
            "write code {0!r} is neither 1 (on) nor 2 (off)".format(code)
        )

    def read_code(self) -> int:
        return READ_ON if self.name == "on" else READ_OFF

    def write_code(self) -> int:
        return WRITE_ON if self.name == "on" else WRITE_OFF

    @property
    def on(self) -> bool:
        return self.name == "on"

    def inverted(self) -> "OutletState":
        return OutletState("off" if self.name == "on" else "on")

    def __eq__(self, other: Any) -> bool:
        if isinstance(other, OutletState):
            return self.name == other.name
        if isinstance(other, str):
            try:
                return self.name == OutletState(other).name
            except PduContractError:
                return False
        return NotImplemented

    def __hash__(self) -> int:
        return hash(self.name)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "OutletState({0!r})".format(self.name)

    def __str__(self) -> str:
        return self.name


STATE_ON = OutletState("on")
STATE_OFF = OutletState("off")


def _as_int(value: Any) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        text = value.strip()
        if re.fullmatch(r"[+-]?\d+", text):
            return int(text)
    raise PduEncodingError("expected an integer state code, got {0!r}".format(value))


def read_code(state: Any) -> int:
    """State -> value for :data:`SOCKET_STATE_READ` (1 = off, 2 = on)."""
    return OutletState(state).read_code()


def write_code(state: Any) -> int:
    """State -> value for :data:`SOCKET_SWITCH_WRITE` (1 = on, 2 = off)."""
    return OutletState(state).write_code()


# -- addressing -----------------------------------------------------------
def validate_outlet(value: Any) -> int:
    """Return the 1-based socket index, or raise.

    The vendor CLI defaults this to ``0``, which is outside the range its own
    SNMP layer validates; here the value is mandatory and ``0`` / ``255`` (the
    all-socket code) are refused with an explicit reason.
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        raise PduContractError(
            "an outlet number is required (1..{0}); this adapter never picks one".format(SOCKET_MAX)
        )
    try:
        number = _as_int(value)
    except PduEncodingError:
        raise PduContractError("outlet must be an integer 1..{0}, got {1!r}".format(SOCKET_MAX, value))
    if number == ALL_SOCKETS_CODE or number == 0:
        raise PduContractError(
            "outlet {0} is the all-sockets form; writing it can cut a whole rack, so it is refused".format(number)
        )
    if number < SOCKET_MIN or number > SOCKET_MAX:
        raise PduContractError(
            "outlet must be 1..{0}, got {1}".format(SOCKET_MAX, number)
        )
    return number


_HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,252}$")


def validate_host(value: Any) -> str:
    """Validate an explicitly supplied PDU address.

    No default is applied: every IP in the vendor archive is a stale sample, so
    the endpoint must come from the caller.
    """
    if value is None or not str(value).strip():
        raise PduContractError(
            "a PDU address is required (--host/--pdu-ip); the archive ships only stale samples"
        )
    text = str(value).strip()
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1]
    try:
        ipaddress.ip_address(text)
        return text
    except ValueError:
        pass
    if not _HOST_RE.match(text) or text.startswith("-"):
        raise PduContractError("not a usable PDU address: {0!r}".format(value))
    return text


def socket_oid(n: int) -> str:
    return SOCKET_SWITCH_WRITE.format(n=validate_outlet(n))


def state_oid(n: int) -> str:
    return SOCKET_STATE_READ.format(n=validate_outlet(n))


def current_oid(n: int) -> str:
    return SOCKET_CURRENT_READ.format(n=validate_outlet(n))


def name_oid(n: int) -> str:
    return SOCKET_NAME_READ.format(idx=7 + validate_outlet(n))


def oid_table(outlet: Optional[int] = None) -> Dict[str, Any]:
    """The OID map as data, for ``pdu doctor`` and for documentation."""
    table: Dict[str, Any] = {
        "enterprise_pdu": ENTERPRISE_PDU,
        "enterprise_socket_meta": ENTERPRISE_SOCKET_META,
        "socket_switch_write": SOCKET_SWITCH_WRITE,
        "socket_state_read": SOCKET_STATE_READ,
        "socket_current_read": SOCKET_CURRENT_READ,
        "total_voltage_read": TOTAL_VOLTAGE_READ,
        "total_current_read": TOTAL_CURRENT_READ,
        "total_power_read": TOTAL_POWER_READ,
        "total_energy_read": TOTAL_ENERGY_READ,
        "device_name_read": DEVICE_NAME_READ,
        "socket_name_read": SOCKET_NAME_READ,
        "read_encoding": {"1": "off", "2": "on"},
        "write_encoding": {"1": "on", "2": "off"},
        "inverted": True,
        "all_sockets_code": ALL_SOCKETS_CODE,
        "socket_range": [SOCKET_MIN, SOCKET_MAX],
    }
    if outlet is not None:
        n = validate_outlet(outlet)
        table["resolved"] = {
            "outlet": n,
            "switch_write": socket_oid(n),
            "state_read": state_oid(n),
            "current_read": current_oid(n),
            "name_read": name_oid(n),
        }
    return table


def divisor_for(oid: str) -> Optional[int]:
    """Divisor for a measurement OID, resolving per-socket templates (``{n}``)."""
    if oid in MEASUREMENT_DIVISORS:
        return MEASUREMENT_DIVISORS[oid]
    for template, divisor in MEASUREMENT_DIVISORS.items():
        if "{n}" not in template:
            continue
        prefix, suffix = template.split("{n}", 1)
        if not oid.startswith(prefix):
            continue
        tail = oid[len(prefix) :]
        if suffix and not tail.endswith(suffix):
            continue
        middle = tail[: len(tail) - len(suffix)] if suffix else tail
        if middle.isdigit():
            return divisor
    return None


def scale(oid: str, raw: Any) -> Optional[float]:
    """Apply the inventory's divisor for a measurement OID."""
    divisor = divisor_for(oid)
    if divisor is None:
        return None
    try:
        return round(float(_as_int(raw)) / divisor, 3)
    except PduEncodingError:
        return None


__all__ = [
    "ALL_SOCKETS_CODE",
    "DEVICE_NAME_READ",
    "ENTERPRISE_PDU",
    "ENTERPRISE_SOCKET_META",
    "MEASUREMENT_DIVISORS",
    "MODBUS_FRAME_READ_STATE",
    "MODBUS_FRAME_WRITE_ALL",
    "MODBUS_MIN_INTERVAL_MS",
    "MODBUS_REGISTER_TOTAL_CURRENT",
    "MODBUS_REGISTER_TOTAL_VOLTAGE",
    "MODBUS_SERIAL_PARAMETERS",
    "MODBUS_SLAVE_ADDRESS",
    "OutletState",
    "PduContractError",
    "PduEncodingError",
    "PduError",
    "READ_OFF",
    "READ_ON",
    "SCHEMA_PLAN",
    "SCHEMA_STATUS",
    "SOCKET_MAX",
    "SOCKET_MIN",
    "SOCKET_CURRENT_READ",
    "SOCKET_NAME_READ",
    "SOCKET_STATE_READ",
    "SOCKET_SWITCH_WRITE",
    "STATE_OFF",
    "STATE_ON",
    "TOTAL_CURRENT_READ",
    "TOTAL_ENERGY_READ",
    "TOTAL_POWER_READ",
    "TOTAL_VOLTAGE_READ",
    "UNKNOWN",
    "WRITE_OFF",
    "WRITE_ON",
    "current_oid",
    "divisor_for",
    "name_oid",
    "oid_table",
    "read_code",
    "scale",
    "socket_oid",
    "state_oid",
    "validate_host",
    "validate_outlet",
    "write_code",
]
