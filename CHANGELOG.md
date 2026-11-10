# Changelog

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
