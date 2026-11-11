# Runbook: PDU power operations

Applies to the DPDU-family rack PDU (8 sockets, SNMP v2c) driven by
`device-io pdu`. Written for the person on the other end of a failed power
cycle, not for a first-time reader.

## Before the first run

You need three facts that the vendor archive cannot give you:

| fact | where it comes from |
|---|---|
| PDU address | the DCIM/rack sheet. The archive's IPs (`10.3.37.30`, `192.168.0.163`, …) are stale samples |
| socket index | the label on the PDU or the socket name OID (`pdu status` shows it) |
| community string | the PDU web UI; store it in `ARK_PDU_SNMP_COMMUNITY` or `~/.config/ark/pdu-snmp-community` (mode 600) |

Set the secret once per machine:

```bash
install -d -m 700 ~/.config/ark
printf '%s\n' "$PDU_COMMUNITY" > ~/.config/ark/pdu-snmp-community
chmod 600 ~/.config/ark/pdu-snmp-community
```

Anything group- or world-readable is refused, not warned about.

## Read first

```bash
device-io pdu doctor --format json                        # config only, no I/O
device-io pdu status --host 10.0.0.9                      # all 8 sockets
device-io pdu outlet-status --host 10.0.0.9 --outlet 3    # one socket, with current
```

`status` also prints the HTTP pre-flight result (the vendor tooling does a
`GET http://<ip>` before SNMP) and the total voltage/current/power.

## Rehearse

```bash
device-io pdu power-cycle --host 10.0.0.9 --outlet 3
```

Prints the exact OIDs and values, the hold window, and the verification step.
It opens **no** socket: `plan`-style output is produced offline, so it works
from a machine that cannot reach the PDU VLAN. Nothing is written.

## Execute

```bash
device-io pdu power-on  --host 10.0.0.9 --outlet 3 --apply
device-io pdu power-off --host 10.0.0.9 --outlet 3 --apply
device-io pdu power-cycle --host 10.0.0.9 --outlet 3 --apply --confirm-write
```

`power-cycle` cuts power for `--hold` seconds (default 10, the vendor
`waitTime`) because some devices need a real power loss, not a glitch. Increase
it for devices with large bulk capacitors; do not set it to `0` "to be faster".

Exit codes:

| code | meaning | what to do |
|---|---|---|
| 0 | the requested state was observed by a read-back | proceed |
| 5 | write accepted, state never confirmed | **check the socket**: `pdu status`. Do not retry the write blindly |
| 4 | PDU unreachable / SNMP timeout | check network and VLAN before touching anything |
| 1 | usage: missing `--outlet`, outlet out of range, missing `--confirm-write`, missing credential | fix the invocation |
| 3 | `--pdu-backend modbus` | the Modbus transport is blocked (serial parameters unknown) |

## When a power cycle fails

Run `device-io pdu status --host <ip>` and read `failure` in the JSON (or the
text output):

| `failure.phase` | meaning | action |
|---|---|---|
| `cut` | OFF was never observed; the cycle aborted **before** restore | the device may not have lost power. Verify by hand; do not assume a reboot happened |
| `restore` | OFF worked, ON never verified | the socket is most likely still **OFF**. Fix the PDU connection, then `pdu power-on --apply` once |

Never run `power-off` on a socket you have not just identified: `--outlet` is
mandatory precisely so that this decision is made by a human, not by a default.

## Forbidden without a second approval

* Outlet `0` or `255` (all sockets). Rejected by the code - do not work around
  it with a raw SNMP tool.
* Any write against a socket carrying the shared storage/network PDU.
* Automating a cycle from a dispatcher without a task-level approval: the
  adapter may be selected by `ark-dispatch`, but the power cut itself is an
  approved action.
