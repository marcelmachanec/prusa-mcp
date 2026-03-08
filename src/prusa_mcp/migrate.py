"""One-time migration to add cost calculator tables to existing DB."""

import asyncio
from pathlib import Path

import aiosqlite

DB_PATH = Path(__file__).parent.parent.parent / "data" / "prints.db"


async def migrate():
    db = await aiosqlite.connect(DB_PATH)

    # Check if columns already exist
    cursor = await db.execute("PRAGMA table_info(print_log)")
    columns = {row[1] for row in await cursor.fetchall()}

    if "cost_breakdown_json" not in columns:
        await db.execute("ALTER TABLE print_log ADD COLUMN cost_breakdown_json TEXT")
        await db.execute("ALTER TABLE print_log ADD COLUMN quoted_price REAL")
        await db.execute(
            "ALTER TABLE print_log ADD COLUMN is_quote INTEGER NOT NULL DEFAULT 0"
        )
        await db.commit()
        print("Added cost columns to print_log")
    else:
        print("print_log already has cost columns")

    # New tables are handled by init_db (CREATE IF NOT EXISTS + INSERT OR IGNORE)
    from prusa_mcp.db import init_db

    await init_db(db)
    print("Settings and presets tables initialized")

    await db.close()
    print("Migration complete")


if __name__ == "__main__":
    asyncio.run(migrate())
