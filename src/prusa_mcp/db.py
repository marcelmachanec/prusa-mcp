import json
import math
import sqlite3
from datetime import date, datetime, timedelta

import aiosqlite

from prusa_mcp.models import (
    VALID_FILAMENT_STATUSES,
    VALID_PRINT_STATUSES,
    Filament,
    PrintJob,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS filament (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    brand       TEXT NOT NULL,
    material    TEXT NOT NULL,
    color       TEXT NOT NULL,
    color_hex   TEXT,
    weight_g    REAL NOT NULL DEFAULT 1000,
    remaining_g REAL NOT NULL DEFAULT 1000,
    price       REAL,
    currency    TEXT NOT NULL DEFAULT 'CZK',
    purchased   TEXT,
    opened      TEXT,
    status      TEXT NOT NULL DEFAULT 'sealed',
    notes       TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS print_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    filename        TEXT NOT NULL,
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    duration_s      INTEGER,
    filament_used_g REAL,
    filament_used_m REAL,
    filament_id     INTEGER REFERENCES filament(id),
    status          TEXT NOT NULL DEFAULT 'completed',
    notes           TEXT,
    cost_breakdown_json TEXT,
    quoted_price    REAL,
    is_quote        INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value REAL NOT NULL,
    unit TEXT
);

CREATE TABLE IF NOT EXISTS prep_presets (
    level TEXT PRIMARY KEY,
    prep_min INTEGER NOT NULL,
    post_min INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS schema_version (
    version     INTEGER PRIMARY KEY,
    applied_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


DEFAULTS_PRESETS = {
    "none": (5, 3),
    "simple": (15, 10),
    "moderate": (22, 38),
    "complex": (40, 60),
}


DEFAULTS_SETTINGS = {
    "energy_czk_kwh": (3.6, "CZK/kWh"),
    "labor_czk_h": (250, "CZK/h"),
    "failure_rate_pct": (10, "%"),
    "default_markup_pct": (33, "%"),
    "printer_depreciation_czk_h": (0.52, "CZK/h"),
    "printer_energy_kwh_h": (0.15, "kWh/h"),
    "default_consumables_czk": (1, "CZK"),
    "default_spool_tare_g": (200, "g"),
}

# A spool below this fraction of its nominal weight counts as "low".
LOW_THRESHOLD = 0.15


# Maps schema version → SQL statements to apply in order.
# Add future migrations here as new version keys — never modify existing ones.
MIGRATIONS: dict[int, list[str]] = {
    2: [
        "ALTER TABLE print_log ADD COLUMN cost_breakdown_json TEXT",
        "ALTER TABLE print_log ADD COLUMN quoted_price REAL",
        "ALTER TABLE print_log ADD COLUMN is_quote INTEGER NOT NULL DEFAULT 0",
    ],
    3: [
        "ALTER TABLE filament ADD COLUMN tare_g REAL",
    ],
    4: [
        "DROP VIEW IF EXISTS daily_stats",
        """CREATE VIEW daily_stats AS
        SELECT
            date(started_at) AS date,
            COUNT(*) AS total_prints,
            SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) AS successful,
            SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed,
            ROUND(
                SUM(CASE WHEN status = 'completed' THEN 1.0 ELSE 0.0 END)
                / COUNT(*) * 100, 1
            ) AS success_rate,
            SUM(duration_s) AS total_time_s,
            ROUND(SUM(filament_used_g), 1) AS total_filament_g,
            COUNT(DISTINCT filament_id) AS spools_used
        FROM print_log
        WHERE is_quote = 0
        GROUP BY date(started_at)
        ORDER BY date DESC;""",
    ],
}


# Development databases at version 4 may have either the filtered view or the
# watcher column. Migration 5 reinstalls the canonical view in both layouts.
MIGRATIONS[5] = MIGRATIONS[4].copy()


async def _apply_migrations(db: aiosqlite.Connection) -> None:
    """Apply pending schema migrations in version order."""
    try:
        # Serialize the version check with the schema changes it controls.
        await db.execute("BEGIN IMMEDIATE")
        cursor = await db.execute(
            "SELECT COALESCE(MAX(version), 0) FROM schema_version"
        )
        row = await cursor.fetchone()
        current_version = row[0]

        for version in sorted(MIGRATIONS):
            if version <= current_version:
                continue
            if version == 5:
                columns = await db.execute_fetchall("PRAGMA table_info(print_log)")
                if not any(column[1] == "printer_job_id" for column in columns):
                    await db.execute(
                        "ALTER TABLE print_log ADD COLUMN printer_job_id INTEGER"
                    )
            for sql in MIGRATIONS[version]:
                try:
                    await db.execute(sql)
                except sqlite3.OperationalError as exc:
                    if "duplicate column name" not in str(exc):
                        raise
            if version == 5:
                await db.execute("SELECT printer_job_id FROM print_log LIMIT 0")
                await db.execute("SELECT * FROM daily_stats LIMIT 0")
            await db.execute(
                "INSERT OR IGNORE INTO schema_version (version) VALUES (?)", (version,)
            )
        await db.commit()
    except BaseException:
        await db.rollback()
        raise


async def init_db(db: aiosqlite.Connection) -> None:
    await db.execute("PRAGMA foreign_keys = ON")
    await db.executescript(SCHEMA)
    await db.execute("INSERT OR IGNORE INTO schema_version (version) VALUES (?)", (1,))
    await db.commit()
    await _apply_migrations(db)
    for key, (value, unit) in DEFAULTS_SETTINGS.items():
        await db.execute(
            "INSERT OR IGNORE INTO settings (key, value, unit) VALUES (?, ?, ?)",
            (key, value, unit),
        )
    for level, (prep, post) in DEFAULTS_PRESETS.items():
        await db.execute(
            "INSERT OR IGNORE INTO prep_presets "
            "(level, prep_min, post_min) VALUES (?, ?, ?)",
            (level, prep, post),
        )
    await db.commit()


# --- Preset CRUD ---


async def get_preset(db: aiosqlite.Connection, level: str) -> dict | None:
    cursor = await db.execute(
        "SELECT level, prep_min, post_min FROM prep_presets WHERE level = ?",
        (level,),
    )
    row = await cursor.fetchone()
    if not row:
        return None
    return {"level": row[0], "prep_min": row[1], "post_min": row[2]}


# --- Settings CRUD ---


async def get_setting(db: aiosqlite.Connection, key: str) -> float | None:
    cursor = await db.execute("SELECT value FROM settings WHERE key = ?", (key,))
    row = await cursor.fetchone()
    return row[0] if row else None


async def set_setting(db: aiosqlite.Connection, key: str, value: float) -> None:
    await db.execute(
        "UPDATE settings SET value = ? WHERE key = ?",
        (value, key),
    )
    await db.commit()


async def get_all_settings(db: aiosqlite.Connection) -> dict[str, float]:
    cursor = await db.execute("SELECT key, value, unit FROM settings")
    rows = await cursor.fetchall()
    return {row[0]: row[1] for row in rows}


async def get_all_settings_detailed(
    db: aiosqlite.Connection,
) -> dict[str, dict[str, float | str | None]]:
    cursor = await db.execute("SELECT key, value, unit FROM settings")
    rows = await cursor.fetchall()
    return {row[0]: {"value": row[1], "unit": row[2]} for row in rows}


# --- Filament CRUD ---


async def add_filament(
    db: aiosqlite.Connection,
    *,
    brand: str,
    material: str,
    color: str,
    weight_g: float = 1000,
    color_hex: str | None = None,
    price: float | None = None,
    currency: str = "CZK",
    purchased: str | None = None,
    notes: str | None = None,
) -> Filament:
    cursor = await db.execute(
        """INSERT INTO filament
           (brand, material, color, color_hex, weight_g,
            remaining_g, price, currency, purchased, notes)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            brand,
            material,
            color,
            color_hex,
            weight_g,
            weight_g,
            price,
            currency,
            purchased,
            notes,
        ),
    )
    await db.commit()
    row_id = cursor.lastrowid
    if row_id is None:
        raise RuntimeError("INSERT returned no lastrowid — filament was not created")
    return await get_filament(db, row_id)


async def get_filament(db: aiosqlite.Connection, filament_id: int) -> Filament | None:
    db.row_factory = aiosqlite.Row
    cursor = await db.execute("SELECT * FROM filament WHERE id = ?", (filament_id,))
    row = await cursor.fetchone()
    if not row:
        return None
    return Filament(**dict(row))


async def list_filaments(
    db: aiosqlite.Connection,
    *,
    status: str | None = None,
    material: str | None = None,
    brand: str | None = None,
    color: str | None = None,
    include_archived: bool = False,
) -> list[Filament]:
    query = "SELECT * FROM filament WHERE 1=1"
    params: list = []

    if status:
        query += " AND status = ?"
        params.append(status)
    elif not include_archived:
        query += " AND status != 'archived'"

    if material:
        query += " AND UPPER(material) = UPPER(?)"
        params.append(material)

    if brand:
        query += " AND UPPER(brand) LIKE UPPER(?)"
        params.append(f"%{brand}%")

    if color:
        query += " AND UPPER(color) LIKE UPPER(?)"
        params.append(f"%{color}%")

    query += " ORDER BY id"

    db.row_factory = aiosqlite.Row
    cursor = await db.execute(query, params)
    rows = await cursor.fetchall()
    return [Filament(**dict(row)) for row in rows]


async def update_filament(
    db: aiosqlite.Connection,
    filament_id: int,
    *,
    clear_fields: list[str] | None = None,
    **fields,
) -> Filament | None:
    allowed = {
        "brand",
        "material",
        "color",
        "color_hex",
        "weight_g",
        "remaining_g",
        "price",
        "currency",
        "purchased",
        "opened",
        "status",
        "notes",
    }
    if (
        "status" in fields
        and fields["status"] is not None
        and fields["status"] not in VALID_FILAMENT_STATUSES
    ):
        raise ValueError(
            f"Invalid filament status '{fields['status']}'. "
            f"Must be one of: {sorted(VALID_FILAMENT_STATUSES)}"
        )

    if "remaining_g" in fields and fields["remaining_g"] is not None:
        current = await get_filament(db, filament_id)
        if current and current.weight_g > 0:
            fields["remaining_g"] = min(fields["remaining_g"], current.weight_g)

    updates = {k: v for k, v in fields.items() if k in allowed and v is not None}
    if clear_fields:
        clearable = {"color_hex", "price", "purchased", "opened", "notes"}
        for field in clear_fields:
            if field in clearable:
                updates[field] = None
    if not updates:
        return await get_filament(db, filament_id)

    updates["updated_at"] = "datetime('now')"
    set_clauses = []
    params = []
    for key, value in updates.items():
        if key == "updated_at":
            set_clauses.append("updated_at = datetime('now')")
        else:
            set_clauses.append(f"{key} = ?")
            params.append(value)

    params.append(filament_id)
    await db.execute(
        f"UPDATE filament SET {', '.join(set_clauses)} WHERE id = ?",
        params,
    )
    await db.commit()
    return await get_filament(db, filament_id)


def _status_for(remaining_g: float, weight_g: float, current: str) -> str:
    """Spool status rule for weigh-in (also applied by the deduction SQL).

    0 g -> empty; < LOW_THRESHOLD of weight_g -> low; sealed -> active;
    a previously low/empty spool that is back above the threshold -> active.
    An archived spool stays archived: only an explicit update brings it back.
    """
    if current == "archived":
        return "archived"
    if remaining_g <= 0:
        return "empty"
    if remaining_g < weight_g * LOW_THRESHOLD:
        return "low"
    if current in ("sealed", "low", "empty"):
        return "active"
    return current


async def _deduct_filament_no_commit(
    db: aiosqlite.Connection, filament_id: int, grams_used: float
) -> None:
    """UPDATE filament weight/status — no commit. Caller must commit."""
    if grams_used < 0:
        raise ValueError("grams_used must be >= 0")
    if grams_used == 0:
        return

    await db.execute(
        """UPDATE filament
           SET (remaining_g, status) = (
               SELECT new_remaining,
                      CASE
                          WHEN status = 'archived' THEN 'archived'
                          WHEN new_remaining <= 0 THEN 'empty'
                          WHEN new_remaining < weight_g * :low_threshold THEN 'low'
                          WHEN status IN ('sealed', 'low', 'empty') THEN 'active'
                          ELSE status
                      END
               FROM (
                   SELECT MAX(0, COALESCE(remaining_g - :grams_used, 0))
                          AS new_remaining
               )
           ),
           opened = COALESCE(NULLIF(opened, ''), :today),
           updated_at = datetime('now')
           WHERE id = :filament_id""",
        {
            "grams_used": grams_used,
            "low_threshold": LOW_THRESHOLD,
            "today": date.today().isoformat(),
            "filament_id": filament_id,
        },
    )


async def use_filament(
    db: aiosqlite.Connection, filament_id: int, grams_used: float
) -> Filament | None:
    if grams_used != 0:
        await _deduct_filament_no_commit(db, filament_id, grams_used)
        await db.commit()
    return await get_filament(db, filament_id)


async def weigh_spool(
    db: aiosqlite.Connection,
    filament_id: int,
    gross_g: float,
    tare_g: float | None = None,
) -> Filament | None:
    """Set remaining_g from a kitchen-scale weighing (gross = spool + filament).

    Tare precedence: tare_g argument (stored on the spool) -> stored tare_g ->
    default_spool_tare_g setting.
    """
    if gross_g < 0:
        raise ValueError("gross_g must be >= 0")
    if tare_g is not None and tare_g < 0:
        raise ValueError("tare_g must be >= 0")

    filament = await get_filament(db, filament_id)
    if not filament:
        return None

    if tare_g is not None:
        tare = tare_g
    elif filament.tare_g is not None:
        tare = filament.tare_g
    else:
        default = await get_setting(db, "default_spool_tare_g")
        tare = (
            default
            if default is not None
            else DEFAULTS_SETTINGS["default_spool_tare_g"][0]
        )

    remaining = min(max(gross_g - tare, 0), filament.weight_g)
    if filament.status == "sealed" and gross_g >= filament.weight_g + tare:
        new_status = "sealed"  # still full and unopened
        opened = filament.opened
    else:
        new_status = _status_for(remaining, filament.weight_g, filament.status)
        opened = filament.opened or date.today().isoformat()

    await db.execute(
        """UPDATE filament
           SET remaining_g = ?, status = ?, opened = ?, tare_g = ?,
               updated_at = datetime('now')
           WHERE id = ?""",
        (
            remaining,
            new_status,
            opened,
            tare_g if tare_g is not None else filament.tare_g,
            filament_id,
        ),
    )
    await db.commit()
    return await get_filament(db, filament_id)


async def shopping_list(db: aiosqlite.Connection) -> dict:
    """Low/empty spools to rebuy, plus stock and 90-day usage per material."""
    spools = await list_filaments(db)  # archived excluded
    to_buy = [f.to_dict() for f in spools if f.status in ("low", "empty")]

    stock: dict[str, dict] = {}
    for f in spools:
        entry = stock.setdefault(f.material, {"spools": 0, "remaining_g": 0.0})
        entry["spools"] += 1
        entry["remaining_g"] += f.remaining_g

    since = (date.today() - timedelta(days=90)).isoformat()
    cursor = await db.execute(
        """SELECT f.material, SUM(p.filament_used_g)
           FROM print_log p JOIN filament f ON f.id = p.filament_id
           WHERE p.is_quote = 0 AND p.filament_used_g IS NOT NULL
             AND date(p.started_at) >= ?
           GROUP BY f.material""",
        (since,),
    )
    used = {row[0]: row[1] for row in await cursor.fetchall()}
    return {"to_buy": to_buy, "stock_by_material": stock, "used_last_90d_g": used}


async def monthly_report(db: aiosqlite.Connection, month: str | None = None) -> dict:
    """Totals for one calendar month (YYYY-MM, default current). Quotes excluded.

    cost = sum of `subtotal` from cost_breakdown_json where present, else None.
    """
    if month is None:
        month = date.today().strftime("%Y-%m")
    try:
        # Normalise "2026-1" to "2026-01" so it matches stored ISO dates.
        month = datetime.strptime(month, "%Y-%m").strftime("%Y-%m")
    except ValueError:
        raise ValueError(f"month must be YYYY-MM, got '{month}'") from None

    cursor = await db.execute(
        """SELECT p.status, p.duration_s, p.filament_used_g,
                  p.cost_breakdown_json, f.material
           FROM print_log p LEFT JOIN filament f ON f.id = p.filament_id
           WHERE p.is_quote = 0 AND substr(p.started_at, 1, 7) = ?""",
        (month,),
    )
    rows = await cursor.fetchall()

    counts = {"completed": 0, "failed": 0, "cancelled": 0}
    seconds = 0
    grams = 0.0
    cost: float | None = None
    by_material: dict[str, dict] = {}
    for status, duration_s, used_g, breakdown, material in rows:
        counts[status] = counts.get(status, 0) + 1
        seconds += duration_s or 0
        grams += used_g or 0
        if breakdown:
            try:
                subtotal = json.loads(breakdown).get("subtotal")
            except (ValueError, AttributeError):
                subtotal = None
            if isinstance(subtotal, int | float):
                cost = (cost or 0) + subtotal
        if material is not None:
            entry = by_material.setdefault(material, {"prints": 0, "filament_g": 0.0})
            entry["prints"] += 1
            entry["filament_g"] = round(entry["filament_g"] + (used_g or 0), 1)

    total = len(rows)
    return {
        "month": month,
        "prints": total,
        "completed": counts["completed"],
        "failed": counts["failed"],
        "cancelled": counts["cancelled"],
        "failure_rate_pct": round(counts["failed"] / total * 100, 1) if total else 0,
        "print_hours": round(seconds / 3600, 2),
        "filament_g": round(grams, 1),
        "cost": round(cost, 2) if cost is not None else None,
        "by_material": by_material,
    }


# --- Print Log CRUD ---


async def log_print(
    db: aiosqlite.Connection,
    *,
    filename: str,
    started_at: str,
    finished_at: str | None = None,
    duration_s: int | None = None,
    filament_used_g: float | None = None,
    filament_used_m: float | None = None,
    filament_id: int | None = None,
    status: str = "completed",
    notes: str | None = None,
    cost_breakdown_json: str | None = None,
    quoted_price: float | None = None,
    is_quote: int = 0,
    printer_job_id: int | None = None,
) -> PrintJob:
    if status not in VALID_PRINT_STATUSES:
        raise ValueError(
            f"Invalid print status '{status}'. "
            f"Must be one of: {sorted(VALID_PRINT_STATUSES)}"
        )
    if filament_used_g is not None and filament_used_g < 0:
        raise ValueError("filament_used_g must be >= 0")

    try:
        cursor = await db.execute(
            """INSERT INTO print_log
               (filename, started_at, finished_at, duration_s,
                filament_used_g, filament_used_m,
                filament_id, status, notes,
                cost_breakdown_json, quoted_price, is_quote, printer_job_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                filename,
                started_at,
                finished_at,
                duration_s,
                filament_used_g,
                filament_used_m,
                filament_id,
                status,
                notes,
                cost_breakdown_json,
                quoted_price,
                is_quote,
                printer_job_id,
            ),
        )
        if not is_quote and filament_id is not None and filament_used_g:
            await _deduct_filament_no_commit(db, filament_id, filament_used_g)

        job = await get_print(db, cursor.lastrowid)
        await db.commit()
        return job
    except BaseException:
        await db.rollback()
        raise


async def get_print(db: aiosqlite.Connection, print_id: int) -> PrintJob | None:
    db.row_factory = aiosqlite.Row
    cursor = await db.execute("SELECT * FROM print_log WHERE id = ?", (print_id,))
    row = await cursor.fetchone()
    if not row:
        return None
    return PrintJob(**dict(row))


async def list_unresolved_prints(
    db: aiosqlite.Connection, limit: int
) -> list[PrintJob]:
    if limit < 0:
        raise ValueError("limit must be nonnegative")
    db.row_factory = aiosqlite.Row
    async with db.execute(
        """SELECT * FROM print_log
           WHERE printer_job_id IS NOT NULL AND is_quote = 0
             AND (filament_id IS NULL OR filament_used_g IS NULL)
           ORDER BY started_at DESC, id DESC LIMIT ?""",
        (limit,),
    ) as cursor:
        return [PrintJob(**dict(row)) for row in await cursor.fetchall()]


async def resolve_print_usage(
    db: aiosqlite.Connection,
    print_id: int,
    filament_id: int | None,
    filament_used_g: float | None,
) -> PrintJob:
    """Fill missing watcher fields and charge stock on the first complete pair."""
    if filament_id is None and filament_used_g is None:
        raise ValueError("Supply a filament_id or filament_used_g")
    if filament_used_g is not None and (
        not math.isfinite(filament_used_g) or filament_used_g < 0
    ):
        raise ValueError("filament_used_g must be finite and nonnegative")

    try:
        await db.execute("BEGIN IMMEDIATE")
        job = await get_print(db, print_id)
        if job is None or job.printer_job_id is None or job.is_quote:
            raise ValueError(f"Print #{print_id} is not a watcher print")
        if filament_id is not None:
            if job.filament_id is not None and job.filament_id != filament_id:
                raise ValueError("The print already has a different spool")
            if await get_filament(db, filament_id) is None:
                raise ValueError(f"Filament #{filament_id} not found")
        if (
            filament_used_g is not None
            and job.filament_used_g is not None
            and job.filament_used_g != filament_used_g
        ):
            raise ValueError("The print already has a different filament usage")

        spool = job.filament_id if filament_id is None else filament_id
        grams = job.filament_used_g if filament_used_g is None else filament_used_g
        if (spool, grams) != (job.filament_id, job.filament_used_g):
            await db.execute(
                """UPDATE print_log SET filament_id = ?, filament_used_g = ?
                   WHERE id = ?""",
                (spool, grams, print_id),
            )
            if spool is not None and grams is not None:
                await _deduct_filament_no_commit(db, spool, grams)
            job.filament_id = spool
            job.filament_used_g = grams
        await db.commit()
        return job
    except BaseException:
        await db.rollback()
        raise


async def list_prints(
    db: aiosqlite.Connection,
    *,
    limit: int = 20,
    status: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    filament_id: int | None = None,
) -> list[PrintJob]:
    query = "SELECT * FROM print_log WHERE 1=1"
    params: list = []

    if status:
        query += " AND status = ?"
        params.append(status)
    if filament_id is not None:
        query += " AND filament_id = ?"
        params.append(filament_id)
    if date_from:
        query += " AND started_at >= ?"
        params.append(date_from)
    if date_to:
        query += " AND started_at <= ?"
        params.append(date_to + "T23:59:59")

    query += " ORDER BY started_at DESC LIMIT ?"
    params.append(limit)

    db.row_factory = aiosqlite.Row
    cursor = await db.execute(query, params)
    rows = await cursor.fetchall()
    return [PrintJob(**dict(row)) for row in rows]


async def get_daily_stats(
    db: aiosqlite.Connection,
    *,
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int = 7,
) -> list[dict]:
    query = "SELECT * FROM daily_stats WHERE 1=1"
    params: list = []

    if date_from:
        query += " AND date >= ?"
        params.append(date_from)
    if date_to:
        query += " AND date <= ?"
        params.append(date_to)

    query += " LIMIT ?"
    params.append(limit)

    db.row_factory = aiosqlite.Row
    cursor = await db.execute(query, params)
    rows = await cursor.fetchall()

    stats = []
    for row in rows:
        row_dict = dict(row)
        total_s = row_dict.get("total_time_s") or 0
        hours, remainder = divmod(total_s, 3600)
        minutes, _ = divmod(remainder, 60)
        row_dict["total_time_formatted"] = (
            f"{hours}h {minutes}m" if hours else f"{minutes}m"
        )
        stats.append(row_dict)
    return stats


async def delete_filament(db: aiosqlite.Connection, filament_id: int) -> dict:
    filament = await get_filament(db, filament_id)
    if not filament:
        return {"error": f"Filament #{filament_id} not found"}
    cursor = await db.execute(
        "SELECT COUNT(*) FROM print_log WHERE filament_id = ?", (filament_id,)
    )
    row = await cursor.fetchone()
    print_count = row[0]
    if print_count > 0:
        return {
            "error": f"Cannot delete filament #{filament_id} — it has {print_count} "
            "print log entries. Archive it instead: update_filament(status='archived')."
        }
    await db.execute("DELETE FROM filament WHERE id = ?", (filament_id,))
    await db.commit()
    return {"deleted": filament_id, "brand": filament.brand, "color": filament.color}


# --- Cost Calculation ---


def _is_czk(currency: str) -> bool:
    return currency.strip().upper() == "CZK"


async def calculate_cost(
    db: aiosqlite.Connection,
    *,
    weight_g: float,
    print_time_h: float,
    filament_id: int | None = None,
    material: str | None = None,
    prep_level: str = "simple",
    post_level: str = "simple",
    prep_min: int | None = None,
    post_min: int | None = None,
    markup_pct: float | None = None,
) -> dict:
    settings = await get_all_settings(db)
    warning = None

    # Resolve filament cost per kg
    if filament_id is not None:
        filament = await get_filament(db, filament_id)
        if not filament:
            return {"error": f"Filament {filament_id} not found"}
        if not _is_czk(filament.currency):
            return {
                "error": f"Filament {filament_id} uses {filament.currency}; "
                "cost estimates require CZK spool prices"
            }
        if filament.weight_g == 0:
            return {
                "error": "Filament weight_g cannot be zero — cannot compute cost per kg"
            }
        price_per_kg = (filament.price or 0) / (filament.weight_g / 1000)
        if filament.price is None:
            warning = "filament price not set — filament_cost is 0"
    elif material:
        cursor = await db.execute(
            "SELECT price / (weight_g / 1000.0), currency FROM filament "
            "WHERE UPPER(material) = UPPER(?) AND price IS NOT NULL",
            (material,),
        )
        prices_per_kg = [
            price
            for price, currency in await cursor.fetchall()
            if price is not None and _is_czk(currency)
        ]
        if prices_per_kg:
            price_per_kg = sum(prices_per_kg) / len(prices_per_kg)
        else:
            price_per_kg = 0
            warning = "No CZK spool price for this material; filament_cost is 0"
    else:
        return {"error": "Provide filament_id or material"}

    # Resolve prep/post times from presets or overrides
    if prep_min is None:
        preset = await get_preset(db, prep_level)
        prep_min = preset["prep_min"] if preset else 15
    if post_min is None:
        preset = await get_preset(db, post_level)
        post_min = preset["post_min"] if preset else 10

    if markup_pct is None:
        markup_pct = settings["default_markup_pct"]

    # Calculate cost components
    filament_cost = (weight_g / 1000) * price_per_kg
    electricity_cost = (
        settings["energy_czk_kwh"] * settings["printer_energy_kwh_h"] * print_time_h
    )
    depreciation_cost = settings["printer_depreciation_czk_h"] * print_time_h
    prep_cost = (prep_min / 60) * settings["labor_czk_h"]
    post_cost = (post_min / 60) * settings["labor_czk_h"]
    consumables_cost = settings["default_consumables_czk"]

    subtotal = (
        filament_cost
        + electricity_cost
        + depreciation_cost
        + prep_cost
        + post_cost
        + consumables_cost
    )
    with_failures = subtotal * (1 + settings["failure_rate_pct"] / 100)
    suggested_price = with_failures * (1 + markup_pct / 100)

    result = {
        "filament_cost": round(filament_cost, 2),
        "electricity_cost": round(electricity_cost, 2),
        "depreciation_cost": round(depreciation_cost, 2),
        "prep_cost": round(prep_cost, 2),
        "prep_min": prep_min,
        "post_cost": round(post_cost, 2),
        "post_min": post_min,
        "consumables_cost": round(consumables_cost, 2),
        "subtotal": round(subtotal, 2),
        "failure_rate_pct": settings["failure_rate_pct"],
        "with_failures": round(with_failures, 2),
        "markup_pct": markup_pct,
        "suggested_price": round(suggested_price, 2),
        "currency": "CZK",
    }

    if warning is not None:
        result["warning"] = warning

    return result
