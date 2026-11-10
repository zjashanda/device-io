# device-io interface contract

This document is the stable surface other skills program against. `venusahisiburner`
and `lstest` must use these entry points instead of enumerating COM/tty devices
themselves; that is the whole point of this repository.

Two rules hold for every command and every API below:

1. **Read-only by default.** Nothing here opens a serial port, resets a device,
   installs a driver, or writes to a node. The only mutating operation is
   `device-io clearusb --apply`, which is Windows-only, preview-by-default, and
   limited to non-present (ghost) device instances.
2. **Versioned JSON.** Every document carries a `schema` key of the form
   `ark.device-<name>.v1`. Adding a field is a minor change; removing or
   renaming one requires `.v2`. The timestamp format below is part of that
   contract, not a formatting preference: it was corrected to the workspace
   `YYYY-MM-DD HH:MM:SS` standard in package 0.2.0, and any value produced by
   0.1.0 must be re-recorded rather than re-parsed.

## Schemas

| schema | produced by | purpose |
|---|---|---|
| `ark.device-io.v1` | `ports`, `boards`, `audio`, `snapshot_document()` | one snapshot of serial ports + audio endpoints |
| `ark.device-boards.v1` | `boards` | physical board grouping derived from a snapshot |
| `ark.device-roles-spec.v1` | authored by the caller | which role each port plays |
| `ark.device-role-map.v1` | `roles` | resolution result per role |
| `ark.device-clear-plan.v1` | `clearusb` | ghost-device preview / apply result |
| `ark.device-driver-check.v1` | `driver check` | driver presence per family |
| `ark.device-adapter.v1` | plugin files | third-party board adapters |
| `ark.device-io.raw.v1` | `windows/Collect-DeviceIo.ps1` | raw Windows collector output (internal) |

`device-io schema` prints one schema; `device-io schema --list` lists them;
`device-io schema snapshot --format json` emits the JSON Schema used for
validation.

## Timestamps

All documents use the workspace time contract:

```
zone:             Asia/Shanghai
offset_in_string: none
format:           YYYY-MM-DD HH:MM:SS
example:          2026-11-10 21:05:00
forbidden:        T separator, Z suffix, +08:00 offset, RFC3339
raw_exception:    fields named raw_* may carry an unmodified external timestamp
```

`generated_at` is Beijing wall-clock time at second precision: no `T`
separator, no `Z`, no `+08:00`. Parsers must not reinterpret it as UTC, and a
value in any other shape is a contract violation (`SchemaError`).

A field whose name starts with `raw_` is the only exemption. It carries an
external timestamp exactly as the source system reported it, in whatever format
that system uses, and consumers must treat it as opaque rather than parsing it
as local time. `device_io.schema.raw_fields()` copies those entries verbatim.

`tools/time_lint.py` enforces this over the whole tree; it runs as part of
`tools/run-all.sh`.

## CLI

```
device-io ports    [--backend auto|linux|windows|fixture] [--fixture NAME]
                   [--sysfs-root DIR] [--no-audio] [--include-disabled]
                   [--format tree|plain|json|csv|md] [--out FILE]
device-io boards   [same backend flags] [--format ...]
device-io audio    [same backend flags] [--format ...]
device-io roles    --spec FILE [--strict|--no-strict] [--format text|json] [--save FILE]
device-io driver   list [--format json|table]
device-io driver   check (--require csv | --spec FILE) [--format json|text]
device-io clearusb [--apply] [--board vid:pid] [--timeout SECONDS] [--format json|text]
device-io doctor   [--format json|text]
device-io schema   [NAME] [--list]
```

Exit codes are part of the contract:

| code | meaning |
|---|---|
| 0 | success |
| 1 | usage error, malformed spec/schema, or unsupported operation |
| 2 | role resolution failed (`ok: false`) |
| 3 | operation unsupported on this platform |
| 4 | backend unavailable (e.g. Windows collector on Linux) |

Callers should treat `2`, `3` and `4` as distinct: `2` means hardware is present
but the mapping is wrong (operator action), `4` means the runner cannot see
hardware at all (environment action).

