import sqlite3

import aiosqlite
import pytest

import prusa_mcp.db as db_module
from prusa_mcp.db import _apply_migrations, init_db


@pytest.mark.asyncio
async def test_duplicate_column_migration_is_recorded(monkeypatch):
    async with aiosqlite.connect(":memory:") as db:
        await db.executescript(
            """
            CREATE TABLE schema_version (version INTEGER PRIMARY KEY);
            CREATE TABLE example (existing TEXT);
            """
        )
        monkeypatch.setattr(
            db_module,
            "MIGRATIONS",
            {1: ["ALTER TABLE example ADD COLUMN existing TEXT"]},
        )

        await _apply_migrations(db)

        cursor = await db.execute("SELECT version FROM schema_version")
        assert await cursor.fetchall() == [(1,)]


@pytest.mark.asyncio
async def test_failed_migration_is_not_recorded(monkeypatch):
    async with aiosqlite.connect(":memory:") as db:
        await db.execute("CREATE TABLE schema_version (version INTEGER PRIMARY KEY)")
        monkeypatch.setattr(
            db_module,
            "MIGRATIONS",
            {1: ["ALTER TABLE no_such_table ADD COLUMN x TEXT"]},
        )

        with pytest.raises(sqlite3.OperationalError, match="no such table"):
            await _apply_migrations(db)

        cursor = await db.execute("SELECT version FROM schema_version")
        assert await cursor.fetchall() == []


@pytest.mark.asyncio
async def test_init_db_is_idempotent():
    async with aiosqlite.connect(":memory:") as db:
        await init_db(db)
        changes_after_first_init = db.total_changes

        await init_db(db)

        assert db.total_changes == changes_after_first_init
        cursor = await db.execute("SELECT version FROM schema_version ORDER BY version")
        assert await cursor.fetchall() == [(1,), (2,), (3,)]


@pytest.mark.asyncio
async def test_migration_3_on_fresh_db_adds_tare_column():
    async with aiosqlite.connect(":memory:") as db:
        await init_db(db)
        cursor = await db.execute("PRAGMA table_info(filament)")
        assert "tare_g" in [row[1] for row in await cursor.fetchall()]


@pytest.mark.asyncio
async def test_migration_3_applies_to_version_2_db():
    async with aiosqlite.connect(":memory:") as db:
        await db.executescript(
            """
            CREATE TABLE filament (id INTEGER PRIMARY KEY, brand TEXT);
            INSERT INTO filament (brand) VALUES ('Old');
            CREATE TABLE schema_version (version INTEGER PRIMARY KEY);
            INSERT INTO schema_version (version) VALUES (1), (2);
            """
        )

        await _apply_migrations(db)

        cursor = await db.execute("SELECT brand, tare_g FROM filament")
        assert await cursor.fetchall() == [("Old", None)]
        cursor = await db.execute("SELECT MAX(version) FROM schema_version")
        assert await cursor.fetchone() == (3,)
