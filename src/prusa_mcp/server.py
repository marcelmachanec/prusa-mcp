import json
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

import aiosqlite
from mcp.server.fastmcp import FastMCP

from prusa_mcp import db, slicer
from prusa_mcp.prusalink import PrusaLinkClient

PROJECT_DIR = Path(__file__).parent.parent.parent
DB_PATH = os.environ.get("PRUSA_MCP_DB_PATH", str(PROJECT_DIR / "data" / "prints.db"))


@dataclass
class AppContext:
    db: aiosqlite.Connection
    prusa: PrusaLinkClient | None


@asynccontextmanager
async def lifespan(server: FastMCP):
    # Ensure data directory exists
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)

    conn = await aiosqlite.connect(DB_PATH)
    await db.init_db(conn)

    prusa = None
    host = os.environ.get("PRUSA_HOST")
    api_key = os.environ.get("PRUSA_API_KEY")
    if host and api_key:
        prusa = PrusaLinkClient(host, api_key)

    try:
        yield AppContext(db=conn, prusa=prusa)
    finally:
        if prusa:
            await prusa.close()
        await conn.close()


mcp = FastMCP("prusa-3d-print-manager", lifespan=lifespan)


# =============================================================================
# Filament Tools
# =============================================================================


@mcp.tool()
async def add_filament(
    brand: str,
    material: str,
    color: str,
    weight_g: float = 1000,
    color_hex: str | None = None,
    price: float | None = None,
    currency: str = "CZK",
    purchased: str | None = None,
    notes: str | None = None,
) -> str:
    """Add a new filament spool to inventory.

    Args:
        brand: Manufacturer (e.g. "Prusament", "eSUN", "Polymaker")
        material: Filament type (e.g. "PLA", "PETG", "ASA", "TPU")
        color: Color name (e.g. "Galaxy Black", "Prusa Orange")
        weight_g: Spool weight in grams (default 1000)
        color_hex: Optional hex color code (e.g. "#1a1a2e")
        price: Purchase price
        currency: Currency code (default "CZK")
        purchased: Purchase date (ISO format, e.g. "2026-03-08")
        notes: Optional notes
    """
    ctx = mcp.get_context()
    app: AppContext = ctx.request_context.lifespan_context
    filament = await db.add_filament(
        app.db,
        brand=brand,
        material=material,
        color=color,
        weight_g=weight_g,
        color_hex=color_hex,
        price=price,
        currency=currency,
        purchased=purchased,
        notes=notes,
    )
    return json.dumps({"added": filament.to_dict()}, indent=2)


@mcp.tool()
async def update_filament(
    filament_id: int,
    brand: str | None = None,
    material: str | None = None,
    color: str | None = None,
    color_hex: str | None = None,
    weight_g: float | None = None,
    remaining_g: float | None = None,
    price: float | None = None,
    currency: str | None = None,
    purchased: str | None = None,
    opened: str | None = None,
    status: str | None = None,
    notes: str | None = None,
    clear_fields: list[str] | None = None,
) -> str:
    """Update fields on an existing filament spool.

    Args:
        filament_id: ID of the spool to update
        status: One of: sealed, active, low, empty, archived
        remaining_g: Estimated remaining weight in grams
        clear_fields: List of nullable fields to explicitly set to NULL
            (color_hex, price, purchased, opened, notes). Passing a field as None
            without clear_fields leaves it unchanged — use clear_fields to wipe a
            value.
        (all other filament fields can also be updated)
    """
    ctx = mcp.get_context()
    app: AppContext = ctx.request_context.lifespan_context
    filament = await db.update_filament(
        app.db,
        filament_id,
        clear_fields=clear_fields,
        brand=brand,
        material=material,
        color=color,
        color_hex=color_hex,
        weight_g=weight_g,
        remaining_g=remaining_g,
        price=price,
        currency=currency,
        purchased=purchased,
        opened=opened,
        status=status,
        notes=notes,
    )
    if not filament:
        return json.dumps({"error": f"Filament #{filament_id} not found"})
    return json.dumps({"updated": filament.to_dict()}, indent=2)


