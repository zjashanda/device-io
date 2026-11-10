#!/usr/bin/env bash
# Full gate for device-io: unit tests, PowerShell parse check, secret scan,
# and a fixture smoke test that proves the CLI contract still works.
# Usage: tools/run-all.sh
set -uo pipefail

cd "$(dirname "$0")/.." || exit 1
ROOT="$PWD"
FAIL=0

step() { printf '\n== %s\n' "$1"; }
fail() { printf 'FAIL: %s\n' "$1"; FAIL=1; }

step "python unittest"
if python3 -m unittest discover -s tests -v; then
  echo "PASS: unittest"
else
  fail "unittest"
fi

step "package import"
if python3 -c "import sys; sys.path.insert(0,'.'); import device_io; print('device_io', device_io.__version__)"; then
  echo "PASS: import"
else
  fail "import"
fi

step "CLI contract"
if python3 -m device_io.cli schema --list >/dev/null; then
  echo "PASS: device-io schema --list"
else
  fail "cli schema"
fi
if python3 -m device_io.cli doctor --format json >/dev/null; then
  echo "PASS: device-io doctor"
else
  fail "cli doctor"
fi

step "fixture smoke test"
shopt -s nullglob
FIXTURES=(fixtures/*.json)
if [ ${#FIXTURES[@]} -eq 0 ]; then
  echo "SKIP: no fixtures yet"
else
  for fixture in "${FIXTURES[@]}"; do
    if python3 -m device_io.cli ports --backend fixture --fixture "$fixture" --format json >/dev/null; then
      echo "PASS: fixture $(basename "$fixture")"
    else
      fail "fixture $(basename "$fixture")"
    fi
  done
fi

step "powershell parse check"
if command -v pwsh >/dev/null 2>&1; then
  PW_FOUND=0
  for script in windows/*.ps1; do
    [ -e "$script" ] || continue
    PW_FOUND=1
    if pwsh -NoProfile -Command "[void][System.Management.Automation.Language.Parser]::ParseFile('$ROOT/$script',[ref]\$null,[ref]\$null)" >/dev/null 2>&1; then
      echo "PASS: parse $script"
    else
      fail "parse $script"
    fi
  done
  [ "$PW_FOUND" -eq 1 ] || echo "SKIP: no PowerShell scripts"
else
  echo "SKIP: pwsh not installed"
fi

step "secret scan"
if python3 tools/secret_scan.py "$ROOT"; then
  echo "PASS: secret scan"
else
  fail "secret scan"
fi

printf '\n'
if [ "$FAIL" -eq 0 ]; then
  echo "ALL CHECKS PASSED"
else
  echo "CHECKS FAILED"
fi
exit "$FAIL"
