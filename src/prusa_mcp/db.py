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
"""


async def init_db(db: aiosqlite.Connection) -> None:
    await db.executescript(SCHEMA)
    await db.commit()


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
) -> PrintJob:
    cursor = await db.execute(
        """INSERT INTO print_log
           (filename, started_at, finished_at, duration_s,
            filament_used_g, filament_used_m,
            filament_id, status, notes)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
