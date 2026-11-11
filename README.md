# device-io

One stable interface for USB/serial device enumeration, physical-board grouping,
port-role resolution, ghost-device cleanup and driver adaptation across Windows
and Linux.

It exists because a burn or test flow that addresses ports by name (`COM3`,
`/dev/ttyUSB0`) breaks the moment Windows renumbers, two identical boards are
plugged in, or the wrong driver is bound. `device-io` answers five questions
with one JSON contract:

1. Which serial ports and audio endpoints are on this machine?
2. Which of them belong to the *same physical board*?
3. Which port plays which role (burn / log / control / prompt …)?
4. Is the driver bound the one this board needs?
5. Which disconnected device instances are cluttering the port list?

It never flashes firmware, never installs a driver and never edits a config. The
only mutating operation is `device-io clearusb --apply`, on Windows, with an
explicit flag.

```
$ device-io ports
物理 USB 板卡（1 个）
├─ CH344 Quad_Serial #1 (1a86:55d3)  <WCH CH344 Quad_Serial (4 路)>
│   ├─ CH344 Quad_Serial A  SERIAL-A  COM13
│   ├─ CH344 Quad_Serial B  SERIAL-B  COM15
│   ├─ CH344 Quad_Serial C  SERIAL-C  COM18
│   └─ CH344 Quad_Serial D  SERIAL-D  COM12
```

## Install

Python 3.9+, standard library only. No pip dependencies.

```bash
git clone https://github.com/zjashanda/device-io.git
cd device-io
python3 -m pip install -e .      # provides the `device-io` command
# or run without installing:
python3 -m device_io.cli ports
```

On Windows the collector shells out to `pwsh` (PowerShell 7) or `powershell`
(5.1); no module installation is required.

## Commands

| Command | What it does |
|---|---|
| `device-io ports` | Serial ports + audio endpoints grouped by physical board. `-f tree\|plain\|json\|csv\|md` |
| `device-io boards` | Just the board grouping, as `ark.device-boards.v1` JSON |
| `device-io audio` | Audio endpoints only |
| `device-io roles --spec roles.json` | Resolve port roles; `--save` writes the role map |
| `device-io driver list` | Known driver adapters (CH34x, CH343, CH348, CP210x, FTDI, CDC-ACM, ListenAI VCOM) |
| `device-io driver check --spec roles.json` | Compare expected vs bound driver per role |
| `device-io clearusb` | Preview disconnected Windows device instances; `--apply` removes them |
| `device-io pdu <action>` | Generic PDU power control over SNMP. `status`, `outlet-status`, `plan`, `power-on`, `power-off`, `power-cycle`, `doctor`. Dry-run by default; writes need `--apply`, cycles also `--confirm-write` |
| `device-io doctor` | Backend availability, PowerShell presence, host info |
| `device-io schema --list` | Every versioned JSON schema this tool emits |

Every command accepts `--backend auto\|linux\|windows\|fixture`, plus
`--fixture <name>` to replay a recording and `--sysfs-root <path>` to point the
Linux backend at a prepared tree. Exit codes: `0` ok, `1` usage/contract error,
`2` resolution failed, `3` unsupported platform, `4` backend unavailable,
`5` write accepted but not verified (the read-back never observed the requested
state - treat the socket as unknown and check it by hand).

## PDU power control

`device-io pdu` replaces the ad-hoc `pduControl.py` scripts with one reviewed
contract for the DPDU-style rack PDUs (SNMP v2c, 8 sockets):

```bash
device-io pdu status --host 10.0.0.9                      # read-only
device-io pdu power-cycle --host 10.0.0.9 --outlet 3      # dry-run: prints the OIDs
device-io pdu power-cycle --host 10.0.0.9 --outlet 3 --apply --confirm-write
```

Four rules make it safe to hand to an agent:

1. **The outlet is mandatory** (1..8, no default) and the all-sockets form
   (outlet `0` / `255`) is refused - that write can cut a whole rack.
2. **Dry-run is the default.** Writes need `--apply`; `power-cycle` additionally
   needs `--confirm-write`.
3. **Every write is verified by reading the socket back.** A SET that returns
   without an error but leaves the state unchanged is reported as a failure
   (exit `5`), never as success.
4. **The community string is never in the code.** It comes from
   `ARK_PDU_SNMP_COMMUNITY` or a mode-600 secret file, and it never appears in
   output, logs or documents.

The read and write OIDs use **opposite encodings** (`read: 1=off 2=on`,
`write: 1=on 2=off`); the adapter keeps them apart in one place and the test
suite pins the inversion. See [docs/runbook-pdu.md](docs/runbook-pdu.md) and
[device_io/pdu/README.md](device_io/pdu/README.md).

