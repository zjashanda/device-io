#!/usr/bin/env python3
"""Time-contract lint.

One rule: every timestamp this project writes is Beijing wall-clock time in the
form ``YYYY-MM-DD HH:MM:SS``. ``T``, ``Z``, ``+08:00`` and RFC3339 are
violations, and an unmodified external timestamp may only appear in a field
whose name starts with ``raw_``.

The lint runs three passes:

1. literals   -- any timestamp-shaped string in the tree must be in the allowed
                 shape, unless it sits in a ``raw_*`` field;
2. producers  -- code that builds a timestamp with a non-conforming API or
                 format string (``utcnow``, ``isoformat``, ``%Y-%m-%dT``,
                 ``toISOString``, PowerShell ``-Format o`` ...);
3. structure  -- the contract constants and the JSON Schema patterns still
                 agree with the format, and ``now_bj()`` actually emits it.

Escape hatch for a line that must show a forbidden shape (a doc listing what
is banned, a test asserting rejection): put ``time-lint: allow`` on that line
(``<!-- time-lint: allow -->`` inside Markdown).

Exit codes: 0 clean, 1 violations.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import List, Tuple

ROOT = Path(__file__).resolve().parents[1]
SELF = Path(__file__).resolve()
SKIP_DIRS = {".git", "__pycache__", "build", "dist", ".venv", "venv", ".mypy_cache", "node_modules"}
SKIP_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".zip", ".gz", ".dll", ".exe", ".pyc", ".pdf", ".lock"}

EXPECTED_FORMAT = "YYYY-MM-DD HH:MM:SS"
ALLOW_MARKER = "time-lint: allow"

TIMESTAMP_RE = re.compile(
    r"(?P<date>\d{4}-\d{2}-\d{2})(?P<sep>[T ])(?P<time>\d{2}:\d{2}:\d{2})"
    r"(?P<fraction>\.\d+)?(?P<zone>Z|[+-]\d{2}:?\d{2})?"
)

PRODUCER_PATTERNS: List[Tuple[str, "re.Pattern[str]"]] = [
    ("naive-datetime-now", re.compile(r"datetime\.now\s*\(\s*\)")),
    ("utcnow", re.compile(r"\butcnow\s*\(")),
    ("isoformat", re.compile(r"\.isoformat\s*\(")),
    ("strftime-T", re.compile(r"strftime\s*\(\s*['\"][^'\"]*%Y-%m-%dT")),
    ("strftime-offset", re.compile(r"strftime\s*\(\s*['\"][^'\"]*%z")),
    ("strptime-T", re.compile(r"strptime\s*\(\s*[^,]+,\s*['\"][^'\"]*%Y-%m-%dT")),
    ("toISOString", re.compile(r"toISOString\s*\(")),
    ("ps-format-iso", re.compile(r"Get-Date[^\n]*-Format\s+['\"]?(o|s|u|O|S|U)\b")),
    ("ps-tostring-T", re.compile(r"ToString\s*\(\s*['\"][^'\"]*yyyy-MM-ddT")),
    ("ps-asutc", re.compile(r"-AsUTC\b")),
    ("date-iso", re.compile(r"date\s+[^\n]*--iso-8601")),
]

RAW_FIELD_RE = re.compile(r"[\"']?(raw_[A-Za-z0-9_]*)[\"']?\s*[:=]\s*[\"']?$")


def iter_files(root: Path) -> List[Path]:
    files: List[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(name for name in dirnames if name not in SKIP_DIRS)
        for filename in sorted(filenames):
            path = Path(dirpath) / filename
            if path.resolve() == SELF:
                continue
            if path.suffix.lower() in SKIP_SUFFIXES:
                continue
            files.append(path)
    return files


def _raw_context(line: str, start: int) -> bool:
    """True when the timestamp at ``start`` is the value of a ``raw_*`` field."""
    head = line[:start]
    match = RAW_FIELD_RE.search(head)
    if match:
        return True
    # JSON one-liner: {"raw_foo": "2026-01-01T00:00:00Z"}
    window = head[-120:]
    return bool(re.search(r"[\"']raw_[A-Za-z0-9_]*[\"']\s*:\s*[\"']?$", window))


def scan_literals(path: Path, text: str) -> List[str]:
    problems: List[str] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        if ALLOW_MARKER in line:
            continue
        for match in TIMESTAMP_RE.finditer(line):
            if _raw_context(line, match.start()):
                continue
            ok = (
                match.group("sep") == " "
                and match.group("fraction") is None
                and match.group("zone") is None
            )
            if not ok:
                problems.append(
                    "{0}:{1}: timestamp {2!r} is not '{3}'".format(
                        path, lineno, match.group(0), EXPECTED_FORMAT
                    )
                )
    return problems


def scan_producers(path: Path, text: str) -> List[str]:
    problems: List[str] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        if ALLOW_MARKER in line:
            continue
        if path.suffix.lower() in (".md", ".txt", ".json"):
            continue
        for name, pattern in PRODUCER_PATTERNS:
            if pattern.search(line):
                problems.append(
                    "{0}:{1}: {2} builds a timestamp outside '{3}'".format(
                        path, lineno, name, EXPECTED_FORMAT
                    )
                )
    return problems


def check_structure() -> List[str]:
    problems: List[str] = []
    sys.path.insert(0, str(ROOT))
    try:
        from device_io import schema
    except Exception as exc:  # pragma: no cover - import guard
        return ["structure: cannot import device_io.schema: {0}".format(exc)]

    if schema.TIME_CONTRACT.get("format") != EXPECTED_FORMAT:
        problems.append(
            "structure: TIME_CONTRACT['format'] is {0!r}, expected {1!r}".format(
                schema.TIME_CONTRACT.get("format"), EXPECTED_FORMAT
            )
        )
    allowed = re.compile(schema.TIME_PATTERN)
    sample = schema.now_bj()
    if not allowed.match(sample):
        problems.append("structure: now_bj() returned {0!r}".format(sample))

    def walk(node, trail):
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "pattern" and isinstance(value, str) and "\\d{4}" in value:
                    if value != schema.TIME_PATTERN:
                        problems.append(
                            "structure: schema pattern at {0} is {1!r}, expected {2!r}".format(
                                trail, value, schema.TIME_PATTERN
                            )
                        )
                else:
                    walk(value, "{0}.{1}".format(trail, key))
        elif isinstance(node, list):
            for index, item in enumerate(node):
                walk(item, "{0}[{1}]".format(trail, index))

    for name, document in schema.all_schemas().items():
        walk(document, name)
    return problems


def main(argv: List[str]) -> int:
    target = Path(argv[1]).resolve() if len(argv) > 1 else ROOT
    problems: List[str] = []
    scanned = 0
    for path in iter_files(target):
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        scanned += 1
        problems.extend(scan_literals(path, text))
        problems.extend(scan_producers(path, text))
    problems.extend(check_structure())

    if problems:
        for problem in problems:
            print(problem)
        print("time lint: {0} violation(s) across {1} files".format(len(problems), scanned))
        return 1
    print("time lint: 0 violations across {0} files (format '{1}')".format(scanned, EXPECTED_FORMAT))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