@mcp.tool()
async def use_filament(filament_id: int, grams_used: float) -> str:
    """Deduct grams from a filament spool after a print.

    Auto-updates status: 'active' (if sealed), 'low' (<15%), or 'empty'.

    Args:
        filament_id: ID of the spool
        grams_used: Grams consumed by the print
    """
    ctx = mcp.get_context()
    app: AppContext = ctx.request_context.lifespan_context
    filament = await db.use_filament(app.db, filament_id, grams_used)
    if not filament:
        return json.dumps({"error": f"Filament #{filament_id} not found"})
    return json.dumps({"updated": filament.to_dict()}, indent=2)


@mcp.tool()
async def weigh_spool(
    filament_id: int, gross_g: float, tare_g: float | None = None
) -> str:
    """Set a spool's remaining weight from a kitchen-scale weighing.

    remaining_g = gross_g - tare (clamped to 0..weight_g). Tare is the empty spool's
    weight: the tare_g argument (stored on the spool) -> the spool's stored tare ->
    the default_spool_tare_g setting. Use tare_g=0 for spoolless refills.

    Args:
        filament_id: ID of the spool
        gross_g: Measured weight of spool + filament in grams
        tare_g: Empty spool weight in grams (optional; remembered for next time)
    """
    ctx = mcp.get_context()
    app: AppContext = ctx.request_context.lifespan_context
    try:
        filament = await db.weigh_spool(app.db, filament_id, gross_g, tare_g)
    except ValueError as e:
        return json.dumps({"error": str(e)})
    if not filament:
        return json.dumps({"error": f"Filament #{filament_id} not found"})
    return json.dumps({"updated": filament.to_dict()}, indent=2)


@mcp.tool()
async def shopping_list() -> str:
    """What to buy: low/empty spools, plus stock and 90-day usage per material."""
    ctx = mcp.get_context()
    app: AppContext = ctx.request_context.lifespan_context
    return json.dumps(await db.shopping_list(app.db), indent=2)


@mcp.tool()
async def monthly_report(month: str | None = None) -> str:
    """Prints, hours, grams, cost and failure rate for one month (quotes excluded).

    Args:
        month: Month as YYYY-MM (default: current month)
    """
    ctx = mcp.get_context()
    app: AppContext = ctx.request_context.lifespan_context
    try:
        report = await db.monthly_report(app.db, month)
    except ValueError as e:
        return json.dumps({"error": str(e)})
    return json.dumps(report, indent=2)


@mcp.tool()
async def list_filaments(
    status: str | None = None,
    material: str | None = None,
    brand: str | None = None,
    color: str | None = None,
    include_archived: bool = False,
) -> str:
    """List filament spools with optional filters.

    Args:
        status: Filter by status (sealed/active/low/empty/archived).
        material: Filter by material type (PLA, PETG, ASA, etc.)
        brand: Filter by brand name (partial match)
        color: Filter by color name (partial match)
        include_archived: If True, include archived spools (default False)
    """
    ctx = mcp.get_context()
    app: AppContext = ctx.request_context.lifespan_context
    filaments = await db.list_filaments(
        app.db,
        status=status,
        material=material,
        brand=brand,
        color=color,
        include_archived=include_archived,
    )
    return json.dumps(
        {"filaments": [f.to_dict() for f in filaments], "count": len(filaments)},
        indent=2,
    )


@mcp.tool()
async def get_filament(filament_id: int) -> str:
    """Get details of a single filament spool.

    Args:
        filament_id: ID of the spool
    """
    ctx = mcp.get_context()
    app: AppContext = ctx.request_context.lifespan_context
    filament = await db.get_filament(app.db, filament_id)
    if not filament:
        return json.dumps({"error": f"Filament #{filament_id} not found"})
    return json.dumps({"filament": filament.to_dict()}, indent=2)


