#!/usr/bin/env python3
"""Secret scan: fail if a credential-looking string reached the repo.

This is a guard, not a proof. It looks for the shapes that actually leak in
practice (personal access tokens, cloud keys, private keys, inline passwords)
and skips binary files and the build/vcs directories.

Exit codes: 0 clean, 1 findings, 2 scan error.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path
from typing import Iterable, List, Tuple

SKIP_DIRS = {".git", "__pycache__", "build", "dist", ".venv", "venv", ".mypy_cache", "node_modules"}
SKIP_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".zip", ".gz", ".dll", ".exe", ".pyc", ".pdf"}

PATTERNS: List[Tuple[str, "re.Pattern"] ] = [
    ("github-pat", re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}")),
    ("github-fine-pat", re.compile(r"github_pat_[A-Za-z0-9_]{20,}")),
    ("aws-akid", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("private-key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----")),
    ("slack-token", re.compile(r"xox[abposr]-[A-Za-z0-9-]{10,}")),
    ("gitlab-pat", re.compile(r"glpat-[A-Za-z0-9_-]{20,}")),
    ("inline-password", re.compile(r"(?i)\b(password|passwd|pwd)\s*[=:]\s*['\"][^'\"]{6,}['\"]")),
    ("sshpass", re.compile(r"sshpass\s+-p\s+\S+")),
    ("bearer-literal", re.compile(r"(?i)authorization:\s*bearer\s+[A-Za-z0-9._-]{20,}")),
]

# Lines that only mention the words are fine; these keep the report useful.
ALLOW = re.compile(r"(?i)(placeholder|example|dummy|redacted|xxxx|<token>|\$\{?[A-Z_]+\}?|%[A-Z_]+%)")


def iter_files(root: Path) -> Iterable[Path]:
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(name for name in dirnames if name not in SKIP_DIRS)
        for filename in sorted(filenames):
            path = Path(dirpath) / filename
            if path.suffix.lower() in SKIP_SUFFIXES:
                continue
            yield path


def scan(root: Path) -> List[Tuple[str, Path, int, str]]:
    findings: List[Tuple[str, Path, int, str]] = []
    for path in iter_files(root):
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            if ALLOW.search(line):
                continue
            for name, pattern in PATTERNS:
                if pattern.search(line):
                    findings.append((name, path, lineno, line.strip()[:160]))
                    break
    return findings


def main(argv: List[str] = None) -> int:
    parser = argparse.ArgumentParser(description="scan a tree for credential-shaped strings")
    parser.add_argument("root", nargs="?", default=".", help="directory to scan (default: .)")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    if not root.exists():
        print("scan error: {0} does not exist".format(root), file=sys.stderr)
        return 2
    findings = scan(root)
    if not findings:
        if not args.quiet:
            print("secret scan: 0 findings in {0}".format(root.name))
        return 0
    for name, path, lineno, snippet in findings:
        print("{0}:{1}: {2}: {3}".format(path, lineno, name, snippet))
    print("secret scan: {0} finding(s)".format(len(findings)), file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
