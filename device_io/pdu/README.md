# PDU adapter

Generic power control for the DPDU-family rack PDU (8 sockets, SNMP v2c),
adapted from the vendor `pudCorntrol` bundle. The bundle itself is **not** in
this repository: it is an asset registered with the asset-tools/1006 manifest
(`pudCorntrol.rar`, 185,754,459 bytes, SHA-256 `76fe3443…a673`). Only the
behaviour was reimplemented, from the NOAH-58 inventory.

## Why this is not a copy of `pdusnmp.py`

The vendor scripts are 60 lines of `pysnmp` calls with three problems that turn
a lab convenience into a rack-wide accident:

| vendor code | here |
|---|---|
| hard-coded read/write community | resolved from `ARK_PDU_SNMP_COMMUNITY` or a mode-600 secret file |
| `--socket` defaults to `0`, outside its own 1..8 range | the outlet is mandatory and validated before any I/O |
| writes to `…23273.4.40.0` reach every socket | the all-sockets form (0 / 255) is refused outright |
| write is assumed to have worked | every write is confirmed by polling the state OID |
| read value reused as a write value | the two encodings live in `contract.py` and nowhere else |

## The encoding inversion

```
read  OID  1.3.6.1.4.1.23280.8.1.2.{n}   1 = off   2 = on
write OID  1.3.6.1.4.1.23280.9.1.2.{n}   1 = on    2 = off
```

Copying a value from a read into a write flips the socket. `contract.read_code`
and `contract.write_code` are the only mapping sites; `OutletState` compares by
meaning (`"on"` / `"off"`), never by wire value, and
`tests/test_pdu_snmp.py::MemoryAgentTests::test_reusing_a_read_code_as_a_write_flips_the_socket`
pins the failure mode so nobody "simplifies" it back.

## Modules

| module | role |
|---|---|
| `contract.py` | OID table, both encodings, outlet/host validation, measurement divisors, Modbus constants |
| `snmp.py` | SNMPv2c BER codec, `UdpTransport`, `MemoryTransport` (in-process PDU agent) |
| `modbus.py` | CRC-16/MODBUS and frame builders; backend reported as **blocked** |
| `adapter.py` | `status`, `read_outlet`, `set_state`, `power_cycle`, `plan` |
| `credentials.py` | community resolution and masking |
| `text.py` | Chinese text renderers for `--format text` |

## Power-cycle sequence

```
read state            (must answer, otherwise nothing is written)
write write-code off  (2)
poll read OID until 1 (off)      -- if this never verifies, ABORT, do not restore:
                                    an unverified OFF means we cannot claim the
                                    device lost power
wait hold seconds     (vendor waitTime, default 10)
write write-code on   (1)
poll read OID until 2 (on)       -- retried (default 2 attempts)
```

If the restore still cannot be verified the document says so explicitly
(`failure.socket_may_be: "OFF - the device is still unpowered"`) and the CLI
exits `5`. A socket that is off is the fail-safe state; hiding that would be
worse than reporting it.

## Modbus RTU: blocked, not defaulted

The vendor frames are recorded (`02 03 00 30 00 01 84 36` for the state read;
`02 06 00 30 00 XX` for the group write) and the CRC is verified against those
bytes in the tests. The serial line parameters are **not** in the archive:
baud rate, parity, stop bits and the physical port are all unknown. A guessed
baud rate produces frames the PDU silently ignores or misreads, so
`modbus.backend_status()` reports `available: false` and the CLI refuses with
exit `3` until someone captures the values from a live unit.

## Testing without a rack

Two doubles, no hardware:

* `MemoryTransport` - an in-process agent with a state dict and knobs for the
  failure paths (`deaf`, `ignore_writes`, `ignore_write_values`, `refuse`).
  It applies write codes through the inverse mapping, so a wrong adapter
  encoding shows up as the opposite socket state.
* a UDP loopback agent in `tests/test_pdu_snmp.py` - the same agent served over
  a real `127.0.0.1` socket, which exercises the BER framing and `UdpTransport`.

`--simulate` wires `MemoryTransport` into the CLI for demos and for
`tools/run-all.sh`; it never opens a socket.

## Non-goals

* No GETNEXT walk, traps, SNMPv3, or per-socket scheduling.
* No binding of "socket 3 is always the DUT" - that is a profile for the caller,
  not a property of the adapter.
* No real power cut in CI, ever.