@mcp.tool()
async def delete_filament(filament_id: int) -> str:
    """Permanently delete a filament spool with no print history.
    If the spool has print log entries, returns an error and suggests archiving.

    Args:
        filament_id: ID of the spool to delete
    """
    ctx = mcp.get_context()
    app: AppContext = ctx.request_context.lifespan_context
    result = await db.delete_filament(app.db, filament_id)
    return json.dumps(result, indent=2)


@mcp.tool()
async def add_filaments_bulk(filaments: list[dict]) -> str:
    """Add multiple filament spools in one call.
    Each dict accepts: brand (required), material (required), color (required),
    weight_g, color_hex, price, currency, purchased, notes.

    Args:
        filaments: List of filament dicts to add
    """
    ctx = mcp.get_context()
    app: AppContext = ctx.request_context.lifespan_context
    added, errors = [], []
    for i, f in enumerate(filaments):
        try:
            filament = await db.add_filament(
                app.db,
                brand=f["brand"],
                material=f["material"],
                color=f["color"],
                weight_g=f.get("weight_g", 1000),
                color_hex=f.get("color_hex"),
                price=f.get("price"),
                currency=f.get("currency", "CZK"),
                purchased=f.get("purchased"),
                notes=f.get("notes"),
            )
            added.append(filament.to_dict())
        except (KeyError, ValueError) as e:
            errors.append({"index": i, "error": str(e), "input": f})
    result = {"added": added, "count": len(added)}
    if errors:
        result["errors"] = errors
    return json.dumps(result, indent=2)


# =============================================================================
# Print Log Tools
# =============================================================================


@mcp.tool()
async def log_print(
    filename: str,
    status: str = "completed",
    started_at: str | None = None,
    finished_at: str | None = None,
    duration_s: int | None = None,
    filament_used_g: float | None = None,
    filament_used_m: float | None = None,
    filament_id: int | None = None,
    notes: str | None = None,
) -> str:
    """Record a completed or failed print job.

    If filament_id and filament_used_g are both provided, the spool's remaining weight
    is automatically deducted.

    Args:
        filename: G-code filename (e.g. "benchy.gcode")
        status: Print result — completed, failed, or cancelled
        started_at: Start time (ISO format). Defaults to now.
        finished_at: End time (ISO format)
        duration_s: Total print time in seconds
        filament_used_g: Grams of filament consumed
        filament_used_m: Meters of filament consumed
        filament_id: ID of the spool used (for auto-deduction)
        notes: Optional notes about the print
    """
    ctx = mcp.get_context()
    app: AppContext = ctx.request_context.lifespan_context
    if not started_at:
        started_at = datetime.now().isoformat()
    job = await db.log_print(
        app.db,
        filename=filename,
        started_at=started_at,
        finished_at=finished_at,
        duration_s=duration_s,
        filament_used_g=filament_used_g,
        filament_used_m=filament_used_m,
        filament_id=filament_id,
        status=status,
        notes=notes,
    )
    return json.dumps({"logged": job.to_dict()}, indent=2)