## Role spec

`ark.device-roles-spec.v1` replaces hardcoded port names:

```json
{
  "schema": "ark.device-roles-spec.v1",
  "strict": true,
  "board": {"vid": "1a86", "pid": "55d3"},
  "roles": [
    {"role": "burn", "interface": "SERIAL-A", "driver": "ch34x", "required": true},
    {"role": "log",  "interface": "SERIAL-B", "required": true},
    {"role": "control", "pin": "COM6", "required": false}
  ]
}
```

- `board` (optional) constrains every role to one physical board. Keys: `vid`,
  `pid`, `serial`, `product`, `board_key`. Matching is exact after lowercasing;
  an empty value means "do not care".
- Per-role expectations: `interface`, `driver` (family name), `product`, `vid`,
  `pid`, matched as case-insensitive substrings against the port.
- `pin` (optional) forces a device name (`COM6`, `/dev/ttyUSB0`); it wins over
  expectations and reports `pinned`, or `conflict` when that device belongs to
  another board.
- `required` (default `true`) participates in `ok` when `strict` is false.
- `strict` (default `true`) means every declared role must resolve, including
  optional ones.
- `allow` (optional list of device names) is a last-resort fallback when an
  expectation matches nothing — useful while a board's interface labels are
  still being confirmed.

Five states, never silently collapsed:

| state | meaning | caller action |
|---|---|---|
| `resolved` | exactly one port matched | proceed |
| `pinned` | caller-named device present (note lists unmet expectations) | proceed |
| `ambiguous` | several ports matched | pin or tighten the spec |
| `missing` | nothing matched; `reason` includes the closest port | check cabling/driver |
| `conflict` | one port claimed twice, or pin on the wrong board | fix the spec |

`ok` is true only when every required role is `resolved`/`pinned` (strict: all
roles). A port claimed by two roles downgrades **both** to `conflict`.

## Python API

```python
from device_io import collect_snapshot, resolve_roles, check_drivers, load_plugin

snap = collect_snapshot()                       # auto backend, read-only
doc  = resolve_roles(snap, "roles.json")        # dict, ark.device-role-map.v1
ports = {role: entry["port"] for role, entry in doc["roles"].items()}
```

Stable names: `collect_snapshot(backend="auto", fixture=None, sysfs_root=None,
include_audio=True, include_disabled=False)`, `resolve_roles(snapshot, spec,
strict=None)`, `check_drivers(snapshot, required=None, os_name=None,
dry_run=True)`, `doctor()`, `load_plugin(path, replace=False)`.

Anything prefixed with `_` is internal and may change without notice. The
`device_io.backends` package is an extension point: subclass `Backend`,
implement `available()` and `collect()`, and register it — do not fork the
package.

## Platform differences (kept, not hidden)

| aspect | Windows | Linux |
|---|---|---|
| enumeration | PowerShell CIM (`Get-CimInstance`, `Get-PnpDevice`) | sysfs files only |
| board identity | Container ID, else PnP parent instance id | upstream USB device path (`1-1.2`) |
| interface label | `MI_XX` mapped to `SERIAL-A..D` / `Ch A..H` / `VCOM` | interface directory suffix `:1.N` |
| driver evidence | driver service name + device status | `driver` symlink basename (kernel module) |
| audio | PnP audio endpoints | `/sys/class/sound` + `/proc/asound/pcm` when available |
| ghost USB instances | real concept; `clearusb --apply` can remove them | not applicable — refused, not faked |

`device-io clearusb` on Linux returns `supported: false` with a reason and exit
code 3. It does not pretend to have cleaned anything.

## Dry-run

`clearusb` previews by default; `--apply` is the only mutating flag in the whole
CLI. Every document sets `dry_run` explicitly so a caller can refuse to act on a
preview. `driver check` is always a report, never an installer.

## What this repository is not

- It does not burn firmware. Burn orchestration stays in `venusahisiburner`.
- It does not own test profiles or device-specific burn parameters.
- It does not replace `usb-tree`'s GUI; the CLI tree renderer exists so logs and
  agents have a readable snapshot.
