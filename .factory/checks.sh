#!/usr/bin/env bash
# Factory gate for prusa-mcp. Exit 0 = pass. Run from anywhere inside the repo or a worktree.
# Override the interpreter with FACTORY_PYTHON (worktrees have no venv of their own).
set -u
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root" || exit 2
py="${FACTORY_PYTHON:-C:/Users/MarcelMachanec/Projects/prusa-mcp/.venv/Scripts/python.exe}"

fail=0
echo "== ruff check"
ruff check . || fail=1
echo "== pytest"
PYTHONPATH=src "$py" -m pytest -q || fail=1

if [ "$fail" -eq 0 ]; then echo "GATE: PASS"; else echo "GATE: FAIL"; fi
exit "$fail"
