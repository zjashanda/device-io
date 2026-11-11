"""SNMP codec, UDP transport and Modbus frame tests - no real hardware.

Three layers are covered:

1. BER encode/decode round trips, including the long-form length and the OID
   sub-identifier continuation bytes that the vendor's /8 and /9 subtrees need.
2. A real UDP round trip against :class:`device_io.pdu.snmp.MemoryTransport`
   served from a background thread on 127.0.0.1, so the framing is exercised
   over an actual socket rather than a function call.
3. The Modbus CRC, checked against the vendor's own frame (``… 84 36``).
"""

from __future__ import annotations

import socket
import threading
import unittest

from device_io.pdu import contract, modbus
from device_io.pdu import snmp


class BerTests(unittest.TestCase):
    def test_length_short_and_long_form(self):
        self.assertEqual(snmp.encode_length(0), b"\x00")
        self.assertEqual(snmp.encode_length(127), b"\x7f")
        self.assertEqual(snmp.encode_length(128), b"\x81\x80")
        self.assertEqual(snmp.encode_length(300), b"\x82\x01\x2c")

    def test_integer_boundaries(self):
        for value in (0, 1, -1, 127, 128, 255, 256, -128, -129, 65535):
            tag, body, end = snmp.decode_tlv(snmp.encode_integer(value))
            self.assertEqual(tag, snmp.TAG_INTEGER)
            self.assertEqual(snmp.decode_integer(body), value)
            self.assertEqual(end, len(snmp.encode_integer(value)))

    def test_oid_round_trip(self):
        for oid in (
            "1.3.6.1.4.1.23280.9.1.2.1",
            "1.3.6.1.4.1.23273.4.15.1",
            "1.3.6.1.2.1.1.1.0",
            contract.state_oid(8),
        ):
            tag, body, _end = snmp.decode_tlv(snmp.encode_oid(oid))
            self.assertEqual(tag, snmp.TAG_OID)
            self.assertEqual(snmp.decode_oid(body), oid)

    def test_oid_with_128_arc_uses_continuation_byte(self):
        encoded = snmp.encode_oid("1.3.6.1.4.1.23280.128.1")
        tag, body, _ = snmp.decode_tlv(encoded)
        self.assertIn(b"\x81\x00", body)
        self.assertEqual(snmp.decode_oid(body), "1.3.6.1.4.1.23280.128.1")

    def test_get_request_parses_back(self):
        payload = snmp.build_get("public", 7, [contract.state_oid(3)])
        parsed = snmp.parse_message(payload)
        self.assertEqual(parsed["kind"], "get")
        self.assertEqual(parsed["version"], snmp.SNMP_V2C)
        self.assertEqual(parsed["request_id"], 7)
        self.assertEqual([vb.oid for vb in parsed["varbinds"]], [contract.state_oid(3)])
        self.assertIsNone(parsed["varbinds"][0].value)

    def test_set_request_carries_the_write_code(self):
        payload = snmp.build_set("public", 9, contract.socket_oid(2), contract.write_code("off"))
        parsed = snmp.parse_message(payload)
        self.assertEqual(parsed["kind"], "set")
        self.assertEqual(parsed["varbinds"][0].value, 2)
        self.assertEqual(
            contract.OutletState.from_write_code(parsed["varbinds"][0].value), contract.STATE_OFF
        )

    def test_truncated_input_is_a_protocol_error(self):
        payload = snmp.build_get("public", 1, [contract.state_oid(1)])
        for cut in (0, 1, len(payload) // 2, len(payload) - 1):
            with self.assertRaises(snmp.SnmpProtocolError):
                snmp.parse_message(payload[:cut])

    def test_unexpected_pdu_kind_is_rejected(self):
        with self.assertRaises(snmp.SnmpProtocolError):
            snmp.parse_message(b"\x30\x03\x02\x01\x01")


class ModbusFrameTests(unittest.TestCase):
    def test_read_frame_matches_the_vendor_bytes(self):
        frame = modbus.build_read_state_request()
        self.assertEqual(modbus.hex_frame(frame), contract.MODBUS_FRAME_READ_STATE)

    def test_crc_verifies_and_detects_corruption(self):
        frame = modbus.build_read_state_request()
        self.assertTrue(modbus.crc_ok(frame))
        corrupted = bytearray(frame)
        corrupted[2] ^= 0xFF
        self.assertFalse(modbus.crc_ok(bytes(corrupted)))

    def test_group_write_frame_template(self):
        frame = modbus.build_write_all_request(1)
        self.assertEqual(modbus.hex_frame(frame)[:17], "02 06 00 30 00 01")
        self.assertTrue(modbus.crc_ok(frame))
        with self.assertRaises(contract.PduContractError):
            modbus.build_write_all_request(3)

    def test_backend_is_blocked_not_defaulted(self):
        status = modbus.backend_status()
        self.assertFalse(status["available"])
        self.assertEqual(status["state"], "blocked")
        self.assertIn("baud_rate", status["unknown"])
        self.assertEqual(contract.MODBUS_SERIAL_PARAMETERS, "unknown_from_inventory")


class LoopbackAgent(object):
    """Serves :class:`MemoryTransport` over a real UDP socket on 127.0.0.1."""

    def __init__(self, agent):
        self.agent = agent
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.settimeout(0.2)
        self.port = self.sock.getsockname()[1]
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def _serve(self):
        while not self._stop.is_set():
            try:
                data, peer = self.sock.recvfrom(snmp.MAX_UDP)
            except socket.timeout:
                continue
            except OSError:
                return
            try:
                reply = self.agent.request(data)
            except Exception:  # noqa: BLE001 - a real agent would just not answer
                continue
            self.sock.sendto(reply, peer)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *_exc):
        self._stop.set()
        self._thread.join(timeout=2)
        self.sock.close()


