import asyncio
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
        await db.executescript(
            """
            CREATE TABLE schema_version (version INTEGER PRIMARY KEY);
            CREATE VIEW daily_stats AS SELECT 42 AS total_prints;
            """
        )
        monkeypatch.setattr(
            db_module,
            "MIGRATIONS",
            {
                1: [
                    "DROP VIEW daily_stats",
                    "ALTER TABLE no_such_table ADD COLUMN x TEXT",
                ]
            },
        )

        with pytest.raises(sqlite3.OperationalError, match="no such table"):
            await _apply_migrations(db)

        cursor = await db.execute("SELECT version FROM schema_version")
        assert await cursor.fetchall() == []
        assert await db.execute_fetchall("SELECT * FROM daily_stats") == [(42,)]
        assert not db.in_transaction


@pytest.mark.asyncio
async def test_init_db_is_idempotent():
    async with aiosqlite.connect(":memory:") as db:
        await init_db(db)
        changes_after_first_init = db.total_changes

        await init_db(db)

        assert db.total_changes == changes_after_first_init
        cursor = await db.execute("SELECT version FROM schema_version ORDER BY version")
        assert await cursor.fetchall() == [(1,), (2,), (3,), (4,), (5,)]


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

        await init_db(db)

        cursor = await db.execute("SELECT brand, tare_g FROM filament")
        assert await cursor.fetchall() == [("Old", None)]
        cursor = await db.execute("SELECT MAX(version) FROM schema_version")
        assert await cursor.fetchone() == (5,)


@pytest.mark.asyncio
@pytest.mark.parametrize("layout", ["fresh", "v3", "main4", "watcher4"])
async def test_watcher_migration_converges(layout):
    async with aiosqlite.connect(":memory:") as conn:
        if layout != "fresh":
            await conn.executescript(db_module.SCHEMA)
            await conn.executescript(
                """ALTER TABLE filament ADD COLUMN tare_g REAL;
                INSERT INTO schema_version(version) VALUES (1), (2), (3);
                INSERT INTO print_log(filename, started_at, is_quote)
                VALUES ('part', '2026-11-23', 0), ('quote', '2026-11-23', 1);"""
            )
            if layout in ("main4", "watcher4"):
                await conn.execute("INSERT INTO schema_version(version) VALUES (4)")
            if layout == "main4":
                for sql in db_module.MIGRATIONS[4]:
                    await conn.execute(sql)
            if layout == "watcher4":
                await conn.execute(
                    "ALTER TABLE print_log ADD COLUMN printer_job_id INTEGER"
                )
                await conn.execute(
                    "UPDATE print_log SET printer_job_id = 7 WHERE filename = 'part'"
                )
                await conn.execute("CREATE VIEW daily_stats AS SELECT * FROM print_log")
            await conn.commit()
        await init_db(conn)
        await init_db(conn)
        assert await conn.execute_fetchall(
            "SELECT max(version) FROM schema_version"
        ) == [(5,)]
        rows = await conn.execute_fetchall(
            "SELECT filename, printer_job_id FROM print_log ORDER BY id"
        )
        expected = (
            []
            if layout == "fresh"
            else [("part", 7 if layout == "watcher4" else None), ("quote", None)]
        )
        assert rows == expected
        assert await conn.execute_fetchall("SELECT total_prints FROM daily_stats") == (
            [] if layout == "fresh" else [(1,)]
        )


@pytest.mark.asyncio
async def test_migration_4_excludes_existing_quotes_from_daily_stats(
    tmp_path, monkeypatch
):
    path = tmp_path / "migration.db"
    async with aiosqlite.connect(path) as db, aiosqlite.connect(path) as second:
        await db.executescript(
            """
            CREATE TABLE schema_version (version INTEGER PRIMARY KEY);
            INSERT INTO schema_version VALUES (1), (2), (3);
            CREATE TABLE print_log (
                started_at TEXT, status TEXT, duration_s INTEGER,
                filament_used_g REAL, filament_id INTEGER, is_quote INTEGER
            );
            INSERT INTO print_log VALUES
                ('2026-11-01', 'completed', 60, 20, 1, 0),
                ('2026-11-01', 'failed', 120, 50, 2, 1),
                ('2026-11-02', 'completed', 180, 60, 2, 1);
            CREATE VIEW daily_stats AS SELECT * FROM print_log;
            """
        )
        view_dropped = asyncio.Event()
        reader_finished = asyncio.Event()
        execute = db.execute

        async def pause_after_drop(sql, *args):
            cursor = await execute(sql, *args)
            if sql == "DROP VIEW IF EXISTS daily_stats":
                view_dropped.set()
                await reader_finished.wait()
            return cursor

        async def read_and_migrate():
            await view_dropped.wait()
            try:
                assert await second.execute_fetchall(
                    "SELECT COUNT(*) FROM daily_stats"
                ) == [(3,)]
            finally:
                reader_finished.set()
            await _apply_migrations(second)

        monkeypatch.setattr(db, "execute", pause_after_drop)
        async with asyncio.timeout(10), asyncio.TaskGroup() as tasks:
            tasks.create_task(_apply_migrations(db))
            tasks.create_task(read_and_migrate())

        stats = await db_module.get_daily_stats(db)
        assert len(stats) == 1
        totals = stats[0]
        assert totals["date"] == "2026-11-01"
        assert totals["total_prints"] == totals["successful"] == 1
        assert totals["failed"] == 0
        assert totals["total_time_s"] == 60
        assert totals["total_filament_g"] == 20
        assert totals["spools_used"] == 1
