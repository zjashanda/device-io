"""High-level operations used by the CLI and by other skills' imports.

Other skills should import from here rather than from individual backends:

    from device_io.core import collect_snapshot, resolve_roles
    snapshot = collect_snapshot(backend="auto")
    ports = resolve_roles(snapshot, "bench-roles.json")
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Sequence

from . import schema
from .backends import BackendOptions, get_backend
from .models import Snapshot
from .roles import load_spec, resolve


def collect_snapshot(
    backend: str = "auto",
    fixture: Optional[str] = None,
    sysfs_root: Optional[str] = None,
    include_audio: bool = True,
    include_disabled: bool = False,
) -> Snapshot:
    """Observe the machine (read-only) and return a snapshot."""
    options = BackendOptions(
        fixture=fixture,
        sysfs_root=sysfs_root,
        include_audio=include_audio,
        include_disabled=include_disabled,
    )
    result = get_backend(backend).collect(options)
    return Snapshot(
        backend=result.backend,
        os=result.os,
        ports=result.ports,
        audio=result.audio,
        warnings=list(result.warnings),
        sources=list(result.sources),
    )


def resolve_roles(
    snapshot: Snapshot,
    spec: Any,
    strict: Optional[bool] = None,
) -> Dict[str, Any]:
    """Resolve a roles spec against a snapshot -> ``ark.device-role-map.v1``."""
    return resolve(snapshot, load_spec(spec), strict=strict)


def snapshot_document(
    backend: str = "auto",
    fixture: Optional[str] = None,
    sysfs_root: Optional[str] = None,
    include_boards: bool = True,
) -> Dict[str, Any]:
    return collect_snapshot(
        backend=backend, fixture=fixture, sysfs_root=sysfs_root
    ).to_dict(include_boards=include_boards)


def doctor() -> Dict[str, Any]:
    """Environment report: which backends work, and why the others do not."""
    from .backends import available_backends

    report: Dict[str, Any] = {
        "schema": "ark.device-io.doctor.v1",
        "generated_at": schema.now_bj(),
        "os": _host_os(),
        "backends": {},
        "warnings": [],
    }
    for name, (ok, reason) in available_backends().items():
        report["backends"][name] = {"available": ok, "reason": reason}
    return report


def _host_os() -> str:
    import platform

    system = platform.system().lower()
    if system.startswith("win"):
        return "windows"
    if system.startswith("linux"):
        return "linux"
    return system or "unknown"


__all__ = ["collect_snapshot", "doctor", "resolve_roles", "snapshot_document"]
