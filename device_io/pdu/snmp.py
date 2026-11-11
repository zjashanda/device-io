"""Minimal SNMPv2c client (GET / SET) with no third-party dependency.

The source tooling linked against ``pysnmp``; device-io has zero runtime
dependencies and must stay installable on a bench node with no wheel cache, so
the four PDUs this adapter needs are encoded here directly:

* ``GetRequest`` (tag ``0xA0``) and ``SetRequest`` (``0xA3``),
* response parsing (``0xA2``) including ``error-status``,
* BER INTEGER / OCTET STRING / OID / NULL codecs.

Scope is deliberately narrow: IPv4/IPv6 UDP, community (v2c) only, one request
in flight, no GETNEXT walk, no traps. That is the whole surface the PDU control
contract uses.

:class:`MemoryTransport` is an in-process PDU agent that answers from a state
dict. It exists so the adapter can be exercised end-to-end - including the
read/write encoding inversion - without touching a real rack.
"""

from __future__ import annotations

import socket
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from . import contract

# -- BER ------------------------------------------------------------------
TAG_INTEGER = 0x02
TAG_OCTET_STRING = 0x04
TAG_NULL = 0x05
TAG_OID = 0x06
TAG_SEQUENCE = 0x30
TAG_IPADDRESS = 0x40
TAG_COUNTER32 = 0x41
TAG_GAUGE32 = 0x42
TAG_TIMETICKS = 0x43

TAG_GET_REQUEST = 0xA0
TAG_GET_NEXT = 0xA1
TAG_GET_RESPONSE = 0xA2
TAG_SET_REQUEST = 0xA3

SNMP_V2C = 1
ERROR_STATUS = {
    0: "noError",
    1: "tooBig",
    2: "noSuchName",
    3: "badValue",
    4: "readOnly",
    5: "genErr",
    6: "noAccess",
    7: "wrongType",
    8: "wrongLength",
    9: "wrongEncoding",
    10: "wrongValue",
    11: "noCreation",
    12: "inconsistentValue",
    13: "resourceUnavailable",
    14: "commitFailed",
    15: "undoFailed",
    16: "authorizationError",
    17: "notWritable",
    18: "inconsistentName",
}

MAX_UDP = 65535


class SnmpError(contract.PduError):
    """Any SNMP transport or protocol failure."""


class SnmpTimeout(SnmpError):
    """No reply inside the timeout budget."""


class SnmpProtocolError(SnmpError):
    """The reply could not be decoded, or was not a GetResponse."""


class SnmpErrorStatus(SnmpError):
    """The agent answered with a non-zero ``error-status``."""

    def __init__(self, status: int, index: int = 0) -> None:
        self.status = int(status)
        self.index = int(index)
        name = ERROR_STATUS.get(self.status, "unknown({0})".format(self.status))
        super(SnmpErrorStatus, self).__init__(
            "SNMP agent refused the request: {0} (error-index {1})".format(name, self.index)
        )


def encode_length(length: int) -> bytes:
    if length < 0:
        raise SnmpProtocolError("negative BER length")
    if length < 0x80:
        return bytes([length])
    body = length.to_bytes((length.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(body)]) + body


def _tlv(tag: int, body: bytes) -> bytes:
    return bytes([tag]) + encode_length(len(body)) + body


