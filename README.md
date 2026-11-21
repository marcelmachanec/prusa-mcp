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

Filament and print-log tools work without the printer; only the three printer
tools need `PRUSA_HOST` / `PRUSA_API_KEY`. Give the printer a DHCP reservation
so its IP doesn't change.

## Tools

- **Filament:** `add_filament`, `add_filaments_bulk`, `update_filament`,
  `use_filament`, `list_filaments`, `get_filament`, `delete_filament`
- **Prints:** `log_print`, `list_prints`, `list_prints_by_spool`, `get_daily_stats`
- **Cost:** `quote_print`, `calc_print_cost`, `get_settings`, `update_setting`
- **Printer:** `get_printer_status`, `get_current_job`, `check_printer_online`

Resources: `print3d://filament/inventory`, `print3d://filament/{filament_id}`,
`print3d://stats/today`, `print3d://stats/week`, `print3d://printer/status`.

## Development

```bash
PYTHONPATH=src .venv/Scripts/python.exe -m pytest -q
ruff check . && ruff format .
```

Layout: `server.py` (MCP tools, thin) → `db.py` (schema, migrations, queries),
`prusalink.py` (HTTP client), `models.py` (dataclasses). Agent rules: `AGENTS.md`.
