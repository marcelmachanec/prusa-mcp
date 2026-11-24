# prusa-mcp roadmap

## Goal

For a single Prusa MK4, an operator can slice a model, upload it, start it at the printer, and later see what happened and how much filament remains. Routine jobs should require no manual print-log entry. Ambiguous jobs should be easy to correct.

## Starting point · 2026-11-23

The main checkout already covers inventory, spool weighing, shopping lists, print history, cost quotes, daily and monthly reporting, printer status, slicing, and upload. Local changes address concurrent writes, quote reporting, and currency handling. The separate `claude/print-watcher` branch implements automatic logging and contains further uncommitted work. Its notes record idle-printer and G-code observations. A live active-to-finished transition still needs verification.

## Implementation and pilot status

The working checkout now includes the accounting changes, schema migration 5, the standalone watcher, and the two reconciliation tools. Automated tests cover competing writers, rollback, replay after restart, both development layouts of schema version 4, and correction retries. A local MCP client exercised discovery, correction, retry, and the monthly report over stdio.

The next acceptance step is the [operator-led pilot](README.md#run-the-print-watcher-on-windows). This implementation has **zero live pilot observations**. Miss rates, inventory drift, and the frequency of manual interventions are still unknown. Record the first print's outcome here before selecting the next improvement.

The five executor packets cover [atomic writes](.agents/tasks/T-2026-11-23-01.md), [quote and currency handling](.agents/tasks/T-2026-11-23-02.md), [watcher integration](.agents/tasks/T-2026-11-23-03.md), [reconciliation and pilot](.agents/tasks/T-2026-11-23-04.md), and [pilot review](.agents/tasks/T-2026-11-23-05.md).

## 1. Establish a trustworthy accounting baseline

- Review the in-progress atomic deduction and print-log changes first. Track the reporting and currency changes as a separate reviewable unit.
- Verify that two 100 g deductions from a 1,000 g spool leave 800 g, that a failed print-log transaction leaves both history and stock untouched, and that subsequent writes succeed.
- Verify that a stored quote contributes zero prints and grams to activity totals and that CZK estimates use compatible prices.

**Done when** these checks pass against temporary SQLite databases, including concurrent writes from separate connections and upgrades from an older schema.

## 2. Integrate the existing print watcher

- Reconcile both implementations of migration 4. The main working tree uses it to update daily statistics. The watcher uses it to add the printer job ID. Test fresh databases, version 3, and both development versions of schema 4. Preserve existing print rows and verify the resulting schema and view.
- Bring the watcher's pending fixes and recorded printer observations into review. Replay completion, cancellation, failure, disconnect, repeated polls, and restart against the integrated database code.
- Preserve the manual start at the printer. Record uncertain outcomes and usage estimates explicitly. Leave the spool unassigned when the evidence cannot identify one.

**Done when** replaying one observed job across a watcher restart produces one history row and at most one stock deduction, including while the MCP server is writing.

## 3. Pilot one full print and make exceptions recoverable

- Run the watcher against a disposable database during a normal, operator-started print. Compare the recorded filename, end state, duration, and grams with available printer and slicer evidence. Verify an interrupted job when one is available.
- Make it easy to find entries with unresolved spool or usage and correct one. Updating the history row and applying the stock difference must be one transaction. Repeating the correction must leave the balance unchanged.
- Document watcher startup, restart, log location, and how to recover from a missed observation using the supported host.

**Done when** an ordinary observed print reaches the monthly report without manual entry and an ambiguous print can be reconciled without hand-editing SQLite.

## 4. Improve the workflow using pilot evidence

Compare estimates with observed outcomes where the units and source are known. Use scale readings to check aggregate spool drift. Select the next improvement from the recurring problem the pilot exposes, such as profile selection, missed jobs, or unresolved spool assignments. Revisit camera or multi-printer support when there is a concrete use case for it.
