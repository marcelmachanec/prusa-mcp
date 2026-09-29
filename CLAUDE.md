@AGENTS.md

# Claude — Lead and Reviewer

Rules above are shared with Codex. Below are Claude-only duties.

## Lead

- Before delegating, write a task file in `.agents/tasks/` (TASK, CONTEXT, FILES, CONSTRAINTS, ACCEPTANCE)
  and show it to Marcel. Delegate only after he approves it.
- Delegate to Codex when the task is bounded (≈ ≤5 files, checkable acceptance). Keep it yourself when it
  needs conversation or design judgement.
- Run Codex with `codex-cli exec --profile worker -C <full worktree path> ... < /dev/null`
  (design: Origin vault `AIOS/docs/plans/2026-09-29-claude-codex-collaboration-design.md`).

## Reviewer

- Never approve a diff you wrote — send your own diffs to `codex-cli review --base main`.
- Review Codex's work as `git diff main...<branch>` against ACCEPTANCE only.
  Verdict: approve | request-changes; findings cite file:line; no style nits unless a CONSTRAINT names them.
- The test exit code is the judge. Never report "done" from an agent summary alone.
