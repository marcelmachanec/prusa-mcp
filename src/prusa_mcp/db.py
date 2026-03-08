import aiosqlite

from prusa_mcp.models import Filament, PrintJob

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

CREATE VIEW IF NOT EXISTS daily_stats AS
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
GROUP BY date(started_at)
ORDER BY date DESC;

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
}


async def init_db(db: aiosqlite.Connection) -> None:
    await db.executescript(SCHEMA)
    # Migrate print_log if needed (existing DBs without new columns)
    for col, typedef in [
        ("cost_breakdown_json", "TEXT"),
        ("quoted_price", "REAL"),
        ("is_quote", "INTEGER NOT NULL DEFAULT 0"),
    ]:
        try:
            await db.execute(f"ALTER TABLE print_log ADD COLUMN {col} {typedef}")
        except Exception:
            pass  # Column already exists
    for key, (value, unit) in DEFAULTS_SETTINGS.items():
        await db.execute(
            "INSERT OR IGNORE INTO settings (key, value, unit) VALUES (?, ?, ?)",
            (key, value, unit),
        )
    for level, (prep, post) in DEFAULTS_PRESETS.items():
        await db.execute(
            "INSERT OR IGNORE INTO prep_presets (level, prep_min, post_min) VALUES (?, ?, ?)",
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
    return await get_filament(db, cursor.lastrowid)


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
) -> list[Filament]:
    query = "SELECT * FROM filament WHERE 1=1"
    params: list = []

    if status:
        query += " AND status = ?"
        params.append(status)
    else:
        query += " AND status != 'archived'"

    if material:
        query += " AND UPPER(material) = UPPER(?)"
        params.append(material)

    if brand:
        query += " AND UPPER(brand) LIKE UPPER(?)"
        params.append(f"%{brand}%")

    query += " ORDER BY id"

    db.row_factory = aiosqlite.Row
    cursor = await db.execute(query, params)
    rows = await cursor.fetchall()
    return [Filament(**dict(row)) for row in rows]


async def update_filament(
    db: aiosqlite.Connection,
    filament_id: int,
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
    updates = {k: v for k, v in fields.items() if k in allowed and v is not None}
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


async def use_filament(
    db: aiosqlite.Connection, filament_id: int, grams_used: float
) -> Filament | None:
    filament = await get_filament(db, filament_id)
    if not filament:
        return None

    new_remaining = max(0, filament.remaining_g - grams_used)
    new_status = filament.status
    if new_remaining == 0:
        new_status = "empty"
    elif new_remaining < filament.weight_g * 0.15:
        new_status = "low"
    elif filament.status == "sealed":
        new_status = "active"

    return await update_filament(
        db,
        filament_id,
        remaining_g=new_remaining,
        status=new_status,
        opened=filament.opened or "date('now')",
    )


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
) -> PrintJob:
    cursor = await db.execute(
        """INSERT INTO print_log
           (filename, started_at, finished_at, duration_s,
            filament_used_g, filament_used_m,
            filament_id, status, notes,
            cost_breakdown_json, quoted_price, is_quote)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
        ),
    )
    await db.commit()

    # Auto-deduct filament if both filament_id and grams used are provided
    if filament_id and filament_used_g:
        await use_filament(db, filament_id, filament_used_g)

    return await get_print(db, cursor.lastrowid)


async def get_print(db: aiosqlite.Connection, print_id: int) -> PrintJob | None:
    db.row_factory = aiosqlite.Row
    cursor = await db.execute("SELECT * FROM print_log WHERE id = ?", (print_id,))
    row = await cursor.fetchone()
    if not row:
        return None
    return PrintJob(**dict(row))


async def list_prints(
    db: aiosqlite.Connection,
    *,
    limit: int = 20,
    status: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> list[PrintJob]:
    query = "SELECT * FROM print_log WHERE 1=1"
    params: list = []

    if status:
        query += " AND status = ?"
        params.append(status)
    if date_from:
        query += " AND started_at >= ?"
        params.append(date_from)
    if date_to:
        query += " AND started_at <= ?"
        params.append(date_to)

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


# --- Cost Calculation ---


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

    # Resolve filament cost per kg
    if filament_id:
        filament = await get_filament(db, filament_id)
        if not filament:
            return {"error": f"Filament {filament_id} not found"}
        price_per_kg = (filament.price or 0) / (filament.weight_g / 1000)
    elif material:
        cursor = await db.execute(
            "SELECT AVG(price / (weight_g / 1000.0)) FROM filament "
            "WHERE UPPER(material) = UPPER(?) AND price IS NOT NULL",
            (material,),
        )
        row = await cursor.fetchone()
        price_per_kg = row[0] if row and row[0] else 0
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

    return {
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