## Role resolution

Describe the bench once, resolve it every run:

```json
{
  "schema": "ark.device-roles-spec.v1",
  "strict": true,
  "board": {"vid": "1a86", "pid": "55d3"},
  "roles": [
    {"role": "burn", "interface": "SERIAL-A", "driver": "ch34x"},
    {"role": "log", "interface": "SERIAL-B"},
    {"role": "control", "interface": "SERIAL-C", "required": false}
  ]
}
```

```
$ device-io roles --spec roles.json
角色解析 2026-11-10 20:54:13  backend=windows  结果=OK
  burn       resolved  COM13    matched driver=ch34x, interface=SERIAL-A via interface SERIAL-A, CH344 Quad_Serial
  control    resolved  COM18    matched interface=SERIAL-C via interface SERIAL-C, CH344 Quad_Serial
  log        resolved  COM15    matched driver=ch34x, interface=SERIAL-B via interface SERIAL-B, CH344 Quad_Serial
  未分配端口: COM12
```

A role is `resolved`, `pinned`, `ambiguous`, `missing` or `conflict`. When two
roles claim one port both become `conflict` — the resolver never silently takes
the first match, and it never picks a port when two boards match equally.

## Python API

```python
from device_io import collect_snapshot, resolve_roles, check_drivers, role_ports

snapshot = collect_snapshot()                      # live, read-only
document = resolve_roles(snapshot, "roles.json")   # ark.device-role-map.v1
ports = role_ports(document)                       # {"burn": "COM13", "log": "COM15"}
print(check_drivers(snapshot, required=["ch34x"]))
```

`collect_snapshot()` returns a `Snapshot` with `ports`, `audio`, `boards`,
`warnings` and `sources`; `snapshot.to_dict()` is the same JSON the CLI prints.
Fixtures replay through the same path, so a recorded bench session is a
regression test.

## Platform differences

| | Windows | Linux |
|---|---|---|
| Enumerator | `Get-PnpDevice` + `Win32_PnPEntity` + `Get-PnpDeviceProperty` | `/sys/class/tty`, `/sys/class/sound` |
| Physical board key | ContainerID, else PnP parent instance id | upstream USB path (`1-1.2`) |
| Interface label | `MI_XX` → `SERIAL-A..D` / `Ch A..H` | interface number `:1.N` → same labels |
| Driver identity | service name (`CH343SER`, `usbser`, …) | kernel module (`ch341-uart`, `cdc_acm`, …) |
| Ghost cleanup | supported (`cfgmgr32` phantom scan) | not applicable — refused explicitly |

Board grouping, ordering and titles are computed in one place, so both
platforms print the same tree for the same hardware.

## Status

* Linux backend: implemented and verified against both synthetic sysfs trees and
  this host's live `/sys`.
* Windows backend: implemented, verified against recorded fixtures, the
  PowerShell parser and preview-path unit tests. It has **not** been run on live
  Windows hardware in this release; `device-io doctor` reports backend
  availability so a call site can tell the difference.
* `clearusb`: preview path is exercised by tests with a mocked PowerShell;
  `--apply` requires an elevated session and re-checks presence before removal.
* `pdu`: implemented against an in-process simulated PDU and a real UDP
  loopback agent; **never run against production hardware in this release**.
  No production PDU address or socket index is recorded anywhere, so every
  first run is necessarily a dry-run with an explicit `--host`. The Modbus RTU
  backend builds frames but is reported as blocked: the archive does not record
  baud rate or parity, and guessing them would corrupt frames silently.

No burn execution, no driver installation, no registry or network changes are
implemented here, by design.

## Layout

```
device_io/            package (schema, models, backends, boards, roles, drivers, render, cli)
windows/              PowerShell collectors invoked by the Windows backend
fixtures/             recorded snapshots (ark.device-io.v1) for offline replay
tests/                unittest suite; runs on any OS
docs/interface.md     the authoritative contract (schemas, CLI, API, non-goals)
docs/runbook-pdu.md   operator runbook for PDU power operations
MIGRATION.md          how venusahisiburner / lstest / usb-tree callers move over
SKILL.md              agent-facing usage notes
```

## Checks

```bash
tools/run-all.sh
```

Runs the unittest suite, package import, CLI contract, every fixture, the
PowerShell parser on each script, the offline/simulated PDU checks, the time
lint and a secret scan. All checks must pass before
a change lands.

## License

MIT — see [LICENSE](LICENSE).
