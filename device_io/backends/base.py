"""Backend contract.

A backend only answers one question: *what endpoints exist right now?* It
returns ``Port`` / ``AudioEndpoint`` objects plus warnings. Board grouping,
role resolution, rendering and driver checks all live above this layer, so a
new platform only has to implement ``collect``.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import List, Optional

from ..models import AudioEndpoint, Port


class BackendError(RuntimeError):
    """The backend could not observe the machine."""


class BackendUnavailable(BackendError):
    """This backend cannot run on the current host (wrong OS, missing tool)."""


@dataclass
class BackendOptions:
    """Knobs every backend accepts; unknown knobs are ignored on purpose."""

    include_disabled: bool = False
    include_audio: bool = True
    fixture: Optional[str] = None
    sysfs_root: Optional[str] = None
    timezone: str = "Asia/Shanghai"
    extra: dict = field(default_factory=dict)


@dataclass
class BackendResult:
    backend: str
    os: str
    ports: List[Port] = field(default_factory=list)
    audio: List[AudioEndpoint] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    sources: List[str] = field(default_factory=list)


class Backend(abc.ABC):
    name = "base"
    os_name = "unknown"

    @abc.abstractmethod
    def available(self) -> tuple:
        """Return (bool, reason). Never raises."""

    @abc.abstractmethod
    def collect(self, options: BackendOptions) -> BackendResult:
        """Observe the machine. Read-only by contract."""
