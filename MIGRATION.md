# Migrating to device-io

`device-io` absorbs the device-enumeration half of `usb-tree`, the ghost-instance
half of `clearusb`, and the port-guessing half of `venusahisiburner`. Nothing is
deleted: the old repositories stay public and keep working until their callers
move over.

## Why

`venusahisiburner` preflight does this (measured in the current source, not
paraphrased):

```python
# scripts/core/phases.py:35
def enumerate_ports():
    return [{"device": p.device, "description": p.description, "hwid": p.hwid}
            for p in serial.tools.list_ports.comports()]

present = {item["device"] for item in enumerate_ports()}
missing = [port for port in profile.expected_ports() if port not in present]
```

That compares **device names only**. Consequences on a real bench:

- Windows renumbers COM ports after a replug or a ghost instance; the profile
  says `COM3` and the board is now `COM7` → preflight fails although the board is
  fine.
- Two identical boards are indistinguishable: the first match wins.
- A wrong driver still looks like a present port, so the failure surfaces later
  as a burn timeout instead of "driver missing".
- The `tcl-pvm1005` profile stores `log_port: COM3` / `burn_port: COM3`, i.e.
  device names baked into data.

## What replaces it

| old | new |
|---|---|
| `serial.tools.list_ports.comports()` | `device_io.collect_snapshot()` |
| `profile.expected_ports()` name check | roles spec (`ark.device-roles-spec.v1`) + `resolve_roles()` |
| `usb-tree` CLI/GUI tree | `device-io ports --format tree` (same grouping rules) |
| `clearusb` `-Apply` script | `device-io clearusb --apply` (Windows, preview first) |
| ad-hoc `ch341ser` checks | `device-io driver check --spec roles.json` |

The profile keeps what is genuinely per-product (baud rates, burn command,
firmware assets). It stops carrying serial port names.

## Migration steps for venusahisiburner

1. Add `roles.json` next to the profile:

   ```json
   {
     "schema": "ark.device-roles-spec.v1",
     "strict": true,
     "board": {"vid": "8765", "pid": "5678"},
     "roles": [
       {"role": "log",  "interface": "VCOM", "required": true},
       {"role": "burn", "interface": "VCOM", "required": false}
     ]
   }
   ```

   For the CH344-based fixtures use `SERIAL-A`/`SERIAL-B` instead of `VCOM`.
   The `control` role of `tcl-pvm1005` (serial relay on `COM6`) should be
   `"pin": "COM6"` — a relay has no USB identity of its own, so pinning is
   honest about what is known.

2. Replace the preflight block:

   ```python
   snapshot = device_io.collect_snapshot()
   document = device_io.resolve_roles(snapshot, roles_path)
   if not document["ok"]:
       raise PreflightError(device_io.render_role_map(document))
   ports = device_io.role_ports(document)   # {"log": "COM4", "burn": "COM3"}
   ```

3. Keep the profile's expected roles but reference them by name; drop
   `log_port`/`burn_port` device strings from JSON profiles (leave the fields
   readable for one release, ignore them in code).

4. Record the snapshot and the role map in the run receipt. Both are plain JSON
   with `generated_at`, so a failed run can be explained after the fact —
   "the board was there, the burn role resolved to COM7" is auditable.

## Migration steps for lstest

`lstest` calls the same two functions. Its voice test needs a log port and a
prompt/capture path; both become roles (`log`, `prompt`). Do not duplicate the
sysfs/CIM walk — that duplication is what this repository exists to remove.

## Migration steps for usb-tree / clearusb users

- `usb-tree` GUI stays as-is for humans. For scripts, `device-io ports
  --format tree|md|csv` produces the same board grouping without .NET.
- `clearusb` keeps working. `device-io clearusb` calls the same
  `cfgmgr32` parent-resolution technique and refuses to touch present devices;
  it is the supported entry point for agents, because it returns JSON and an
  explicit `dry_run` flag.

## Compatibility promises

- Documents are additive-only within `v1`.
- Exit codes are frozen (see `docs/interface.md`).
- The Windows collector's raw format (`ark.device-io.raw.v1`) is internal: the
  normaliser may change as long as `ark.device-io.v1` does not.

## What is deliberately not migrated

- Media/TCL device profiles: deferred by the owner; no measured claims yet.
- `tcl-device-autoburn`: dead, not read, not migrated.
- Any burn execution: stays in `venusahisiburner`; `device-io` never opens a
  port.
