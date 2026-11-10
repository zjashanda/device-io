"""device-io: canonical device I/O layer for Ark bench skills.

Read-only by default. ``collect_snapshot`` observes USB/serial/audio endpoints;
``resolve_roles`` maps them to named roles; ``clearusb`` previews (and only on
explicit request removes) Windows ghost devices. Burning, flashing and port
I/O stay in the calling skill (venusahisiburner, lstest, ...).
"""

from __future__ import annotations

from .models import AudioEndpoint, DriverInfo, Port, Snapshot
from .schema import SCHEMAS, TIME_CONTRACT, now_bj

__version__ = "0.1.0"


def __getattr__(name: str):
    # Lazy so ``import device_io`` never pulls a platform backend in.
    if name in ("collect_snapshot", "resolve_roles", "snapshot_document", "doctor"):
        from . import core

        return getattr(core, name)
    if name in ("check_drivers", "adapters", "load_plugin"):
        from . import drivers

        return getattr(drivers, name)
    raise AttributeError(name)


__all__ = [
    "AudioEndpoint",
    "DriverInfo",
    "Port",
    "SCHEMAS",
    "Snapshot",
    "TIME_CONTRACT",
    "__version__",
    "check_drivers",
    "collect_snapshot",
    "doctor",
    "load_plugin",
    "now_bj",
    "resolve_roles",
    "snapshot_document",
]
