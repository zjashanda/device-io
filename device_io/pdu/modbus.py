"""Modbus RTU frame builder for the same PDU family - reported as BLOCKED.

The inventory records the vendor's Modbus frames but not the serial line
parameters (baud rate, parity, stop bits, and which port the PDU exposes). Those
cannot be derived from the code, only measured on a live unit, so this module
builds and checks frames and then refuses to run: :func:`backend_status` returns
``available: false`` with the exact missing inputs.

Building the frames is still useful. The read frame in the inventory
(``02 03 00 30 00 01 84 36``) is a CRC-16/MODBUS test vector, so the CRC here is
verified against the vendor bytes in the test suite rather than trusted.
"""

from __future__ import annotations

from typing import Any, Dict, List

from . import contract


def crc16(data: bytes) -> int:
    """CRC-16/MODBUS (poly 0xA001, init 0xFFFF), returned little-endian-ready."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 0x0001:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return crc & 0xFFFF


def append_crc(frame: bytes) -> bytes:
    crc = crc16(frame)
    return frame + bytes([crc & 0xFF, (crc >> 8) & 0xFF])


def crc_ok(frame: bytes) -> bool:
    if len(frame) < 3:
        return False
    return crc16(frame[:-2]) == int.from_bytes(frame[-2:], "little")


def build_read_state_request(slave: int = contract.MODBUS_SLAVE_ADDRESS) -> bytes:
    """Read the state register: ``02 03 00 30 00 01 84 36`` for slave 2."""
    return append_crc(bytes([slave, 0x03, 0x00, 0x30, 0x00, 0x01]))


def build_write_all_request(code: int, slave: int = contract.MODBUS_SLAVE_ADDRESS) -> bytes:
    """Write the all-socket register.

    This is the Modbus spelling of the same rack-wide blast radius the SNMP
    path refuses: the register addresses a group, not one socket. Nothing in
    device-io sends it; the builder exists to document and test the refusal.
    """
    if code not in (0, 1, 2, 15, 255):
        raise contract.PduContractError(
            "Modbus write code {0!r} is not one of the documented group codes".format(code)
        )
    return append_crc(bytes([slave, 0x06, 0x00, 0x30, 0x00, code & 0xFF]))


def hex_frame(frame: bytes) -> str:
    return " ".join("{0:02X}".format(byte) for byte in frame)


def backend_status() -> Dict[str, Any]:
    """Machine-readable status for ``pdu doctor`` and the CLI refusal."""
    return {
        "backend": "modbus-rtu",
        "available": False,
        "state": "blocked",
        "reason": "serial line parameters are not recorded in the vendor archive",
        "unknown": [
            "baud_rate",
            "parity",
            "stop_bits",
            "data_bits",
            "serial_port",
        ],
        "known": {
            "slave_address": contract.MODBUS_SLAVE_ADDRESS,
            "min_interval_ms": contract.MODBUS_MIN_INTERVAL_MS,
            "read_frame": hex_frame(build_read_state_request()),
            "write_group_frame_template": contract.MODBUS_FRAME_WRITE_ALL,
            "registers": {
                "total_voltage": "0x0003 (raw / 10)",
                "total_current": "0x0005 (raw / 1000)",
            },
        },
        "required_before_enable": [
            "capture baud/parity/stop bits from a live PDU (vendor Modbus debug tool)",
            "confirm which physical port exposes Modbus RTU",
            "record the measured values in the inventory, then implement the transport",
        ],
        "note": "parameters are UNKNOWN, not defaulted; guessing a baud rate would silently corrupt frames",
    }


def refusal() -> Dict[str, Any]:
    status = backend_status()
    return {
        "supported": False,
        "backend": "modbus-rtu",
        "reason": status["reason"],
        "unknown": status["unknown"],
        "required_before_enable": status["required_before_enable"],
    }


__all__ = [
    "append_crc",
    "backend_status",
    "build_read_state_request",
    "build_write_all_request",
    "crc16",
    "crc_ok",
    "hex_frame",
    "refusal",
]
