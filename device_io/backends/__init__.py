"""Backend registry.

``get_backend("auto")`` picks the platform backend. Fixture playback works on
both operating systems and is how CI and other machines without a bench verify
the normalisation path.
"""

from __future__ import annotations

import platform
from typing import Dict, Type

from .base import Backend, BackendError, BackendOptions, BackendResult, BackendUnavailable

_REGISTRY: Dict[str, Type[Backend]] = {}


def register(cls: Type[Backend]) -> Type[Backend]:
    _REGISTRY[cls.name] = cls
    return cls


_LOADED = False


def _load_backends() -> None:
    global _LOADED
    if _LOADED:
        return
    # Imported here, not at module import time: each backend module imports
    # ``register`` from this package, so touching them earlier is circular.
    from . import fixture, linux, windows  # noqa: F401  (registration side effect)

    _LOADED = True


def get_backend(name: str = "auto") -> Backend:
    _load_backends()
    key = (name or "auto").strip().lower()
    if key == "auto":
        system = platform.system().lower()
        key = "windows" if system.startswith("win") else "linux"
    if key not in _REGISTRY:
        raise BackendUnavailable(
            "unknown backend {0!r}; expected one of {1}".format(
                name, ", ".join(sorted(["auto"] + list(_REGISTRY)))
            )
        )
    return _REGISTRY[key]()


def available_backends() -> Dict[str, tuple]:
    _load_backends()
    out: Dict[str, tuple] = {}
    for key in sorted(_REGISTRY):
        try:
            out[key] = _REGISTRY[key]().available()
        except Exception as exc:  # pragma: no cover - defensive
            out[key] = (False, "probe failed: {0}".format(exc))
    return out


__all__ = [
    "Backend",
    "BackendError",
    "BackendOptions",
    "BackendResult",
    "BackendUnavailable",
    "available_backends",
    "get_backend",
    "register",
]
