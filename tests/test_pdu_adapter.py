"""PDU adapter and CLI tests: verification, refusal, dry-run, exit codes.

Every test runs against :class:`device_io.pdu.snmp.MemoryTransport` (an
in-process PDU) or an offline plan. No socket is opened to a real device and no
hardware is switched, so the suite is safe to run on any node - including the
one whose rack it might otherwise cut.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import unittest
from unittest import mock

from device_io import cli, schema
from device_io.pdu import adapter, contract, credentials, snmp


class Clock(object):
    """Deterministic clock: ``sleep`` advances time instead of waiting."""

    def __init__(self) -> None:
        self.value = 0.0
        self.sleeps = []

    def now(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.value += seconds


def make_adapter(states=None, outlet=1, **kwargs):
    clock = Clock()
    transport = snmp.MemoryTransport(states=states or {1: "off", 2: "off"})
    for key, value in kwargs.items():
        setattr(transport, key, value)
    client = snmp.SnmpClient("lab-community", transport)
    pdu = adapter.PduAdapter(
        client,
        "10.9.9.9",
        outlet=outlet,
        community=credentials.Community("lab-community", "env:TEST"),
        probe_http=False,
        now=clock.now,
        sleep=clock.sleep,
        poll_timeout=5.0,
        poll_interval=1.0,
    )
    return pdu, transport, clock


class WriteTests(unittest.TestCase):
    def test_power_on_writes_the_write_code_and_verifies(self):
        pdu, transport, _clock = make_adapter(states={1: "off"})
        document = pdu.set_state("on", apply=True)
        self.assertTrue(document["executed"])
        self.assertTrue(document["verified"])
        self.assertEqual(document["after"]["state"], "on")
        self.assertEqual(transport.state(1), "on")
        self.assertEqual(transport.writes[0]["oid"], contract.socket_oid(1))
        self.assertEqual(transport.writes[0]["value"], 1)  # write: 1 = on

    def test_power_off_writes_the_inverse_code(self):
        pdu, transport, _clock = make_adapter(states={1: "on"})
        document = pdu.set_state("off", apply=True)
        self.assertTrue(document["verified"])
        self.assertEqual(transport.state(1), "off")
        self.assertEqual(transport.writes[0]["value"], 2)  # write: 2 = off
        self.assertEqual(document["before"]["read_code"], 2)  # read: 2 = on

    def test_already_in_target_state_sends_no_write(self):
        pdu, transport, _clock = make_adapter(states={1: "on"})
        document = pdu.set_state("on", apply=True)
        self.assertFalse(document["executed"])
        self.assertTrue(document["verified"])
        self.assertEqual(transport.writes, [])

    def test_every_socket_1_to_8_switches(self):
        for outlet in range(1, 9):
            pdu, transport, _clock = make_adapter(states={outlet: "off"}, outlet=outlet)
            document = pdu.set_state("on", apply=True)
            self.assertTrue(document["verified"], "outlet {0}".format(outlet))
            self.assertEqual(transport.state(outlet), "on", "outlet {0}".format(outlet))
            self.assertEqual(transport.writes[0]["oid"], contract.socket_oid(outlet))
            pdu.close()

    def test_all_sockets_write_is_unreachable(self):
        pdu, transport, _clock = make_adapter()
        with self.assertRaises(contract.PduContractError):
            contract.validate_outlet(255)
        with self.assertRaises(contract.PduContractError):
            contract.validate_outlet(0)
        # and no adapter call accepts a socket outside the range
        with self.assertRaises(contract.PduContractError):
            adapter.PduAdapter(
                snmp.SnmpClient("lab-community", transport), "10.9.9.9", outlet=9
            )

    def test_write_without_outlet_is_refused(self):
        transport = snmp.MemoryTransport()
        pdu = adapter.PduAdapter(
            snmp.SnmpClient("lab-community", transport), "10.9.9.9", probe_http=False
        )
        with self.assertRaises(contract.PduContractError) as ctx:
            pdu.set_state("on", apply=True)
        self.assertIn("outlet", str(ctx.exception))
        self.assertEqual(transport.writes, [])


class VerificationTests(unittest.TestCase):
    def test_state_unchanged_after_write_is_a_failure(self):
        pdu, transport, _clock = make_adapter(states={1: "off"}, ignore_writes=True)
        document = pdu.set_state("on", apply=True)
        self.assertTrue(document["executed"])
        self.assertFalse(document["verified"])
        self.assertEqual(transport.writes[0]["value"], 1)
        self.assertIn("failure", document)
        self.assertIn("unknown", document["failure"]["socket_may_be"])

    def test_poll_timeout_is_a_failure_not_a_success(self):
        pdu, transport, clock = make_adapter(states={1: "off"}, ignore_writes=True)
        document = pdu.set_state("on", apply=True, timeout_s=3.0)
        self.assertFalse(document["verified"])
        self.assertGreaterEqual(document["steps"][0]["attempts"], 2)
        self.assertLessEqual(clock.value, 3.0 + 1.0)

    def test_unreadable_socket_refuses_to_write(self):
        # Without a pre-read the adapter cannot claim anything about the socket,
        # so it fails loudly instead of writing blind.
        pdu, transport, _clock = make_adapter(states={1: "off"}, deaf=[contract.state_oid(1)])
        with self.assertRaises(snmp.SnmpTimeout):
            pdu.set_state("on", apply=True, timeout_s=3.0)
        self.assertEqual(transport.writes, [])


class PowerCycleTests(unittest.TestCase):
    def test_cycle_cuts_holds_restores_and_verifies(self):
        pdu, transport, clock = make_adapter(states={2: "on"}, outlet=2)
        document = pdu.power_cycle(apply=True, confirm_write=True, hold_s=10.0)
        self.assertTrue(document["executed"])
        self.assertTrue(document["verified"])
        self.assertEqual(transport.state(2), "on")
        self.assertEqual([write["value"] for write in transport.writes], [2, 1])
        self.assertIn(10.0, clock.sleeps)
        phases = [phase["phase"] for phase in document["phases"]]
        self.assertEqual(phases, ["cut", "hold", "restore"])

    def test_cycle_without_confirm_write_is_refused(self):
        pdu, transport, _clock = make_adapter(states={1: "on"})
        with self.assertRaises(contract.PduContractError):
            pdu.power_cycle(apply=True, confirm_write=False)
        self.assertEqual(transport.writes, [])

    def test_unverified_cut_aborts_before_restore(self):
        pdu, transport, _clock = make_adapter(
            states={1: "on"}, ignore_write_values=[contract.write_code("off")]
        )
        document = pdu.power_cycle(apply=True, confirm_write=True, hold_s=1.0)
        self.assertFalse(document["verified"])
        self.assertEqual(document["failure"]["phase"], "cut")
        self.assertIn("restore not attempted", document["failure"]["aborted"])
        self.assertEqual([write["value"] for write in transport.writes], [2])

    def test_failed_restore_reports_the_socket_may_be_off(self):
        pdu, transport, _clock = make_adapter(
            states={1: "on"}, ignore_write_values=[contract.write_code("on")]
        )
        document = pdu.power_cycle(apply=True, confirm_write=True, hold_s=1.0)
        self.assertFalse(document["verified"])
        self.assertEqual(document["failure"]["phase"], "restore")
        self.assertIn("OFF", document["failure"]["socket_may_be"])
        self.assertEqual([write["value"] for write in transport.writes], [2, 1, 1])
        restore = [phase for phase in document["phases"] if phase["phase"] == "restore"][0]
        self.assertEqual(len(restore["restore_attempts"]), 2)

    def test_dry_run_cycle_writes_nothing(self):
        pdu, transport, clock = make_adapter(states={1: "on"})
        document = pdu.power_cycle(apply=False, hold_s=10.0)
        self.assertFalse(document["apply"])
        self.assertFalse(document["executed"])
        self.assertEqual(transport.writes, [])
        self.assertEqual(clock.sleeps, [])


class PlanTests(unittest.TestCase):
    def test_offline_plan_needs_no_connection_and_lists_the_writes(self):
        pdu = adapter.PduAdapter(None, "10.9.9.9", outlet=4, transport_desc="offline")
        document = pdu.plan("power-cycle")
        self.assertEqual(document["schema"], contract.SCHEMA_PLAN)
        self.assertFalse(document["apply"])
        self.assertEqual(document["target"]["outlet"], 4)
        values = [step.get("value") for step in document["steps"] if step["op"] == "set"]
        self.assertEqual(values, [2, 1])  # write off, then on
        read_values = [step.get("value") for step in document["steps"] if step["op"] == "verify"]
        self.assertEqual(read_values, [1, 2])  # read off, then on
        self.assertTrue(any(step["op"] == "wait" for step in document["steps"]))

    def test_plan_requires_an_outlet(self):
        pdu = adapter.PduAdapter(None, "10.9.9.9", transport_desc="offline")
        with self.assertRaises(contract.PduContractError):
            pdu.plan("power-on")

    def test_status_document_shape(self):
        pdu, _transport, _clock = make_adapter(states={1: "on", 2: "off"}, outlet=None)
        document = pdu.status()
        self.assertEqual(document["schema"], contract.SCHEMA_STATUS)
        self.assertFalse(document["apply"])
        self.assertEqual(len(document["outlets"]), 8)
        self.assertEqual(document["summary"]["on"], 1)
        self.assertTrue(document["encoding"]["inverted"])
        self.assertRegex(document["generated_at"], schema.TIME_PATTERN)


class CredentialTests(unittest.TestCase):
    def test_env_wins_and_is_masked(self):
        community = credentials.resolve_community(env={credentials.ENV_VAR: "s3cr3t-value"})
        self.assertEqual(community.reveal(), "s3cr3t-value")
        self.assertNotIn("s3cr3t-value", repr(community))
        self.assertNotIn("s3cr3t-value", str(community))

    def test_missing_credentials_raise_with_instructions(self):
        with self.assertRaises(credentials.CommunityError) as ctx:
            credentials.resolve_community(env={}, secret_file="/nonexistent/ark/pdu-community")
        self.assertIn(credentials.ENV_VAR, str(ctx.exception))

    def test_loose_secret_file_permissions_are_refused(self):
        import tempfile

        handle = tempfile.NamedTemporaryFile("w", delete=False, suffix=".secret")
        handle.write("lab-community\n")
        handle.close()
        try:
            os.chmod(handle.name, 0o644)
            if os.name == "posix":
                with self.assertRaises(credentials.CommunityError) as ctx:
                    credentials.resolve_community(env={}, secret_file=handle.name)
                self.assertIn("chmod 600", str(ctx.exception))
                os.chmod(handle.name, 0o600)
            community = credentials.resolve_community(env={}, secret_file=handle.name)
            self.assertEqual(community.reveal(), "lab-community")
            self.assertTrue(community.source.startswith("file:"))
        finally:
            os.unlink(handle.name)

    def test_documents_never_contain_the_community(self):
        pdu, _transport, _clock = make_adapter(states={1: "off"})
        document = pdu.status()
        rendered = json.dumps(document, ensure_ascii=False)
        self.assertNotIn("lab-community", rendered)
        self.assertEqual(document["credential"]["value"], credentials.MASK)


class CliTests(unittest.TestCase):
    def run_cli(self, argv):
        out = io.StringIO()
        err = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_plan_runs_offline_without_credentials(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(credentials.ENV_VAR, None)
            code, out, _err = self.run_cli(
                ["pdu", "plan", "--host", "10.9.9.9", "--outlet", "3", "--plan-action", "power-cycle", "--format", "json"]
            )
        self.assertEqual(code, cli.EXIT_OK)
        document = json.loads(out)
        self.assertEqual(document["schema"], contract.SCHEMA_PLAN)
        self.assertFalse(document["apply"])

    def test_dry_run_write_needs_no_credentials(self):
        code, out, _err = self.run_cli(["pdu", "power-off", "--host", "10.9.9.9", "--outlet", "3"])
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("预演", out)

    def test_apply_without_credentials_is_a_usage_error(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(credentials.ENV_VAR, None)
            code, _out, err = self.run_cli(
                ["pdu", "power-off", "--host", "10.9.9.9", "--outlet", "3", "--apply"]
            )
        self.assertEqual(code, cli.EXIT_USAGE)
        self.assertIn(credentials.ENV_VAR, err)

    def test_outlet_range_is_enforced(self):
        for bad in ("0", "9", "255"):
            code, _out, err = self.run_cli(
                ["pdu", "power-on", "--host", "10.9.9.9", "--outlet", bad, "--apply"]
            )
            self.assertEqual(code, cli.EXIT_USAGE, bad)
            self.assertIn("outlet", err)

    def test_missing_outlet_is_enforced(self):
        code, _out, err = self.run_cli(
            ["pdu", "power-cycle", "--host", "10.9.9.9", "--apply", "--confirm-write"]
        )
        self.assertEqual(code, cli.EXIT_USAGE)
        self.assertIn("outlet", err)

    def test_power_cycle_requires_confirm_write(self):
        code, _out, err = self.run_cli(
            ["pdu", "power-cycle", "--host", "10.9.9.9", "--outlet", "2", "--apply"]
        )
        self.assertEqual(code, cli.EXIT_USAGE)
        self.assertIn("--confirm-write", err)

    def test_simulated_status_reports_the_encoding(self):
        code, out, _err = self.run_cli(
            ["pdu", "status", "--host", "127.0.0.1", "--simulate", "--format", "json"]
        )
        self.assertEqual(code, cli.EXIT_OK)
        document = json.loads(out)
        self.assertEqual(document["encoding"]["write"], {"1": "on", "2": "off"})
        self.assertEqual(len(document["outlets"]), 8)

    def test_simulated_cycle_verifies(self):
        code, out, _err = self.run_cli(
            [
                "pdu", "power-cycle", "--host", "127.0.0.1", "--simulate",
                "--outlet", "2", "--apply", "--confirm-write", "--hold", "1", "--format", "json",
            ]
        )
        self.assertEqual(code, cli.EXIT_OK)
        document = json.loads(out)
        self.assertTrue(document["verified"])

    def test_unverified_write_exits_5_and_never_echoes_the_community(self):
        secret = "lab-community-should-not-leak"
        client = snmp.SnmpClient(secret, snmp.MemoryTransport(states={1: "off"}, ignore_writes=True))
        with mock.patch.dict(os.environ, {credentials.ENV_VAR: secret}, clear=False):
            with mock.patch("device_io.pdu.adapter.build_client", return_value=client):
                code, out, err = self.run_cli(
                    [
                        "pdu", "power-on", "--host", "10.9.9.9", "--outlet", "1",
                        "--apply", "--timeout", "1", "--interval", "0.05",
                    ]
                )
        self.assertEqual(code, cli.EXIT_UNVERIFIED)
        self.assertIn("未通过校验", out)
        self.assertNotIn(secret, out)
        self.assertNotIn(secret, err)

    def test_doctor_never_requires_a_device(self):
        code, out, _err = self.run_cli(["pdu", "doctor", "--format", "json"])
        self.assertEqual(code, cli.EXIT_OK)
        document = json.loads(out)
        self.assertEqual([item["backend"] for item in document["backends"]], ["snmp", "modbus-rtu"])
        self.assertFalse(document["backends"][1]["available"])

    def test_modbus_backend_is_reported_as_blocked(self):
        code, out, _err = self.run_cli(
            ["pdu", "status", "--host", "10.9.9.9", "--pdu-backend", "modbus", "--format", "json"]
        )
        self.assertEqual(code, cli.EXIT_UNSUPPORTED)
        document = json.loads(out)
        self.assertFalse(document["supported"])
        self.assertIn("baud_rate", document["unknown"])

    def test_schema_listing_includes_the_pdu_contracts(self):
        code, out, _err = self.run_cli(["schema", "--list"])
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("pdu-status", out)
        self.assertIn("pdu-plan", out)


if __name__ == "__main__":
    unittest.main()
