"""SNMP community resolution: environment first, controlled file second.

The vendor source hard-coded the community string in ``pdusnmp.py``; the
inventory records that as the top-ranked credential risk. Here the value is
never a constant:

1. ``ARK_PDU_SNMP_COMMUNITY`` from the process environment, else
2. a secret file (default ``~/.config/ark/pdu-snmp-community``), which on POSIX
   must not be group/world readable - ``chmod 600`` is enforced, not suggested.

The value is wrapped in :class:`Community` whose ``repr``/``str`` are masked, so
an accidental ``print(client)``, a traceback, or a rendered document cannot
leak it. Nothing in this module writes the value anywhere.

The inventory also notes the vendor default is shared by the read and the write
path (and only takes effect after a PDU web-UI edit plus reboot), so one
community covers both directions here; a site that splits them can point the
write client at a second :class:`Community` instance.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Iterable, Optional

from .contract import PduContractError

ENV_VAR = "ARK_PDU_SNMP_COMMUNITY"
DEFAULT_SECRET_FILE = Path("~/.config/ark/pdu-snmp-community")

MASK = "***"


class CommunityError(PduContractError):
    """The community could not be resolved, or was not adequately protected."""


class Community(object):
    """A resolved community string that refuses to reveal itself."""

    __slots__ = ("_value", "source")

    def __init__(self, value: str, source: str) -> None:
        text = str(value).strip()
        if not text:
            raise CommunityError("SNMP community is empty")
        if len(text) > 255:
            raise CommunityError("SNMP community is longer than 255 bytes")
        if any(char.isspace() for char in text) or any(ord(char) < 0x20 for char in text):
            raise CommunityError("SNMP community contains whitespace or control characters")
        self._value = text
        self.source = source

    # The string itself is only reachable through the private attribute, so
    # logging the object, a dict of settings, or a dataclass repr stays clean.
    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "Community({0}, source={1!r})".format(MASK, self.source)

    __str__ = __repr__

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Community):
            return self._value == other._value
        return NotImplemented

    def __hash__(self) -> int:
        return hash(self._value)

    def __bool__(self) -> bool:
        return bool(self._value)


def _check_permissions(path: Path) -> None:
    if os.name != "posix":
        return
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError as exc:
        raise CommunityError("cannot stat secret file {0}: {1}".format(path, exc))
    if mode & 0o077:
        raise CommunityError(
            "secret file {0} is mode {1:o}; group/other bits must be cleared (chmod 600)".format(
                path, mode
            )
        )


def resolve_community(
    env: Optional[dict] = None,
    secret_file: Optional[object] = None,
) -> Community:
    """Resolve the community, or raise :class:`CommunityError`.

    ``env`` defaults to ``os.environ``; ``secret_file`` overrides the default
    path. The returned object never prints the value.
    """
    environment = os.environ if env is None else env
    raw = environment.get(ENV_VAR, "")
    if raw and str(raw).strip():
        return Community(str(raw), "env:{0}".format(ENV_VAR))

    path = Path(secret_file).expanduser() if secret_file else DEFAULT_SECRET_FILE.expanduser()
    if not path.exists():
        raise CommunityError(
            "no SNMP community: set {0} or create {1} with mode 600".format(ENV_VAR, path)
        )
    if not path.is_file():
        raise CommunityError("{0} is not a regular file".format(path))
    _check_permissions(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise CommunityError("cannot read secret file {0}: {1}".format(path, exc))
    return Community(text, "file:{0}".format(path))


def describe_sources(env: Optional[dict] = None, secret_file: Optional[object] = None) -> dict:
    """Non-secret description of where a community *would* come from.

    Used by ``pdu doctor`` so an operator can debug a missing credential
    without ever echoing the value.
    """
    environment = os.environ if env is None else env
    raw = environment.get(ENV_VAR, "")
    if raw and str(raw).strip():
        return {"available": True, "source": "env:{0}".format(ENV_VAR), "detail": "set"}
    path = Path(secret_file).expanduser() if secret_file else DEFAULT_SECRET_FILE.expanduser()
    if not path.exists():
        return {
            "available": False,
            "source": "none",
            "detail": "set {0} or create {1} (mode 600)".format(ENV_VAR, path),
        }
    detail = "present"
    if os.name == "posix":
        try:
            mode = stat.S_IMODE(path.stat().st_mode)
            detail = "mode {0:o}{1}".format(mode, "" if not mode & 0o077 else " (too open)")
        except OSError:
            detail = "unreadable"
    return {"available": True, "source": "file:{0}".format(path), "detail": detail}


def redact(value: object) -> str:
    """Replace a credential with a mask, keeping non-secrets untouched."""
    if isinstance(value, Community):
        return MASK
    return str(value)


def scrub(text: str, secrets: Iterable[object]) -> str:
    """Remove any known secret from free text (defence in depth for errors)."""
    result = str(text)
    for secret in secrets:
        raw = secret.reveal() if isinstance(secret, Community) else str(secret)
        if raw:
            result = result.replace(raw, MASK)
    return result


__all__ = [
    "Community",
    "CommunityError",
    "DEFAULT_SECRET_FILE",
    "ENV_VAR",
    "MASK",
    "describe_sources",
    "redact",
    "resolve_community",
    "scrub",
]