def encode_integer(value: int, tag: int = TAG_INTEGER) -> bytes:
    value = int(value)
    if value == 0:
        body = b"\x00"
    else:
        body = value.to_bytes((value.bit_length() + 8) // 8, "big", signed=True)
    return _tlv(tag, body)


def encode_octet_string(value: Any) -> bytes:
    if isinstance(value, bytes):
        body = value
    else:
        body = str(value).encode("utf-8")
    return _tlv(TAG_OCTET_STRING, body)


def encode_null() -> bytes:
    return b"\x05\x00"


def encode_oid(oid: str) -> bytes:
    parts = _oid_parts(oid)
    if len(parts) < 2:
        raise SnmpProtocolError("OID {0!r} has fewer than two arcs".format(oid))
    first = 40 * parts[0] + parts[1]
    if not 0 <= first <= 255:
        raise SnmpProtocolError("OID {0!r} cannot be encoded (first arcs)".format(oid))
    body = bytearray([first])
    for arc in parts[2:]:
        if arc < 0:
            raise SnmpProtocolError("OID {0!r} has a negative arc".format(oid))
        chunk = [arc & 0x7F]
        arc >>= 7
        while arc:
            chunk.append(0x80 | (arc & 0x7F))
            arc >>= 7
        body.extend(reversed(chunk))
    return _tlv(TAG_OID, bytes(body))


def _oid_parts(oid: Any) -> List[int]:
    if isinstance(oid, (list, tuple)):
        return [int(part) for part in oid]
    text = str(oid).strip()
    if text.startswith("."):
        text = text[1:]
    if not text:
        raise SnmpProtocolError("empty OID")
    try:
        return [int(part) for part in text.split(".")]
    except ValueError:
        raise SnmpProtocolError("OID {0!r} is not dotted numeric".format(oid))


def decode_tlv(data: bytes, offset: int = 0) -> Tuple[int, bytes, int]:
    """Return ``(tag, body, next_offset)``."""
    if offset + 2 > len(data):
        raise SnmpProtocolError("truncated BER element at offset {0}".format(offset))
    tag = data[offset]
    first = data[offset + 1]
    cursor = offset + 2
    if first & 0x80:
        count = first & 0x7F
        if count == 0:
            raise SnmpProtocolError("indefinite BER length is not allowed in SNMP")
        if cursor + count > len(data):
            raise SnmpProtocolError("truncated BER length at offset {0}".format(offset))
        length = int.from_bytes(data[cursor : cursor + count], "big")
        cursor += count
    else:
        length = first
    end = cursor + length
    if end > len(data):
        raise SnmpProtocolError("truncated BER body at offset {0}".format(offset))
    return tag, data[cursor:end], end


def decode_integer(body: bytes) -> int:
    if not body:
        raise SnmpProtocolError("empty BER INTEGER")
    return int.from_bytes(body, "big", signed=True)


def decode_oid(body: bytes) -> str:
    if not body:
        raise SnmpProtocolError("empty BER OID")
    first = body[0]
    arcs = [first // 40, first % 40]
    value = 0
    for index, byte in enumerate(body[1:]):
        value = (value << 7) | (byte & 0x7F)
        if not byte & 0x80:
            arcs.append(value)
            value = 0
        elif index == len(body) - 2:
            raise SnmpProtocolError("truncated BER OID sub-identifier")
    return ".".join(str(arc) for arc in arcs)


def decode_value(tag: int, body: bytes) -> Any:
    if tag == TAG_INTEGER:
        return decode_integer(body)
    if tag == TAG_OCTET_STRING:
        return body.decode("utf-8", "replace")
    if tag == TAG_NULL:
        return None
    if tag == TAG_OID:
        return decode_oid(body)
    if tag in (TAG_COUNTER32, TAG_GAUGE32, TAG_TIMETICKS):
        return int.from_bytes(body, "big")
    if tag == TAG_IPADDRESS:
        return ".".join(str(byte) for byte in body)
    return body


def value_tlv(value: Any) -> bytes:
    """Encode a varbind value the way ``SetRequest`` needs it."""
    if isinstance(value, bool):
        return encode_integer(int(value))
    if isinstance(value, int):
        return encode_integer(value)
    if isinstance(value, (bytes, bytearray)):
        return encode_octet_string(bytes(value))
    return encode_octet_string(str(value))


def _varbind(oid: str, value: Optional[bytes] = None) -> bytes:
    body = encode_oid(oid) + (value if value is not None else encode_null())
    return _tlv(TAG_SEQUENCE, body)


def _request(tag: int, community: str, request_id: int, varbinds: Sequence[bytes]) -> bytes:
    pdu = _tlv(
        tag,
        encode_integer(request_id)
        + encode_integer(0)
        + encode_integer(0)
        + _tlv(TAG_SEQUENCE, b"".join(varbinds)),
    )
    return _tlv(
        TAG_SEQUENCE, encode_integer(SNMP_V2C) + encode_octet_string(community) + pdu
    )


def build_get(community: str, request_id: int, oids: Iterable[str]) -> bytes:
    return _request(TAG_GET_REQUEST, community, request_id, [_varbind(oid) for oid in oids])


def build_set(community: str, request_id: int, oid: str, value: Any) -> bytes:
    return _request(TAG_SET_REQUEST, community, request_id, [_varbind(oid, value_tlv(value))])


class Varbind(object):
    __slots__ = ("oid", "tag", "value")

    def __init__(self, oid: str, tag: int = TAG_NULL, value: Any = None) -> None:
        self.oid = oid
        self.tag = tag
        self.value = value

    def to_dict(self) -> Dict[str, Any]:
        return {"oid": self.oid, "type": self.tag, "value": self.value}

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "Varbind({0!r}, {1!r})".format(self.oid, self.value)


def parse_message(data: bytes) -> Dict[str, Any]:
    """Decode an SNMP message into a plain dict.

    ``kind`` is ``get`` / ``set`` / ``get-next`` / ``response``; unknown PDU
    tags raise :class:`SnmpProtocolError`.
    """
    tag, body, end = decode_tlv(data, 0)
    if tag != TAG_SEQUENCE:
        raise SnmpProtocolError("SNMP message is not a BER SEQUENCE")
    if data[end:].strip(b"\x00"):
        raise SnmpProtocolError("trailing bytes after SNMP message")
    cursor = 0
    version_tag, version_body, cursor = decode_tlv(body, cursor)
    if version_tag != TAG_INTEGER:
        raise SnmpProtocolError("SNMP version is not an INTEGER")
    version = decode_integer(version_body)
    if version not in (0, SNMP_V2C):
        raise SnmpProtocolError("unsupported SNMP version {0}".format(version))
    community_tag, community_body, cursor = decode_tlv(body, cursor)
    if community_tag != TAG_OCTET_STRING:
        raise SnmpProtocolError("SNMP community is not an OCTET STRING")
    pdu_tag, pdu_body, cursor = decode_tlv(body, cursor)
    kinds = {
        TAG_GET_REQUEST: "get",
        TAG_SET_REQUEST: "set",
        TAG_GET_RESPONSE: "response",
        TAG_GET_NEXT: "get-next",
    }
    if pdu_tag not in kinds:
        raise SnmpProtocolError("unsupported PDU tag 0x{0:02x}".format(pdu_tag))
    offset = 0
    request_id_tag, request_id_body, offset = decode_tlv(pdu_body, offset)
    error_status_tag, error_status_body, offset = decode_tlv(pdu_body, offset)
    error_index_tag, error_index_body, offset = decode_tlv(pdu_body, offset)
    for label, tag_value in (
        ("request-id", request_id_tag),
        ("error-status", error_status_tag),
        ("error-index", error_index_tag),
    ):
        if tag_value != TAG_INTEGER:
            raise SnmpProtocolError("{0} is not an INTEGER".format(label))
    list_tag, list_body, offset = decode_tlv(pdu_body, offset)
    if list_tag != TAG_SEQUENCE:
        raise SnmpProtocolError("varbind list is not a SEQUENCE")
    varbinds: List[Varbind] = []
    position = 0
    while position < len(list_body):
        vb_tag, vb_body, position = decode_tlv(list_body, position)
        if vb_tag != TAG_SEQUENCE:
            raise SnmpProtocolError("varbind is not a SEQUENCE")
        oid_tag, oid_body, inner = decode_tlv(vb_body, 0)
        if oid_tag != TAG_OID:
            raise SnmpProtocolError("varbind OID is not an OID")
        value_tag, value_body, inner = decode_tlv(vb_body, inner)
        varbinds.append(Varbind(decode_oid(oid_body), value_tag, decode_value(value_tag, value_body)))
    return {
        "kind": kinds[pdu_tag],
        "version": version,
        "community": community_body.decode("utf-8", "replace"),
        "request_id": decode_integer(request_id_body),
        "error_status": decode_integer(error_status_body),
        "error_index": decode_integer(error_index_body),
        "varbinds": varbinds,
    }


def build_response(request: Dict[str, Any], varbinds: Sequence[Varbind], error_status: int = 0) -> bytes:
    encoded = [
        _varbind(vb.oid, value_tlv(vb.value) if vb.tag != TAG_NULL else encode_null())
        for vb in varbinds
    ]
    pdu = _tlv(
        TAG_GET_RESPONSE,
        encode_integer(request["request_id"])
        + encode_integer(error_status)
        + encode_integer(0)
        + _tlv(TAG_SEQUENCE, b"".join(encoded)),
    )
    return _tlv(
        TAG_SEQUENCE,
        encode_integer(request["version"])
        + encode_octet_string(request["community"])
        + pdu,
    )


# -- transports -----------------------------------------------------------
class Transport(object):
    """A single ``request(bytes) -> bytes`` round trip."""

    def request(self, payload: bytes) -> bytes:  # pragma: no cover - interface
        raise NotImplementedError

    def close(self) -> None:
        return None

    def describe(self) -> str:
        return self.__class__.__name__


class UdpTransport(Transport):
    """IPv4/IPv6 UDP, one datagram per request, bounded by timeout * attempts."""

    def __init__(
        self,
        host: str,
        port: int = 161,
        timeout: float = 2.0,
        attempts: int = 2,
        family: int = 0,
    ) -> None:
        self.host = host
        self.port = int(port)
        self.timeout = float(timeout)
        self.attempts = max(1, int(attempts))
        self.family = family or 0
        self._sock: Optional[socket.socket] = None

    def _socket(self) -> socket.socket:
        if self._sock is None:
            family = self.family
            if not family:
                try:
                    info = socket.getaddrinfo(self.host, self.port, socket.AF_UNSPEC, socket.SOCK_DGRAM)
                    family = info[0][0]
                except socket.gaierror as exc:
                    raise SnmpTimeout(
                        "cannot resolve {0}: {1}".format(self.host, exc)
                    )
            sock = socket.socket(family, socket.SOCK_DGRAM)
            sock.settimeout(self.timeout)
            self._sock = sock
        return self._sock

    def request(self, payload: bytes) -> bytes:
        sock = self._socket()
        last: Optional[BaseException] = None
        for _ in range(self.attempts):
            try:
                sock.sendto(payload, (self.host, self.port))
                data, _peer = sock.recvfrom(MAX_UDP)
                return data
            except socket.timeout:
                last = SnmpTimeout(
                    "no SNMP reply from {0}:{1} within {2}s".format(self.host, self.port, self.timeout)
                )
            except OSError as exc:
                last = SnmpTimeout(
                    "SNMP UDP failure talking to {0}:{1}: {2}".format(self.host, self.port, exc)
                )
        raise last or SnmpTimeout("no SNMP reply from {0}:{1}".format(self.host, self.port))

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None

    def describe(self) -> str:
        return "udp {0}:{1} timeout={2}s attempts={3}".format(
            self.host, self.port, self.timeout, self.attempts
        )

    def __enter__(self) -> "UdpTransport":
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()


class MemoryTransport(Transport):
    """In-process PDU agent: answers from a state dict, never opens a socket.

    It deliberately applies a *write* code through
    :meth:`contract.OutletState.from_write_code`, so a caller that confused the
    two encodings would observe the opposite socket state and fail the test.

    Knobs (all optional) make the failure paths reachable:

    ``deaf``       OIDs that never answer (raises :class:`SnmpTimeout`)
    ``ignore_writes``  accept the SET but keep the old state
    ``ignore_write_values``  same, but only for these write codes (1 = on)
    ``refuse``     outlet -> ``error-status`` returned for writes
    """

    def __init__(
        self,
        states: Optional[Dict[int, Any]] = None,
        names: Optional[Dict[int, str]] = None,
        measurements: Optional[Dict[str, Any]] = None,
        device_name: str = "DPDU (simulated)",
        deaf: Optional[Iterable[str]] = None,
        ignore_writes: bool = False,
        ignore_write_values: Optional[Iterable[int]] = None,
        refuse: Optional[Dict[int, int]] = None,
    ) -> None:
        self.states: Dict[int, contract.OutletState] = {}
        for outlet in range(contract.SOCKET_MIN, contract.SOCKET_MAX + 1):
            self.states[outlet] = contract.STATE_OFF
        for outlet, state in (states or {}).items():
            self.states[contract.validate_outlet(outlet)] = contract.OutletState(state)
        self.names = {contract.validate_outlet(k): str(v) for k, v in (names or {}).items()}
        self.measurements = dict(measurements or {})
        self.device_name = device_name
        self.deaf = set(deaf or ())
        self.ignore_writes = bool(ignore_writes)
        self.ignore_write_values = set(int(v) for v in (ignore_write_values or ()))
        self.refuse = {int(k): int(v) for k, v in (refuse or {}).items()}
        self.history: List[Dict[str, Any]] = []
        self.community_used: List[str] = []

    # -- agent -------------------------------------------------------------
    def _lookup(self, oid: str) -> Optional[Any]:
        for outlet in range(contract.SOCKET_MIN, contract.SOCKET_MAX + 1):
            if oid == contract.state_oid(outlet):
                return self.states[outlet].read_code()
            if oid == contract.current_oid(outlet):
                return self.measurements.get(oid, 0)
            if oid == contract.name_oid(outlet):
                return self.names.get(outlet, "socket {0}".format(outlet))
        if oid == contract.DEVICE_NAME_READ:
            return self.device_name
        return self.measurements.get(oid, 0)

    def request(self, payload: bytes) -> bytes:
        parsed = parse_message(payload)
        self.community_used.append(parsed["community"])
        if parsed["kind"] == "get":
            values: List[Varbind] = []
            for vb in parsed["varbinds"]:
                if vb.oid in self.deaf:
                    raise SnmpTimeout("no SNMP reply from simulated agent (deaf {0})".format(vb.oid))
                value = self._lookup(vb.oid)
                values.append(Varbind(vb.oid, TAG_INTEGER if isinstance(value, int) else TAG_OCTET_STRING, value))
            self.history.append({"kind": "get", "oids": [vb.oid for vb in parsed["varbinds"]]})
            return build_response(parsed, values)
        if parsed["kind"] == "set":
            vb = parsed["varbinds"][0]
            outlet = self._outlet_for_write(vb.oid)
            if outlet in self.refuse:
                self.history.append({"kind": "set", "oid": vb.oid, "value": vb.value, "refused": True})
                return build_response(parsed, list(parsed["varbinds"]), self.refuse[outlet])
            if self.ignore_writes or int(vb.value) in self.ignore_write_values:
                pass
            else:
                self.states[outlet] = contract.OutletState.from_write_code(vb.value)
            self.history.append(
                {"kind": "set", "oid": vb.oid, "value": vb.value, "outlet": outlet}
            )
            return build_response(parsed, list(parsed["varbinds"]))
        raise SnmpProtocolError("simulated agent cannot answer {0!r}".format(parsed["kind"]))

    def _outlet_for_write(self, oid: str) -> int:
        for outlet in range(contract.SOCKET_MIN, contract.SOCKET_MAX + 1):
            if oid == contract.socket_oid(outlet):
                return outlet
        raise SnmpProtocolError("simulated agent has no writable OID {0}".format(oid))

    def describe(self) -> str:
        return "memory (simulated PDU, no network)"

    def close(self) -> None:
        return None

    # -- inspection --------------------------------------------------------
    @property
    def writes(self) -> List[Dict[str, Any]]:
        return [item for item in self.history if item["kind"] == "set"]

    def state(self, outlet: int) -> str:
        return self.states[contract.validate_outlet(outlet)].name


# -- client ---------------------------------------------------------------
class SnmpClient(object):
    """GET/SET against one agent, with a monotonically increasing request id."""

    def __init__(self, community: str, transport: Transport, timeout: float = 2.0) -> None:
        if not community:
            raise SnmpError("an SNMP community must be supplied")
        self._community = community
        self.transport = transport
        self.timeout = timeout
        self._request_id = 0

    def _next_id(self) -> int:
        self._request_id += 1
        return self._request_id

    def get(self, oids: Sequence[str]) -> List[Varbind]:
        if not oids:
            return []
        request_id = self._next_id()
        parsed = self._exchange(build_get(self._community, request_id, oids), request_id)
        return list(parsed["varbinds"])

    def get_int(self, oid: str) -> int:
        values = self.get([oid])
        if not values:
            raise SnmpProtocolError("empty reply for {0}".format(oid))
        value = values[0].value
        if not isinstance(value, int):
            raise SnmpProtocolError("{0} returned {1!r}, not an integer".format(oid, value))
        return value

    def get_many(self, oids: Sequence[str]) -> Dict[str, Any]:
        return {vb.oid: vb.value for vb in self.get(oids)}

    def set_int(self, oid: str, value: int) -> List[Varbind]:
        request_id = self._next_id()
        parsed = self._exchange(build_set(self._community, request_id, oid, int(value)), request_id)
        return list(parsed["varbinds"])

    def _exchange(self, payload: bytes, request_id: int) -> Dict[str, Any]:
        try:
            data = self.transport.request(payload)
        except SnmpError:
            raise
        except socket.timeout:
            raise SnmpTimeout("SNMP request timed out")
        parsed = parse_message(data)
        if parsed["kind"] != "response":
            raise SnmpProtocolError("expected a GetResponse, got {0!r}".format(parsed["kind"]))
        if parsed["request_id"] != request_id:
            raise SnmpProtocolError("request-id mismatch in SNMP reply")
        if parsed["error_status"]:
            raise SnmpErrorStatus(parsed["error_status"], parsed["error_index"])
        return parsed

    def close(self) -> None:
        self.transport.close()

    def __repr__(self) -> str:
        # Deliberately excludes the community: reprs end up in tracebacks.
        return "SnmpClient(transport={0!r})".format(self.transport.describe())

    __str__ = __repr__

    @property
    def community_set(self) -> bool:
        return bool(self._community)

    def __enter__(self) -> "SnmpClient":
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()


__all__ = [
    "ERROR_STATUS",
    "MAX_UDP",
    "MemoryTransport",
    "SnmpClient",
    "SnmpError",
    "SnmpErrorStatus",
    "SnmpProtocolError",
    "SnmpTimeout",
    "TAG_GET_REQUEST",
    "TAG_GET_RESPONSE",
    "TAG_SET_REQUEST",
    "Transport",
    "UdpTransport",
    "Varbind",
    "build_get",
    "build_response",
    "build_set",
    "decode_integer",
    "decode_oid",
    "decode_tlv",
    "decode_value",
    "encode_integer",
    "encode_length",
    "encode_octet_string",
    "encode_oid",
    "parse_message",
    "value_tlv",
]
