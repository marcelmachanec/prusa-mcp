# prusa-mcp — Agent Rules

Shared rules for every agent working in this repo (Codex reads this file; Claude imports it from CLAUDE.md).
This file is the single owner of these rules — don't copy them elsewhere.

## What this is

MCP server for 3D-print management with PrusaLink integration. Python ≥3.13, SQLite via aiosqlite.

- `src/prusa_mcp/server.py` — MCP tool definitions (thin; delegates to db/prusalink)
- `src/prusa_mcp/db.py` — schema, `_apply_migrations`, filament/print-log queries
- `src/prusa_mcp/prusalink.py` — PrusaLink HTTP client
- `src/prusa_mcp/models.py` — dataclasses/constants
- `tests/` — pytest + pytest-asyncio

## Commands (run from the repo or worktree root)

- Tests: `PYTHONPATH=src C:/Users/MarcelMachanec/Projects/prusa-mcp/.venv/Scripts/python.exe -m pytest -q`
  - `PYTHONPATH=src` is required: the venv is editable-linked to the main checkout, so without it
    a worktree silently tests the main checkout's code.
  - Worktrees have no venv of their own; always use this absolute path (PC-specific — revisit when the Mini runs tasks).
- Lint: `ruff check .` · Format: `ruff format .` (config in `ruff.toml`)

## Conventions

- Ruff defaults from `ruff.toml` (88 chars, double quotes). Type hints on non-obvious signatures.
- No new dependencies without asking. Keep MCP tool names and signatures stable unless the task says otherwise.
- Every behaviour change gets a test.
- Conventional commits (`feat:`, `fix:`, `refactor:`, `test:`, `chore:`).

## Never

- Read, print, or copy `.mcp.json` or `.env` (they hold `PRUSA_API_KEY`).
- Touch `data/prints.db` (real print history).
- Commit, push, or change git config — the human or the harness commits.

## Executor contract (headless runs on a task file in `.agents/tasks/`)

1. Read the task file. Work only on paths listed under FILES. Needing another file = stop and report it as an open question.
2. Don't guess requirements. Ambiguity goes under open questions with status `blocked`.
3. Run the test command before finishing; fix failures you caused. Run `ruff check` on changed files.
4. Final message: JSON matching `.agents/result.schema.json`. No prose around it. In `tests`, give
   pass/fail/skip counts per check, and mark any check that was skipped or not run "unverified".
