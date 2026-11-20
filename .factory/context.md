# prusa-mcp: factory brief

Read `AGENTS.md` first. It owns the stack, commands, conventions and the "Never" list.
This file adds only what the factory stages need on top.

## Goal of the pilot

Make prusa-mcp practical for daily printing: fewer manual steps, clearer tool output,
safer failure when the printer is offline. Small, testable changes beat big rewrites.

## For intake

- Accept: bug fixes, tool usability, tests, docs (README is empty), error handling.
- Reject or split: anything that needs a new dependency (ask first), changes MCP tool
  names/signatures without saying so, or touches `data/`.
- Real printer access is not available to the factory. Anything touching `prusalink.py`
  must be testable with a mocked HTTP client.

## For spec + builder

- Gate = `.factory/checks.sh` (ruff check + pytest). Every behaviour change gets a test.
- Schema changes go through `_apply_migrations` in `db.py`, with a migration test.

## For reviewers

- Security paths: `prusalink.py` (API key, HTTP), migrations, dependency files.
- The API key must never appear in logs, errors, tool output or tests.
