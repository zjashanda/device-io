"""Generic PDU power adapter: read state, switch one socket, power-cycle it.

Behaviour is defined by the NOAH-58 inventory of ``pudCorntrol.rar`` and by the
vendor's own recovery procedure, tightened in four places:

* the outlet is mandatory (the vendor CLI defaults it to ``0``, outside its own
  valid range) and the all-sockets form is refused outright;
* every write is followed by a read-back poll - a SET that "succeeded" but left
  the socket unchanged is a failure, never a success;
* a power cycle aborts before the restore step if the OFF transition cannot be
  *verified*, because an unverified OFF means we cannot claim the device lost
  power;
* nothing is written unless the caller passed ``apply`` (and, for a cycle,
  ``confirm_write``). Everything else returns a plan document.

Timing comes from an injected clock and sleeper so the poll/timeout contract is
testable without waiting and without touching hardware.
"""

from __future__ import annotations

import time
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Sequence

from .. import schema
from . import contract, modbus
from .credentials import MASK, Community
from .snmp import (
    MemoryTransport,
    SnmpClient,
    SnmpError,
    SnmpTimeout,
    Transport,
    UdpTransport,
)

ACTIONS = ("status", "outlet-status", "power-on", "power-off", "power-cycle", "doctor", "plan")

WRITE_ACTIONS = ("power-on", "power-off", "power-cycle")

DEFAULT_HOLD_SECONDS = 10.0        # vendor waitTime, so power is really lost
DEFAULT_POLL_TIMEOUT = 30.0
DEFAULT_POLL_INTERVAL = 1.0
DEFAULT_RESTORE_ATTEMPTS = 2

HTTP_PROBE_TIMEOUT = 2.0


class PduVerificationError(contract.PduError):
    """A write could not be confirmed by reading the socket back."""

    def __init__(self, message: str, document: Optional[Dict[str, Any]] = None) -> None:
        super(PduVerificationError, self).__init__(message)
        self.document = document or {}


def _encoding_contract() -> Dict[str, Any]:
    return {
        "read": {"1": "off", "2": "on"},
        "write": {"1": "on", "2": "off"},
        "inverted": True,
        "note": "write and read use opposite values; a read code must never be reused as a write value",
    }


