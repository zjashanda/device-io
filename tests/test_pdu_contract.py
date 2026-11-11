"""PDU contract tests: the inverted encoding, the outlet range, the OIDs.

These tests exist because the vendor code has two ways to get the same socket
wrong: reuse a read value as a write value (the socket flips), or address the
all-sockets form (the rack goes dark). Both are asserted here.
"""

from __future__ import annotations

import unittest

from device_io.pdu import contract


class EncodingTests(unittest.TestCase):
    def test_read_encoding_is_1_off_2_on(self):
        self.assertEqual(contract.OutletState.from_read_code(1), contract.STATE_OFF)
        self.assertEqual(contract.OutletState.from_read_code(2), contract.STATE_ON)
        self.assertEqual(contract.read_code("off"), 1)
        self.assertEqual(contract.read_code("on"), 2)

    def test_write_encoding_is_1_on_2_off(self):
        self.assertEqual(contract.OutletState.from_write_code(1), contract.STATE_ON)
        self.assertEqual(contract.OutletState.from_write_code(2), contract.STATE_OFF)
        self.assertEqual(contract.write_code("on"), 1)
        self.assertEqual(contract.write_code("off"), 2)

    def test_read_and_write_are_inverted(self):
        # The whole point: the same state has different wire values.
        for state in ("on", "off"):
            self.assertNotEqual(contract.read_code(state), contract.write_code(state))
        self.assertEqual(contract.read_code("on"), contract.write_code("off"))
        self.assertEqual(contract.read_code("off"), contract.write_code("on"))

    def test_read_code_and_write_code_agree_on_meaning(self):
        for state in ("on", "off"):
            self.assertEqual(
                contract.OutletState.from_read_code(contract.read_code(state)).name, state
            )
            self.assertEqual(
                contract.OutletState.from_write_code(contract.write_code(state)).name, state
            )

    def test_invalid_codes_raise(self):
        for bad in (0, 3, 255, -1, None, "x"):
            with self.assertRaises(contract.PduEncodingError):
                contract.OutletState.from_read_code(bad)
            with self.assertRaises(contract.PduEncodingError):
                contract.OutletState.from_write_code(bad)

    def test_string_state_input(self):
        self.assertEqual(contract.OutletState("ON"), contract.STATE_ON)
        self.assertEqual(contract.OutletState("off"), contract.STATE_OFF)
        with self.assertRaises(contract.PduContractError):
            contract.OutletState("standby")

    def test_states_compare_by_meaning_not_by_code(self):
        self.assertEqual(contract.STATE_ON, "on")
        self.assertNotEqual(contract.STATE_ON, "off")
        self.assertEqual(contract.STATE_ON, contract.OutletState.from_write_code(1))
        self.assertEqual(contract.STATE_ON, contract.OutletState.from_read_code(2))


class OutletValidationTests(unittest.TestCase):
    def test_every_documented_socket_is_accepted(self):
        for outlet in range(1, 9):
            self.assertEqual(contract.validate_outlet(outlet), outlet)
            self.assertEqual(contract.validate_outlet(str(outlet)), outlet)

    def test_all_sockets_form_is_refused(self):
        for bad in (0, 255):
            with self.assertRaises(contract.PduContractError) as ctx:
                contract.validate_outlet(bad)
            self.assertIn("all-sockets", str(ctx.exception))

    def test_out_of_range_is_refused(self):
        for bad in (-1, 9, 100):
            with self.assertRaises(contract.PduContractError):
                contract.validate_outlet(bad)

    def test_missing_outlet_is_refused(self):
        for bad in (None, "", "   "):
            with self.assertRaises(contract.PduContractError) as ctx:
                contract.validate_outlet(bad)
            self.assertIn("required", str(ctx.exception))

    def test_non_numeric_outlet_is_refused(self):
        with self.assertRaises(contract.PduContractError):
            contract.validate_outlet("socket-1")

    def test_oid_builders_validate_too(self):
        with self.assertRaises(contract.PduContractError):
            contract.state_oid(0)
        with self.assertRaises(contract.PduContractError):
            contract.socket_oid(255)


class HostValidationTests(unittest.TestCase):
    def test_ip_and_hostname_accepted(self):
        self.assertEqual(contract.validate_host("10.3.37.30"), "10.3.37.30")
        self.assertEqual(contract.validate_host("pdu-rack-1.local"), "pdu-rack-1.local")
        self.assertEqual(contract.validate_host("[fe80::1]"), "fe80::1")

    def test_no_default_address(self):
        for bad in (None, "", " "):
            with self.assertRaises(contract.PduContractError) as ctx:
                contract.validate_host(bad)
            self.assertIn("required", str(ctx.exception))

    def test_junk_refused(self):
        for bad in ("-rf", "host name", "10.0.0.1; rm -rf /"):
            with self.assertRaises(contract.PduContractError):
                contract.validate_host(bad)


class OidTests(unittest.TestCase):
    def test_vendor_oids_match_the_inventory(self):
        self.assertEqual(contract.ENTERPRISE_PDU, "1.3.6.1.4.1.23280")
        self.assertEqual(
            contract.socket_oid(3), "1.3.6.1.4.1.23280.9.1.2.3"
        )
        self.assertEqual(
            contract.state_oid(3), "1.3.6.1.4.1.23280.8.1.2.3"
        )
        self.assertEqual(contract.current_oid(8), "1.3.6.1.4.1.23280.8.1.4.8")
        self.assertEqual(contract.name_oid(1), "1.3.6.1.4.1.23273.4.8.1")
        self.assertEqual(contract.name_oid(8), "1.3.6.1.4.1.23273.4.15.1")
        self.assertEqual(contract.DEVICE_NAME_READ, "1.3.6.1.2.1.1.1.0")

    def test_switch_and_state_oids_are_different_subtrees(self):
        for outlet in range(1, 9):
            self.assertNotEqual(contract.socket_oid(outlet), contract.state_oid(outlet))

    def test_oid_table_reports_the_inversion(self):
        table = contract.oid_table(2)
        self.assertTrue(table["inverted"])
        self.assertEqual(table["read_encoding"], {"1": "off", "2": "on"})
        self.assertEqual(table["write_encoding"], {"1": "on", "2": "off"})
        self.assertEqual(table["resolved"]["outlet"], 2)
        self.assertEqual(table["resolved"]["switch_write"], contract.socket_oid(2))
        self.assertEqual(table["resolved"]["state_read"], contract.state_oid(2))


class ScalingTests(unittest.TestCase):
    def test_documented_divisors(self):
        self.assertEqual(contract.scale(contract.TOTAL_VOLTAGE_READ, 2200), 220.0)
        self.assertEqual(contract.scale(contract.TOTAL_CURRENT_READ, 150), 1.5)
        self.assertEqual(contract.scale(contract.TOTAL_POWER_READ, 33000), 33.0)
        self.assertEqual(contract.scale(contract.TOTAL_ENERGY_READ, 12345), 12.345)
        self.assertEqual(contract.scale(contract.current_oid(4), 55), 0.55)

    def test_unknown_measurement_has_no_divisor(self):
        self.assertIsNone(contract.scale(contract.DEVICE_NAME_READ, 1))
        self.assertIsNone(contract.divisor_for(contract.socket_oid(1)))


if __name__ == "__main__":
    unittest.main()