@mcp.tool()
async def list_prints(
    limit: int = 20,
    status: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> str:
    """List print history with optional filters.

    Args:
        limit: Max results (default 20)
        status: Filter by status (completed, failed, cancelled)
        date_from: Start date filter (ISO format)
        date_to: End date filter (ISO format)
    """
    ctx = mcp.get_context()
    app: AppContext = ctx.request_context.lifespan_context
    prints = await db.list_prints(
        app.db, limit=limit, status=status, date_from=date_from, date_to=date_to
    )
    return json.dumps(
        {"prints": [p.to_dict() for p in prints], "count": len(prints)},
        indent=2,
    )


@mcp.tool()
async def list_prints_by_spool(filament_id: int, limit: int = 20) -> str:
    """List all prints that used a specific filament spool.

    Args:
        filament_id: ID of the spool to query
        limit: Max results (default 20)
    """
    ctx = mcp.get_context()
    app: AppContext = ctx.request_context.lifespan_context
    prints = await db.list_prints(app.db, filament_id=filament_id, limit=limit)
    return json.dumps(
        {"prints": [p.to_dict() for p in prints], "count": len(prints)},
        indent=2,
    )


@mcp.tool()
async def get_daily_stats(
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int = 7,
) -> str:
    """Get aggregated daily print statistics.

    Returns per-day totals: print count, success rate, total time, filament consumed.

    Args:
        date_from: Start date filter (ISO format)
        date_to: End date filter (ISO format)
        limit: Number of days to return (default 7)
    """
    ctx = mcp.get_context()
    app: AppContext = ctx.request_context.lifespan_context
    stats = await db.get_daily_stats(
        app.db, date_from=date_from, date_to=date_to, limit=limit
    )
    return json.dumps({"daily_stats": stats, "days": len(stats)}, indent=2)


# =============================================================================
# Cost Calculator Tools
# =============================================================================


@mcp.tool()
async def quote_print(
    weight_g: float,
    print_time_h: float,
    filament_id: int | None = None,
    material: str | None = None,
    prep_level: str = "simple",
    post_level: str = "simple",
    markup_pct: float | None = None,
    quantity: int = 1,
) -> str:
    """Calculate a cost estimate for quoting a print job.

    Args:
        weight_g: Estimated filament weight in grams.
        print_time_h: Estimated print time in hours.
        filament_id: Specific spool ID from inventory (for exact pricing).
        material: Material type for average pricing (PLA, PETG, ABS, etc.).
        prep_level: Preparation complexity (none/simple/moderate/complex).
        post_level: Post-processing complexity (none/simple/moderate/complex).
        markup_pct: Override default markup percentage.
        quantity: Number of prints (default 1).
    """
    ctx = mcp.get_context()
    app: AppContext = ctx.request_context.lifespan_context
    result = await db.calculate_cost(
        app.db,
        weight_g=weight_g,
        print_time_h=print_time_h,
        filament_id=filament_id,
        material=material,
        prep_level=prep_level,
        post_level=post_level,
        markup_pct=markup_pct,
    )
    if "error" in result:
        return json.dumps({"error": result["error"]}, indent=2)

    if quantity > 1:
        for key in [
            "filament_cost",
            "electricity_cost",
            "depreciation_cost",
            "prep_cost",
            "post_cost",
            "consumables_cost",
            "subtotal",
            "with_failures",
            "suggested_price",
        ]:
            result[key] = round(result[key] * quantity, 2)
        result["quantity"] = quantity

    return json.dumps({"quote": result}, indent=2)


@mcp.tool()
async def calc_print_cost(
    filament_id: int,
    weight_g: float,
    print_time_h: float,
    filename: str = "unnamed",
    prep_min: int | None = None,
    post_min: int | None = None,
    prep_level: str = "simple",
    post_level: str = "simple",
    status: str = "completed",
    quoted_price: float | None = None,
    notes: str | None = None,
) -> str:
    """Calculate actual print cost, log the print, and deduct filament.

    Args:
        filament_id: Spool ID used for the print.
        weight_g: Actual filament weight used in grams.
        print_time_h: Actual print time in hours.
        filename: Print job name/filename.
        prep_min: Actual preparation time in minutes (overrides preset).
        post_min: Actual post-processing time in minutes (overrides preset).
        prep_level: Preset level if prep_min not given (none/simple/moderate/complex).
        post_level: Preset level if post_min not given (none/simple/moderate/complex).
        status: Print status (completed/failed).
        quoted_price: Price quoted to customer (for margin tracking).
        notes: Optional notes.
    """
    ctx = mcp.get_context()
    app: AppContext = ctx.request_context.lifespan_context

    cost = await db.calculate_cost(
        app.db,
        weight_g=weight_g,
        print_time_h=print_time_h,
        filament_id=filament_id,
        prep_min=prep_min,
        post_min=post_min,
        prep_level=prep_level,
        post_level=post_level,
    )
    if "error" in cost:
        return json.dumps({"error": cost["error"]}, indent=2)

    duration_s = int(print_time_h * 3600)
    started_at = datetime.now().isoformat()

    job = await db.log_print(
        app.db,
        filename=filename,
        started_at=started_at,
        duration_s=duration_s,
        filament_used_g=weight_g,
        filament_id=filament_id,
        status=status,
        notes=notes,
        cost_breakdown_json=json.dumps(cost),
        quoted_price=quoted_price,
        is_quote=0,
    )

    result = {
        "logged": job.to_dict(),
        "cost_breakdown": cost,
    }
    if quoted_price:
        result["margin"] = round(quoted_price - cost["subtotal"], 2)

    return json.dumps(result, indent=2)


@mcp.tool()
async def get_settings() -> str:
    """View all calculator settings (energy price, labor rate, markup, etc.)."""
    ctx = mcp.get_context()
    app: AppContext = ctx.request_context.lifespan_context
    settings = await db.get_all_settings_detailed(app.db)
    return json.dumps({"settings": settings}, indent=2)


@mcp.tool()
async def update_setting(key: str, value: float) -> str:
    """Update a calculator setting.

    Args:
        key: Setting key (energy_czk_kwh, labor_czk_h, failure_rate_pct,
             default_markup_pct, printer_depreciation_czk_h,
             printer_energy_kwh_h, default_consumables_czk).
        value: New numeric value.
    """
    ctx = mcp.get_context()
    app: AppContext = ctx.request_context.lifespan_context
    existing = await db.get_setting(app.db, key)
    if existing is None:
        return json.dumps({"error": f"Unknown setting: {key}"}, indent=2)
    await db.set_setting(app.db, key, value)
    return json.dumps({"updated": {key: value}}, indent=2)


# =============================================================================
# PrusaLink Tools
# =============================================================================


@mcp.tool()
async def get_printer_status() -> str:
    """Get current Prusa printer status — temperatures, state, speed, fan info.

    Requires PRUSA_HOST and PRUSA_API_KEY environment variables.
    """
    ctx = mcp.get_context()
    app: AppContext = ctx.request_context.lifespan_context
    if not app.prusa:
        return json.dumps(
            {"error": "PrusaLink not configured. Set PRUSA_HOST and PRUSA_API_KEY."}
        )
    try:
        data = await app.prusa.get_printer()
        return json.dumps({"printer": data}, indent=2)
    except Exception as e:
        return json.dumps({"error": f"PrusaLink error: {e}"})


@mcp.tool()
async def get_current_job() -> str:
    """Get the currently active print job — progress, time remaining, filename.

    Requires PRUSA_HOST and PRUSA_API_KEY environment variables.
    """
    ctx = mcp.get_context()
    app: AppContext = ctx.request_context.lifespan_context
    if not app.prusa:
        return json.dumps(
            {"error": "PrusaLink not configured. Set PRUSA_HOST and PRUSA_API_KEY."}
        )
    try:
        data = await app.prusa.get_job()
        return json.dumps({"job": data}, indent=2)
    except Exception as e:
        return json.dumps({"error": f"PrusaLink error: {e}"})


@mcp.tool()
async def check_printer_online() -> str:
    """Quick check if the Prusa printer is reachable on the network.

    Requires PRUSA_HOST and PRUSA_API_KEY environment variables.
    """
    ctx = mcp.get_context()
    app: AppContext = ctx.request_context.lifespan_context
    if not app.prusa:
        return json.dumps(
            {"error": "PrusaLink not configured. Set PRUSA_HOST and PRUSA_API_KEY."}
        )
    online = await app.prusa.is_online()
    return json.dumps({"online": online, "host": os.environ.get("PRUSA_HOST")})


@mcp.tool()
async def slice_model(
    model_path: str,
    printer_profile: str,
    print_profile: str,
    material_profile: str,
    output_dir: str | None = None,
) -> str:
    """Slice an STL/OBJ with PrusaSlicer and report time, grams and cost.

    Profile arguments are names of presets installed in PrusaSlicer. Writes an
    ASCII .gcode next to the model unless output_dir is given. Does not upload
    and never starts a print. Executable: env PRUSA_SLICER_PATH.

    Args:
        model_path: Path to the model file (.stl or .obj; .3mf is refused).
        printer_profile: Printer preset name.
        print_profile: Print (quality) preset name.
        material_profile: Filament preset name.
        output_dir: Directory for the G-code (default: the model's directory).
    """
    ctx = mcp.get_context()
    app: AppContext = ctx.request_context.lifespan_context
    try:
        result = await slicer.slice_model(
            model_path,
            printer_profile,
            print_profile,
            material_profile,
            output_dir,
            db=app.db,
        )
    except (slicer.SlicerError, OSError) as e:  # file errors -> error JSON too
        return json.dumps({"error": str(e)})
    return json.dumps({"slice": result}, indent=2)


@mcp.tool()
async def upload_gcode(gcode_path: str, overwrite: bool = False) -> str:
    """Upload a .gcode/.bgcode file to the printer's USB storage.

    Upload only: the print is NEVER started, press start on the printer.
    Requires PRUSA_HOST and PRUSA_API_KEY environment variables.

    Args:
        gcode_path: Path to the G-code file.
        overwrite: Replace a file with the same name if it exists.
    """
    ctx = mcp.get_context()
    app: AppContext = ctx.request_context.lifespan_context
    if not app.prusa:
        return json.dumps(
            {"error": "PrusaLink not configured. Set PRUSA_HOST and PRUSA_API_KEY."}
        )
    try:
        result = await slicer.upload_gcode_file(app.prusa, gcode_path, overwrite)
    except (slicer.SlicerError, OSError) as e:  # file errors -> error JSON too
        return json.dumps({"error": str(e)})
    return json.dumps({"upload": result}, indent=2)


# =============================================================================
# MCP Resources
# =============================================================================


@mcp.resource("print3d://filament/inventory")
async def filament_inventory() -> str:
    """Current filament inventory — all non-archived spools."""
    ctx = mcp.get_context()
    app: AppContext = ctx.request_context.lifespan_context
    filaments = await db.list_filaments(app.db)
    return json.dumps(
        {"filaments": [f.to_dict() for f in filaments], "count": len(filaments)},
        indent=2,
    )


@mcp.resource("print3d://filament/{filament_id}")
async def filament_detail(filament_id: int) -> str:
    """Details of a specific filament spool."""
    ctx = mcp.get_context()
    app: AppContext = ctx.request_context.lifespan_context
    filament = await db.get_filament(app.db, filament_id)
    if not filament:
        return json.dumps({"error": f"Filament #{filament_id} not found"})
    return json.dumps({"filament": filament.to_dict()}, indent=2)


@mcp.resource("print3d://stats/today")
async def stats_today() -> str:
    """Today's print statistics."""
    ctx = mcp.get_context()
    app: AppContext = ctx.request_context.lifespan_context
    today = date.today().isoformat()
    stats = await db.get_daily_stats(app.db, date_from=today, date_to=today, limit=1)
    if stats:
        return json.dumps({"today": stats[0]}, indent=2)
    return json.dumps({"today": "No prints recorded today."})


@mcp.resource("print3d://stats/week")
async def stats_week() -> str:
    """This week's aggregated print statistics."""
    ctx = mcp.get_context()
    app: AppContext = ctx.request_context.lifespan_context
    stats = await db.get_daily_stats(app.db, limit=7)
    return json.dumps({"week": stats, "days": len(stats)}, indent=2)


@mcp.resource("print3d://printer/status")
async def printer_status_resource() -> str:
    """Current Prusa printer status snapshot."""
    ctx = mcp.get_context()
    app: AppContext = ctx.request_context.lifespan_context
    if not app.prusa:
        return json.dumps({"error": "PrusaLink not configured."})
    try:
        data = await app.prusa.get_printer()
        return json.dumps({"printer": data}, indent=2)
    except Exception as e:
        return json.dumps({"error": f"PrusaLink error: {e}"})


# =============================================================================
# Entry point
# =============================================================================


def main():
    mcp.run(transport="stdio")
