# Changelog

## 0.3.0 — 2026-11-11

Added: **generic PDU power control** (`device-io pdu`), adapted from
`pudCorntrol.rar` (NOAH-58 inventory, SHA-256 `76fe3443…a673`).

- `device_io.pdu` package: `contract` (OID map, read/write encoding inversion,
  outlet validation), `snmp` (hand-rolled SNMPv2c BER codec + UDP transport +
  an in-process simulated agent), `modbus` (frame builder, backend reported as
  BLOCKED), `adapter` (probe/read/set/power-cycle/plan), `credentials`
  (environment or mode-600 secret file only), `text` (renderers).
- CLI: `device-io pdu status | outlet-status | plan | power-on | power-off |
  power-cycle | doctor`, plus `--simulate` (in-process PDU, no socket).
- Safety contract: dry-run by default; `--apply` for writes; `--confirm-write`
  for cycles; outlet mandatory and limited to 1..8; all-sockets writes refused;
  every write followed by a read-back poll, with "state unchanged" treated as
  failure; `power-cycle` aborts before restore if the OFF transition cannot be
  verified.
- New schemas: `ark.device-pdu-status.v1`, `ark.device-pdu-plan.v1`.
- New exit code `5`: write accepted but not verified.
- Tests: encoding inversion (both directions), BER round trips including
  long-form lengths, a real UDP loopback agent, sockets 1..8, poll timeout,
  state-unchanged, failed cut / failed restore, all-sockets refusal, credential
  masking and permission checks, and CLI exit codes. No real device is touched.
- Docs: `docs/runbook-pdu.md`, `device_io/pdu/README.md`, README/interface
  updates.

Not included, deliberately: the vendor RAR, its `exe`, build output,
`secondary archives` and manual binaries stay out of Git (they are referenced by
the asset-tools/1006 manifest); the Modbus RTU transport is not enabled because
baud rate and parity are not recorded in the archive.

## 0.2.0 — 2026-11-11

**BREAKING:** the timestamp format changed. Every timestamp this tool writes is
now `YYYY-MM-DD HH:MM:SS` (Beijing wall-clock, second precision, no timezone in
the string). `T`, `Z`, `+08:00` and RFC3339 are rejected.

Changed:

- `schema.now_bj()` emits `%Y-%m-%d %H:%M:%S`; `TIME_CONTRACT`, `TIME_PATTERN`
  and the `generated_at` pattern in every JSON Schema use that form.
- `windows/Collect-DeviceIo.ps1` formats its `generated_at` the same way.
- Fixtures, transcripts in `README.md`/`SKILL.md`/`docs/interface.md` and unit
  test expectations use the new shape.
- Unmodified external timestamps are carried only in fields whose name starts
  with `raw_`; `schema.raw_fields()` copies them verbatim and the Windows
  normaliser preserves them in `extra`. Nothing reinterprets them as local time.
- Added `tools/time_lint.py`, wired into `tools/run-all.sh`, plus
  `tests/test_time_contract.py` covering the format, the schema patterns, the
  fixtures and the `raw_*` exemption.

Migration: documents produced by 0.1.0 carry the old `T`/millisecond shape and
must be re-recorded (rerun the collector or the fixture replay) instead of being
re-parsed with 0.2.0.

## 0.1.0 — 2026-11-10

First public release. Consolidates the device-enumeration parts of `usb-tree`,
`clearusb` and `venusahisiburner` into one stable interface.

Added:

- `device_io` package (stdlib only, Python 3.9+): `collect_snapshot`,
  `resolve_roles`, `check_drivers`, `doctor`.
- Backends: `linux` (read-only sysfs), `windows` (PowerShell CIM collector +
  pure-Python normaliser), `fixture` (offline replay of a recorded snapshot).
- Board grouping shared by both platforms: one physical board, one entry, with
  per-interface labels (`SERIAL-A..D`, `Ch A..H`, `VCOM`).
- Port role resolution with five explicit states and conflict detection.
- Driver adapter registry (CH34x/CH343/CH348, CP210x, FTDI, CDC-ACM, ListenAI
  VCOM) plus `ark.device-adapter.v1` plugins.
- `device-io` CLI: `ports`, `boards`, `audio`, `roles`, `driver`, `clearusb`,
  `doctor`, `schema`.
- Windows ghost-instance preview (`clearusb`) with `--apply` as the only
  mutating flag in the tool.
- Versioned JSON schemas, Beijing-no-offset timestamps, fixture replay, and a
  `tools/run-all.sh` gate (unittest + PowerShell parse + secret scan).

Notes:

- Windows support is verified against recorded fixtures and the PowerShell
  parser, not on live hardware in this release.
- Media/TCL device profiles remain deferred by the owner; no measured claims are
  made for them.