class PduAdapter(object):
    """One PDU, one client, one socket at a time."""

    def __init__(
        self,
        client: Optional[SnmpClient],
        host: str,
        port: int = 161,
        outlet: Optional[int] = None,
        community: Optional[Community] = None,
        transport_desc: str = "",
        probe_http: bool = True,
        now: Optional[Callable[[], float]] = None,
        sleep: Optional[Callable[[float], None]] = None,
        poll_timeout: float = DEFAULT_POLL_TIMEOUT,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
    ) -> None:
        self.client = client
        self.host = contract.validate_host(host)
        self.port = int(port)
        self.outlet = contract.validate_outlet(outlet) if outlet is not None else None
        self.community = community
        if transport_desc:
            self.transport_desc = transport_desc
        elif client is not None:
            self.transport_desc = client.transport.describe()
        else:
            self.transport_desc = "offline (no connection)"
        self.probe_http = bool(probe_http)
        self._now = now or time.monotonic
        self._sleep = sleep or time.sleep
        self.poll_timeout = float(poll_timeout)
        self.poll_interval = float(poll_interval)

    # -- helpers -----------------------------------------------------------
    def _require_outlet(self) -> int:
        if self.outlet is None:
            raise contract.PduContractError(
                "an outlet number is required (--outlet 1..{0}); this adapter never picks one".format(
                    contract.SOCKET_MAX
                )
            )
        return self.outlet

    def _credential_block(self) -> Dict[str, Any]:
        if self.community is None:
            return {"required": True, "source": "none", "value": MASK}
        return {"required": True, "source": self.community.source, "value": MASK}

    def _base(self, schema_name: str, **extra: Any) -> Dict[str, Any]:
        document: Dict[str, Any] = {
            "schema": schema_name,
            "generated_at": schema.now_bj(),
            "backend": "snmp",
            "transport": self.transport_desc,
            "target": {"host": self.host, "port": self.port, "outlet": self.outlet},
            "encoding": _encoding_contract(),
            "credential": self._credential_block(),
        }
        document.update(extra)
        return document

    def close(self) -> None:
        if self.client is not None:
            self.client.close()

    def _client(self) -> SnmpClient:
        if self.client is None:
            raise contract.PduContractError("this adapter was built without a transport (plan-only)")
        return self.client

    # -- reads -------------------------------------------------------------
    def read_state(self, outlet: Optional[int] = None) -> contract.OutletState:
        number = contract.validate_outlet(outlet if outlet is not None else self._require_outlet())
        code = self._client().get_int(contract.state_oid(number))
        return contract.OutletState.from_read_code(code)

    def read_outlet(self, outlet: Optional[int] = None) -> Dict[str, Any]:
        number = contract.validate_outlet(outlet if outlet is not None else self._require_outlet())
        readings = self._client().get_many(
            [contract.state_oid(number), contract.current_oid(number), contract.name_oid(number)]
        )
        state = contract.OutletState.from_read_code(readings.get(contract.state_oid(number)))
        current = contract.scale(contract.current_oid(number), readings.get(contract.current_oid(number)))
        return {
            "outlet": number,
            "name": readings.get(contract.name_oid(number)),
            "state": state.name,
            "read_code": state.read_code(),
            "current_a": current,
        }

    def read_totals(self) -> Dict[str, Any]:
        oids = [
            contract.TOTAL_VOLTAGE_READ,
            contract.TOTAL_CURRENT_READ,
            contract.TOTAL_POWER_READ,
            contract.TOTAL_ENERGY_READ,
        ]
        try:
            readings = self._client().get_many(oids)
        except SnmpError as exc:
            return {"available": False, "reason": str(exc)}
        return {
            "available": True,
            "voltage_v": contract.scale(contract.TOTAL_VOLTAGE_READ, readings.get(contract.TOTAL_VOLTAGE_READ)),
            "current_a": contract.scale(contract.TOTAL_CURRENT_READ, readings.get(contract.TOTAL_CURRENT_READ)),
            "power_w": contract.scale(contract.TOTAL_POWER_READ, readings.get(contract.TOTAL_POWER_READ)),
            "energy_kwh": contract.scale(contract.TOTAL_ENERGY_READ, readings.get(contract.TOTAL_ENERGY_READ)),
        }

    def http_probe(self) -> Dict[str, Any]:
        """Pre-flight reachability as the vendor tooling does (HTTP GET, 200)."""
        if not self.probe_http:
            return {"performed": False, "reason": "disabled"}
        url = "http://{0}/".format(self.host)
        try:
            with urllib.request.urlopen(url, timeout=HTTP_PROBE_TIMEOUT) as response:
                return {"performed": True, "url": url, "status": int(response.status), "ok": int(response.status) == 200}
        except (urllib.error.URLError, OSError, ValueError) as exc:
            return {"performed": True, "url": url, "ok": False, "error": type(exc).__name__}

    def status(self, outlets: Optional[Sequence[int]] = None) -> Dict[str, Any]:
        document = self._base(contract.SCHEMA_STATUS, apply=False, supported=True)
        document["http_probe"] = self.http_probe()
        device: Dict[str, Any] = {"name": None}
        try:
            device["name"] = self._client().get_many([contract.DEVICE_NAME_READ]).get(contract.DEVICE_NAME_READ)
        except SnmpError as exc:
            device["name_error"] = str(exc)
        document["device"] = device
        wanted = list(outlets) if outlets is not None else (
            [self.outlet] if self.outlet is not None else list(range(contract.SOCKET_MIN, contract.SOCKET_MAX + 1))
        )
        rows: List[Dict[str, Any]] = []
        for number in wanted:
            try:
                rows.append(self.read_outlet(number))
            except SnmpError as exc:
                rows.append({"outlet": contract.validate_outlet(number), "state": "unknown", "error": str(exc)})
        document["outlets"] = rows
        document["totals"] = self.read_totals()
        document["summary"] = {
            "outlets": len(rows),
            "on": sum(1 for row in rows if row.get("state") == "on"),
            "off": sum(1 for row in rows if row.get("state") == "off"),
            "unknown": sum(1 for row in rows if row.get("state") == "unknown"),
        }
        document["verified"] = True
        return document

    # -- writes ------------------------------------------------------------
    def _poll(self, outlet: int, expected: contract.OutletState, timeout_s: float) -> Dict[str, Any]:
        started = self._now()
        deadline = started + float(timeout_s)
        attempts = 0
        last: Optional[str] = None
        observed: List[str] = []
        while True:
            attempts += 1
            try:
                state = self.read_state(outlet)
            except SnmpError as exc:
                last = "read failed: {0}".format(exc)
                state = None
            if state is not None:
                observed.append(state.name)
                if state == expected:
                    return {
                        "verified": True,
                        "attempts": attempts,
                        "elapsed_s": round(self._now() - started, 3),
                        "observed": observed,
                    }
                last = "state is {0}, expected {1}".format(state.name, expected.name)
            remaining = deadline - self._now()
            if remaining <= 0:
                return {
                    "verified": False,
                    "attempts": attempts,
                    "elapsed_s": round(self._now() - started, 3),
                    "observed": observed,
                    "reason": last or "no state observed",
                }
            self._sleep(min(self.poll_interval, remaining))

    def _write_step(self, outlet: int, desired: contract.OutletState, timeout_s: float) -> Dict[str, Any]:
        oid = contract.socket_oid(outlet)
        code = desired.write_code()
        self._client().set_int(oid, code)
        poll = self._poll(outlet, desired, timeout_s)
        step = {
            "outlet": outlet,
            "oid": oid,
            "value": code,
            "meaning": desired.name,
            "read_oid": contract.state_oid(outlet),
            "expect_read_code": desired.read_code(),
            "verified": poll["verified"],
            "attempts": poll["attempts"],
            "elapsed_s": poll["elapsed_s"],
            "observed": poll["observed"],
        }
        if not poll["verified"]:
            step["reason"] = poll.get("reason")
        return step

    def plan(self, action: str) -> Dict[str, Any]:
        """Offline description of what an action would do. Never opens a socket."""
        if action not in ("power-on", "power-off", "power-cycle"):
            raise contract.PduContractError(
                "plan covers power-on / power-off / power-cycle, got {0!r}".format(action)
            )
        outlet = self._require_outlet()
        desired = contract.STATE_ON if action == "power-on" else contract.STATE_OFF
        document = self._base(
            contract.SCHEMA_PLAN,
            action=action,
            apply=False,
            executed=False,
            verified=False,
            supported=True,
            current_state="unknown (plan is offline; run status to read it)",
            steps=[],
            preconditions=[
                "explicit PDU address ({0})".format(self.host),
                "explicit outlet number in 1..{0} ({1})".format(contract.SOCKET_MAX, outlet),
                "SNMP community from environment or a mode-600 secret file",
            ],
        )
        if action == "power-cycle":
            document["estimated_downtime_s"] = DEFAULT_HOLD_SECONDS
            document["preconditions"].append("--confirm-write on the apply run")
            document["steps"] = [
                self._plan_step(1, "read", outlet, "confirm the socket answers before cutting"),
                self._plan_step(2, "set", outlet, "cut power", contract.STATE_OFF),
                self._plan_step(3, "verify", outlet, "poll until OFF is observed", contract.STATE_OFF),
                {
                    "step": 4,
                    "op": "wait",
                    "seconds": DEFAULT_HOLD_SECONDS,
                    "why": "vendor waitTime, so the device really loses power",
                },
                self._plan_step(5, "set", outlet, "restore power", contract.STATE_ON),
                self._plan_step(6, "verify", outlet, "poll until ON is observed", contract.STATE_ON),
            ]
        else:
            document["steps"] = [
                self._plan_step(1, "read", outlet, "current state"),
                self._plan_step(2, "set", outlet, "switch {0}".format(desired.name), desired),
                self._plan_step(3, "verify", outlet, "poll until {0} is observed".format(desired.name), desired),
            ]
        document["refusals_guaranteed"] = [
            "no all-sockets write",
            "no write without --apply",
            "no power-cycle without --confirm-write",
            "no success report without a verified read-back",
        ]
        return document

    def _plan_step(
        self,
        step: int,
        op: str,
        outlet: int,
        why: str,
        state: Optional[contract.OutletState] = None,
    ) -> Dict[str, Any]:
        item: Dict[str, Any] = {"step": step, "op": op, "outlet": outlet, "why": why}
        if state is None:
            item["oid"] = contract.state_oid(outlet)
            item["read_encoding"] = {"1": "off", "2": "on"}
        else:
            item["oid"] = contract.socket_oid(outlet) if op == "set" else contract.state_oid(outlet)
            if op == "set":
                item["value"] = state.write_code()
                item["encoding"] = "write: 1=on 2=off"
            else:
                item["value"] = state.read_code()
                item["encoding"] = "read: 1=off 2=on"
            item["meaning"] = state.name
        return item

    def set_state(
        self,
        state: Any,
        apply: bool = False,
        confirm_write: bool = False,
        timeout_s: Optional[float] = None,
    ) -> Dict[str, Any]:
        outlet = self._require_outlet()
        desired = contract.OutletState(state)
        timeout = self.poll_timeout if timeout_s is None else float(timeout_s)
        if not apply:
            document = self.plan("power-on" if desired.on else "power-off")
            document["desired_state"] = desired.name
            document["note"] = "dry-run: nothing was written"
            return document
        document = self._base(
            contract.SCHEMA_PLAN,
            action="power-on" if desired.on else "power-off",
            apply=True,
            executed=False,
            verified=False,
            supported=True,
            steps=[],
        )
        before = self.read_state(outlet)
        document["before"] = {"state": before.name, "read_code": before.read_code()}
        if before == desired:
            document["executed"] = False
            document["verified"] = True
            document["verified_by"] = "read-back"
            document["note"] = "socket already {0}; no write sent".format(desired.name)
            document["after"] = {"state": before.name, "read_code": before.read_code()}
            return document
        document["executed"] = True
        step = self._write_step(outlet, desired, timeout)
        document["steps"] = [step]
        document["verified"] = step["verified"]
        document["verified_by"] = "read-back poll" if step["verified"] else "none"
        document["after"] = {"state": step["observed"][-1] if step["observed"] else "unknown"}
        if not step["verified"]:
            document["failure"] = {
                "phase": "switch-{0}".format(desired.name),
                "reason": step.get("reason"),
                "socket_may_be": "unknown - the write was accepted but never confirmed",
                "next": "re-run status; do not retry the write blindly",
            }
        return document

    def power_cycle(
        self,
        apply: bool = False,
        confirm_write: bool = False,
        hold_s: float = DEFAULT_HOLD_SECONDS,
        timeout_s: Optional[float] = None,
        restore_attempts: int = DEFAULT_RESTORE_ATTEMPTS,
    ) -> Dict[str, Any]:
        outlet = self._require_outlet()
        timeout = self.poll_timeout if timeout_s is None else float(timeout_s)
        if hold_s < 0:
            raise contract.PduContractError("hold seconds must be >= 0")
        if not apply:
            document = self.plan("power-cycle")
            document["hold_s"] = float(hold_s)
            document["note"] = "dry-run: nothing was written"
            return document
        if not confirm_write:
            raise contract.PduContractError(
                "power-cycle cuts power: pass --confirm-write together with --apply"
            )
        document = self._base(
            contract.SCHEMA_PLAN,
            action="power-cycle",
            apply=True,
            executed=False,
            verified=False,
            supported=True,
            hold_s=float(hold_s),
            phases=[],
        )
        before = self.read_state(outlet)
        document["before"] = {"state": before.name, "read_code": before.read_code()}

        # 1. cut power
        off_step = self._write_step(outlet, contract.STATE_OFF, timeout)
        document["phases"].append({"phase": "cut", **off_step})
        document["executed"] = True
        if not off_step["verified"]:
            document["failure"] = {
                "phase": "cut",
                "reason": off_step.get("reason"),
                "aborted": "restore not attempted: an unverified OFF means the device may not have lost power",
                "socket_may_be": "off or unknown",
                "next": "run status; verify manually before any further write",
            }
            return document

        # 2. hold, so the device really loses power
        self._sleep(float(hold_s))
        document["phases"].append({"phase": "hold", "verified": True, "seconds": float(hold_s)})

        # 3. restore, with bounded retries
        attempts: List[Dict[str, Any]] = []
        on_step: Optional[Dict[str, Any]] = None
        for attempt in range(1, max(1, int(restore_attempts)) + 1):
            step = self._write_step(outlet, contract.STATE_ON, timeout)
            step["attempt"] = attempt
            attempts.append(step)
            on_step = step
            if step["verified"]:
                break
        document["phases"].append(
            {
                "phase": "restore",
                "restore_attempts": attempts,
                "verified": bool(on_step and on_step["verified"]),
            }
        )
        document["verified"] = bool(on_step and on_step["verified"])
        document["verified_by"] = "read-back poll" if document["verified"] else "none"
        document["after"] = {
            "state": (on_step["observed"][-1] if on_step and on_step["observed"] else "unknown")
        }
        if not document["verified"]:
            document["failure"] = {
                "phase": "restore",
                "reason": on_step.get("reason") if on_step else "restore was not attempted",
                "socket_may_be": "OFF - the device is still unpowered",
                "next": "power the socket on manually (pdu power-on) once the PDU answers",
            }
        return document