class UdpRoundTripTests(unittest.TestCase):
    def test_read_then_write_over_udp(self):
        agent = snmp.MemoryTransport(states={3: "off"})
        with LoopbackAgent(agent) as server:
            client = snmp.SnmpClient("lab-community", snmp.UdpTransport("127.0.0.1", server.port, timeout=1.0, attempts=1))
            try:
                code = client.get_int(contract.state_oid(3))
                self.assertEqual(code, contract.read_code("off"))
                client.set_int(contract.socket_oid(3), contract.write_code("on"))
                self.assertEqual(agent.state(3), "on")
                self.assertEqual(client.get_int(contract.state_oid(3)), contract.read_code("on"))
            finally:
                client.close()

    def test_silent_agent_times_out(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        client = snmp.SnmpClient("lab-community", snmp.UdpTransport("127.0.0.1", port, timeout=0.2, attempts=1))
        try:
            with self.assertRaises(snmp.SnmpTimeout):
                client.get_int(contract.state_oid(1))
        finally:
            client.close()


class MemoryAgentTests(unittest.TestCase):
    def test_write_code_applies_through_the_inverse_mapping(self):
        agent = snmp.MemoryTransport(states={1: "on"})
        client = snmp.SnmpClient("lab-community", agent)
        client.set_int(contract.socket_oid(1), contract.write_code("off"))
        self.assertEqual(agent.state(1), "off")
        client.set_int(contract.socket_oid(1), contract.write_code("on"))
        self.assertEqual(agent.state(1), "on")

    def test_reusing_a_read_code_as_a_write_flips_the_socket(self):
        # Documents the trap: read 2 means ON, so writing 2 turns the socket OFF.
        agent = snmp.MemoryTransport(states={1: "on"})
        client = snmp.SnmpClient("lab-community", agent)
        read_value = client.get_int(contract.state_oid(1))
        self.assertEqual(read_value, 2)
        client.set_int(contract.socket_oid(1), read_value)
        self.assertEqual(agent.state(1), "off")

    def test_client_never_leaks_the_community(self):
        agent = snmp.MemoryTransport()
        client = snmp.SnmpClient("super-secret-community", agent)
        self.assertNotIn("super-secret-community", repr(client))
        client.close()


if __name__ == "__main__":
    unittest.main()
