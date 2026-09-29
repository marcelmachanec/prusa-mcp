import aiosqlite
import pytest
import pytest_asyncio

from prusa_mcp.db import (
    add_filament,
    delete_filament,
    get_filament,
    init_db,
    list_filaments,
    log_print,
    update_filament,
    use_filament,
)


@pytest_asyncio.fixture
async def db():
    conn = await aiosqlite.connect(":memory:")
    await init_db(conn)
    yield conn
    await conn.close()


@pytest.mark.asyncio
async def test_opened_date_is_iso_string(db):
    filament = await add_filament(
        db, brand="Test", material="PLA", color="Red", weight_g=1000
    )
    updated = await use_filament(db, filament.id, 100)
    assert updated.opened is not None
    # Should be YYYY-MM-DD, not a SQL expression
    assert "date(" not in updated.opened
    assert len(updated.opened) == 10  # YYYY-MM-DD


@pytest.mark.asyncio
async def test_fk_enforced(db):
    with pytest.raises(aiosqlite.IntegrityError):
        await db.execute(
            """INSERT INTO print_log (filename, started_at, filament_id, status)
               VALUES ('test.gcode', '2026-01-01', 99999, 'completed')""",
        )
        await db.commit()


@pytest.mark.asyncio
async def test_atomic_deduction(db):
    filament = await add_filament(
        db, brand="Test", material="PLA", color="Blue", weight_g=1000
    )
    job = await log_print(
        db,
        filename="test.gcode",
        started_at="2026-01-01T10:00:00",
        filament_id=filament.id,
        filament_used_g=200,
    )
    assert job is not None
    updated = await get_filament(db, filament.id)
    assert updated.remaining_g == 800


@pytest.mark.asyncio
async def test_use_filament_rejects_negative_grams(db):
    filament = await add_filament(
        db, brand="Test", material="PLA", color="Blue", weight_g=1000
    )

    with pytest.raises(ValueError, match="grams_used must be >= 0"):
        await use_filament(db, filament.id, -50)

    unchanged = await get_filament(db, filament.id)
    assert unchanged.remaining_g == filament.remaining_g
    assert unchanged.status == filament.status


@pytest.mark.asyncio
async def test_log_print_rejects_negative_filament_usage_before_insert(db):
    filament = await add_filament(
        db, brand="Test", material="PLA", color="Blue", weight_g=1000
    )

    with pytest.raises(ValueError, match="filament_used_g must be >= 0"):
        await log_print(
            db,
            filename="negative.gcode",
            started_at="2026-01-01T10:00:00",
            filament_id=filament.id,
            filament_used_g=-50,
        )

    unchanged = await get_filament(db, filament.id)
    assert unchanged.remaining_g == filament.remaining_g
    assert unchanged.status == filament.status

    await db.commit()
    cursor = await db.execute(
        "SELECT COUNT(*) FROM print_log WHERE filename = ?", ("negative.gcode",)
    )
    row = await cursor.fetchone()
    assert row[0] == 0


@pytest.mark.asyncio
async def test_use_filament_zero_grams_changes_nothing(db):
    filament = await add_filament(
        db, brand="Test", material="PLA", color="Blue", weight_g=1000
    )

    unchanged = await use_filament(db, filament.id, 0)

    assert unchanged == filament


@pytest.mark.asyncio
async def test_status_validation_filament(db):
    filament = await add_filament(
        db, brand="Test", material="PLA", color="Red", weight_g=1000
    )
    with pytest.raises(ValueError, match="Invalid filament status"):
        await update_filament(db, filament.id, status="bad")


@pytest.mark.asyncio
async def test_status_validation_print(db):
    with pytest.raises(ValueError, match="Invalid print status"):
        await log_print(
            db,
            filename="test.gcode",
            started_at="2026-01-01T10:00:00",
            status="invalid",
        )


@pytest.mark.asyncio
async def test_remaining_g_cap(db):
    filament = await add_filament(
        db, brand="Test", material="PLA", color="Green", weight_g=1000
    )
    updated = await update_filament(db, filament.id, remaining_g=5000)
    assert updated.remaining_g == 1000


@pytest.mark.asyncio
async def test_delete_filament_no_prints(db):
    filament = await add_filament(
        db, brand="Test", material="PLA", color="White", weight_g=1000
    )
    result = await delete_filament(db, filament.id)
    assert "deleted" in result
    assert await get_filament(db, filament.id) is None


@pytest.mark.asyncio
async def test_delete_filament_with_prints(db):
    filament = await add_filament(
        db, brand="Test", material="PLA", color="Black", weight_g=1000
    )
    await log_print(
        db,
        filename="test.gcode",
        started_at="2026-01-01T10:00:00",
        filament_id=filament.id,
        filament_used_g=50,
    )
    result = await delete_filament(db, filament.id)
    assert "error" in result
    assert "1" in result["error"]  # mentions print count


@pytest.mark.asyncio
async def test_list_filaments_color_filter(db):
    await add_filament(db, brand="A", material="PLA", color="Galaxy Black")
    await add_filament(db, brand="B", material="PLA", color="Prusa Orange")
    results = await list_filaments(db, color="black")
    assert len(results) == 1
    assert results[0].color == "Galaxy Black"


@pytest.mark.asyncio
async def test_list_filaments_include_archived(db):
    await add_filament(db, brand="A", material="PLA", color="Red")
    f2 = await add_filament(db, brand="B", material="PLA", color="Blue")
    await update_filament(db, f2.id, status="archived")

    default = await list_filaments(db)
    assert len(default) == 1

    with_archived = await list_filaments(db, include_archived=True)
    assert len(with_archived) == 2


@pytest.mark.asyncio
async def test_add_filaments_bulk(db):
    # Test via db functions directly (bulk is a server-level tool)
    spools = [
        {"brand": "A", "material": "PLA", "color": "Red"},
        {"brand": "B", "material": "PETG", "color": "Blue"},
        {"brand": "C", "material": "ASA", "color": "Green"},
    ]
    added = []
    for s in spools:
        f = await add_filament(db, brand=s["brand"], material=s["material"], color=s["color"])
        added.append(f)
    assert len(added) == 3
    all_filaments = await list_filaments(db)
    assert len(all_filaments) == 3


@pytest.mark.asyncio
async def test_add_filaments_bulk_partial_failure(db):
    # Valid spool succeeds, missing brand raises KeyError
    f = await add_filament(db, brand="Valid", material="PLA", color="Red")
    assert f is not None

    with pytest.raises(TypeError):
        await add_filament(db, material="PLA", color="Blue")  # missing brand