# -- construction helpers -------------------------------------------------
def build_client(
    host: str,
    port: int = 161,
    community: Optional[Community] = None,
    timeout: float = 2.0,
    attempts: int = 2,
    transport: Optional[Transport] = None,
) -> SnmpClient:
    if transport is None:
        if community is None:
            raise contract.PduContractError("an SNMP community is required for a network transport")
        transport = UdpTransport(host, port, timeout=timeout, attempts=attempts)
        secret = community.reveal()
    else:
        secret = community.reveal() if community is not None else ""
        if not secret and isinstance(transport, MemoryTransport):
            secret = "simulated"
        if not secret:
            raise contract.PduContractError("an SNMP community is required")
    return SnmpClient(secret, transport, timeout=timeout)


def simulated_transport(states: Optional[Dict[int, Any]] = None) -> MemoryTransport:
    """An in-process PDU for ``--simulate``: no socket, no network, no hardware."""
    return MemoryTransport(
        states=states or {1: "on", 2: "off", 3: "off", 4: "off"},
        names={1: "bench-1", 2: "bench-2", 3: "bench-3", 4: "bench-4"},
        measurements={
            contract.TOTAL_VOLTAGE_READ: 2200,
            contract.TOTAL_CURRENT_READ: 150,
            contract.TOTAL_POWER_READ: 33000,
            contract.TOTAL_ENERGY_READ: 12345,
            contract.SOCKET_CURRENT_READ.format(n=1): 50,
        },
    )


