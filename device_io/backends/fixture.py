"""Fixture backend: replay a recorded snapshot.

This is the offline path. It makes every downstream contract (board grouping,
role resolution, rendering) testable on any machine, including CI, without USB
hardware and without lying about what was measured: a fixture snapshot keeps
its original ``generated_at`` and the backend name stays ``fixture``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from .. import schema
from ..models import AudioEndpoint, Port, Snapshot
from . import register
from .base import Backend, BackendError, BackendOptions, BackendResult

_FIXTURE_DIR = Path(__file__).resolve().parents[2] / "fixtures"


def _resolve(path: Optional[str]) -> Path:
    if not path:
        raise BackendError("fixture backend needs --fixture <path> (a recorded snapshot JSON)")
    candidate = Path(path).expanduser()
    if candidate.exists():
        return candidate
    named = _FIXTURE_DIR / path
    if named.suffix != ".json":
        named = named.with_suffix(".json")
    if named.exists():
        return named
    raise BackendError(
        "fixture not found: {0} (looked next to {1})".format(path, _FIXTURE_DIR)
    )


@register
class FixtureBackend(Backend):
    name = "fixture"
    os_name = "unknown"

    def available(self) -> tuple:
        return True, "offline replay"

    def collect(self, options: BackendOptions) -> BackendResult:
        path = _resolve(options.fixture)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise BackendError("cannot read fixture {0}: {1}".format(path, exc))
        snapshot = Snapshot.from_dict(data)
        ports = [Port.from_dict(item) for item in data.get("ports") or []]
        audio = [AudioEndpoint.from_dict(item) for item in data.get("audio") or []]
        warnings = list(snapshot.warnings)
        warnings.append(
            "fixture replay from {0}; generated_at={1} is the recording time, not now".format(
                path.name, snapshot.generated_at
            )
        )
        return BackendResult(
            backend="fixture",
            os=snapshot.os,
            ports=ports,
            audio=audio,
            warnings=warnings,
            sources=["fixture:{0}".format(path)],
        )


def list_fixtures() -> list:
    if not _FIXTURE_DIR.exists():
        return []
    return sorted(path.name for path in _FIXTURE_DIR.glob("*.json"))


__all__ = ["FixtureBackend", "list_fixtures", "schema"]
