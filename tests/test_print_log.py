import aiosqlite
import pytest
import pytest_asyncio

from prusa_mcp.db import (
    add_filament,
    init_db,
    list_prints,
    log_print,
)


@pytest_asyncio.fixture
async def db():
    conn = await aiosqlite.connect(":memory:")
    await init_db(conn)
    yield conn
    await conn.close()


@pytest.mark.asyncio
async def test_list_prints_by_spool(db):
    spool_a = await add_filament(db, brand="A", material="PLA", color="Red")
    spool_b = await add_filament(db, brand="B", material="PLA", color="Blue")

    await log_print(
        db,
        filename="a1.gcode",
        started_at="2026-01-01T10:00:00",
        filament_id=spool_a.id,
    )
    await log_print(
        db,
        filename="a2.gcode",
        started_at="2026-01-01T11:00:00",
        filament_id=spool_a.id,
    )
    await log_print(
        db,
        filename="b1.gcode",
        started_at="2026-01-01T12:00:00",
        filament_id=spool_b.id,
    )

    prints_a = await list_prints(db, filament_id=spool_a.id)
    assert len(prints_a) == 2

    prints_b = await list_prints(db, filament_id=spool_b.id)
    assert len(prints_b) == 1


@pytest.mark.asyncio
async def test_list_prints_date_range(db):
    await log_print(db, filename="jan.gcode", started_at="2026-01-15T10:00:00")
    await log_print(db, filename="feb.gcode", started_at="2026-02-15T10:00:00")
    await log_print(db, filename="mar.gcode", started_at="2026-03-15T10:00:00")

    prints = await list_prints(db, date_from="2026-02-01", date_to="2026-02-28")
    assert len(prints) == 1
    assert prints[0].filename == "feb.gcode"


@pytest.mark.asyncio
async def test_list_prints_combined_filters(db):
    spool = await add_filament(db, brand="A", material="PLA", color="Red")

    await log_print(
        db,
        filename="ok.gcode",
        started_at="2026-01-01T10:00:00",
        filament_id=spool.id,
        status="completed",
    )
    await log_print(
        db,
        filename="fail.gcode",
        started_at="2026-01-01T11:00:00",
        filament_id=spool.id,
        status="failed",
    )
    await log_print(
        db,
        filename="other.gcode",
        started_at="2026-01-01T12:00:00",
        status="completed",
    )

    prints = await list_prints(db, filament_id=spool.id, status="completed")
    assert len(prints) == 1
    assert prints[0].filename == "ok.gcode"
