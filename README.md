# prusa-mcp

MCP server for 3D-print management: filament inventory, print log, cost quotes,
and live printer status via PrusaLink. Python ≥3.13, SQLite (aiosqlite).

## Setup

```bash
uv sync --group dev
```

Register it with your MCP client (e.g. `.mcp.json` for Claude Code):

```json
{
  "mcpServers": {
    "prusa-3d": {
      "command": "uv",
      "args": ["run", "--directory", "<path-to-repo>", "prusa-mcp"],
      "env": {
        "PRUSA_HOST": "<printer-ip>",
        "PRUSA_API_KEY": "<prusalink-api-key>"
      }
    }
  }
}
```

| Variable | Required | Purpose |
|---|---|---|
| `PRUSA_HOST` | for printer tools | PrusaLink address of the printer |
| `PRUSA_API_KEY` | for printer tools | PrusaLink API key |
| `PRUSA_MCP_DB_PATH` | no | SQLite path (default `data/prints.db`) |
| `PRUSA_SLICER_PATH` | no | `prusa-slicer-console.exe` (default: `C:/Program Files/Prusa3D/PrusaSlicer/`) |

Inventory and reporting work offline. Set the printer host and API key for
live status, uploads, and the standalone print watcher.

## Tools

- **Filament:** `add_filament`, `add_filaments_bulk`, `update_filament`,
  `use_filament`, `list_filaments`, `get_filament`, `delete_filament`
- **Prints:** `log_print`, `list_prints`, `list_prints_by_spool`, `get_daily_stats`,
  `list_unresolved_prints`, `resolve_print_usage`
- **Spool truth:** `weigh_spool` (scale weight minus empty-spool tare, default 200 g),
  `shopping_list`, `monthly_report`
- **Cost:** `quote_print`, `calc_print_cost`, `get_settings`, `update_setting`
- **Printer:** `get_printer_status`, `get_current_job`, `check_printer_online`
- **Slice & send:** `slice_model` (PrusaSlicer CLI, ASCII G-code, time/grams/cost),
  `upload_gcode` (to printer USB; never starts a print)

Resources: `print3d://filament/inventory`, `print3d://filament/{filament_id}`,
`print3d://stats/today`, `print3d://stats/week`, `print3d://printer/status`.

## Run the print watcher on Windows

`prusa-watch` polls the configured printer every 30 seconds independently of
the MCP client. From the repo root, use a PowerShell session with the printer
environment variables already set. For a pilot, create an isolated directory:

```powershell
uv sync --group dev
$pilotDir = Join-Path $env:TEMP ("prusa-pilot-" + [guid]::NewGuid())
New-Item -ItemType Directory -Path $pilotDir | Out-Null
$env:PRUSA_MCP_DB_PATH = Join-Path $pilotDir "pilot.sqlite"
$watchLog = Join-Path $pilotDir "watch.log"
$pilotDir
uv run prusa-watch 2>> $watchLog
```

Configure the pilot MCP client with the same absolute database path and restart
its server. Stop the watcher with Ctrl+C. To resume, reuse the same database
and log paths and rerun the final command. The log contains watcher events and
poll errors. A print that starts and ends while the watcher is down can be missed.

1. In the pilot inventory, add the loaded spool and set its status to
   `active`. Keep one active or low spool of that material for the automatic
   assignment check. Record its starting balance and the slicer's grams estimate.
2. Start a normal print at the MK4 and keep the watcher running through
   completion. Record the displayed filename, end state, elapsed time, and
   any available scale readings with the spool tare.
3. Compare `list_prints`, `get_filament`, and `monthly_report` for the print's
   start month. Expect one row and one applicable deduction. Label slicer grams
   as planned and watcher grams by their source. A G-code estimate repeated in
   the history is the same measurement. Note discrepancies and manual actions.
4. For the ambiguous case, use two active pilot spools of the same material
   on a subsequent observed job, or use the automated ambiguous-spool fixture.
   Find the entry with `list_unresolved_prints`. Call `resolve_print_usage`
   with its print ID and the actual spool ID, supplying grams if missing.
   Repeating the same correction should leave the balance unchanged.
5. For a missed print, check history first, then use `log_print` once with
   the known details. Record an interrupted physical job when one occurs
   naturally. Share the pilot counts, discrepancies, and interventions for
   the [roadmap review](ROADMAP.md).

## Development

```bash
PYTHONPATH=src .venv/Scripts/python.exe -m pytest -q
ruff check . && ruff format .
```

Layout: `server.py` (MCP tools, thin) → `db.py` (schema, migrations, queries),
`prusalink.py` (HTTP client), `models.py` (dataclasses). Agent rules: `AGENTS.md`.