def parse_simulated_states(text: Optional[str]) -> Dict[int, Any]:
    """``"1=on,2=off"`` -> ``{1: "on", 2: "off"}`` for ``--simulate-state``."""
    if not text:
        return {}
    states: Dict[int, Any] = {}
    for chunk in str(text).split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "=" not in chunk:
            raise contract.PduContractError("--simulate-state expects outlet=on|off, got {0!r}".format(chunk))
        key, value = chunk.split("=", 1)
        states[contract.validate_outlet(key.strip())] = contract.OutletState(value.strip()).name
    return states


def backend_report() -> List[Dict[str, Any]]:
    """Availability of each backend, for ``pdu doctor`` and ``doctor``."""
    return [
        {
            "backend": "snmp",
            "available": True,
            "transport": "stdlib UDP + hand-rolled BER (no pysnmp)",
            "protocol": "SNMPv2c GET/SET",
            "note": "community is injected; nothing is read from the environment until a command needs it",
        },
        modbus.backend_status(),
    ]


__all__ = [
    "ACTIONS",
    "DEFAULT_HOLD_SECONDS",
    "DEFAULT_POLL_INTERVAL",
    "DEFAULT_POLL_TIMEOUT",
    "PduAdapter",
    "PduVerificationError",
    "WRITE_ACTIONS",
    "backend_report",
    "build_client",
    "parse_simulated_states",
    "simulated_transport",
]
