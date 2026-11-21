import json
from datetime import date, timedelta

import aiosqlite
import pytest
import pytest_asyncio

from prusa_mcp.db import (
    add_filament,
    get_filament,
    init_db,
    log_print,
    monthly_report,
    shopping_list,
    update_filament,
    weigh_spool,
)


@pytest_asyncio.fixture
async def db():
    conn = await aiosqlite.connect(":memory:")
    await init_db(conn)
    yield conn
    await conn.close()


async def _spool(db, **kw):
    kw.setdefault("brand", "Test")
    kw.setdefault("material", "PLA")
    kw.setdefault("color", "Red")
    return await add_filament(db, **kw)


# --- weigh_spool ---


@pytest.mark.asyncio
async def test_weigh_default_tare_opens_sealed_spool(db):
    spool = await _spool(db)
    assert spool.tare_g is None
    result = await weigh_spool(db, spool.id, 812)
    assert result.remaining_g == 612
    assert result.status == "active"
    assert result.opened == date.today().isoformat()


@pytest.mark.asyncio
async def test_weigh_explicit_tare_is_stored_and_reused(db):
    spool = await _spool(db)
    first = await weigh_spool(db, spool.id, 900, tare_g=250)
    assert first.remaining_g == 650
    assert first.tare_g == 250
    second = await weigh_spool(db, spool.id, 700)
    assert second.remaining_g == 450
    assert second.tare_g == 250


@pytest.mark.asyncio
async def test_weigh_tare_zero(db):
    spool = await _spool(db)
    result = await weigh_spool(db, spool.id, 500, tare_g=0)
    assert result.remaining_g == 500
    assert result.tare_g == 0
    # stored 0 is honoured, not treated as "missing"
    again = await weigh_spool(db, spool.id, 400)
    assert again.remaining_g == 400


@pytest.mark.asyncio
async def test_weigh_low(db):
    spool = await _spool(db)
    result = await weigh_spool(db, spool.id, 340)  # 140 g < 15 %
    assert result.remaining_g == 140
    assert result.status == "low"


@pytest.mark.asyncio
async def test_weigh_empty_clamped_to_zero(db):
    spool = await _spool(db)
    result = await weigh_spool(db, spool.id, 150)  # below tare
    assert result.remaining_g == 0
    assert result.status == "empty"


@pytest.mark.asyncio
async def test_weigh_above_full_is_clamped(db):
    spool = await _spool(db)
    await weigh_spool(db, spool.id, 500)  # opens it
    result = await weigh_spool(db, spool.id, 1500)
    assert result.remaining_g == 1000
    assert result.status == "active"


@pytest.mark.asyncio
async def test_weigh_full_sealed_spool_stays_sealed(db):
    spool = await _spool(db)
    result = await weigh_spool(db, spool.id, 1200)  # weight_g + default tare
    assert result.remaining_g == 1000
    assert result.status == "sealed"
    assert result.opened is None


@pytest.mark.asyncio
async def test_weigh_low_spool_recovers_to_active(db):
    spool = await _spool(db)
    await weigh_spool(db, spool.id, 340)
    result = await weigh_spool(db, spool.id, 800)
    assert result.status == "active"


@pytest.mark.asyncio
async def test_weigh_negative_input_raises(db):
    spool = await _spool(db)
    with pytest.raises(ValueError):
        await weigh_spool(db, spool.id, -1)
    with pytest.raises(ValueError):
        await weigh_spool(db, spool.id, 500, tare_g=-5)
    unchanged = await get_filament(db, spool.id)
    assert unchanged.remaining_g == 1000
    assert unchanged.tare_g is None


@pytest.mark.asyncio
async def test_weigh_unknown_spool_returns_none(db):
    assert await weigh_spool(db, 999, 500) is None


@pytest.mark.asyncio
async def test_weigh_uses_default_tare_setting(db):
    await db.execute(
        "UPDATE settings SET value = 100 WHERE key = 'default_spool_tare_g'"
    )
    await db.commit()
    spool = await _spool(db)
    result = await weigh_spool(db, spool.id, 600)
    assert result.remaining_g == 500


# --- shopping_list ---


@pytest.mark.asyncio
async def test_shopping_list(db):
    low = await _spool(db, color="Low")
    empty = await _spool(db, color="Empty", material="PETG")
    archived = await _spool(db, color="Old")
    await _spool(db, color="Full", material="PETG")

    await update_filament(db, low.id, remaining_g=100, status="low")
    await update_filament(db, empty.id, remaining_g=0, status="empty")
    await update_filament(db, archived.id, remaining_g=50, status="archived")

    today = date.today()
    recent = (today - timedelta(days=10)).isoformat() + "T10:00:00"
    old = (today - timedelta(days=120)).isoformat() + "T10:00:00"
    await log_print(
        db, filename="a", started_at=recent, filament_id=low.id, filament_used_g=30
    )
    await log_print(
        db, filename="b", started_at=recent, filament_id=empty.id, filament_used_g=70
    )
    await log_print(
        db, filename="c", started_at=old, filament_id=empty.id, filament_used_g=500
    )
    await log_print(
        db,
        filename="q",
        started_at=recent,
        filament_id=low.id,
        filament_used_g=99,
        is_quote=1,
    )

    result = await shopping_list(db)
    assert {s["color"] for s in result["to_buy"]} == {"Low", "Empty"}
    # Low spool: 100 g, the logged prints deduct 30 + 99 (quote also deducts via
    # log_print), so compare against what is actually stored.
    stored_low = await get_filament(db, low.id)
    assert result["stock_by_material"]["PLA"] == {
        "spools": 1,  # archived excluded
        "remaining_g": stored_low.remaining_g,
    }
    assert result["stock_by_material"]["PETG"] == {
        "spools": 2,
        "remaining_g": 1000.0,  # empty (0) + full (1000)
    }
    # 90-day usage: old print and quote excluded
    assert result["used_last_90d_g"] == {"PLA": 30.0, "PETG": 70.0}


# --- monthly_report ---


@pytest.mark.asyncio
async def test_monthly_report(db):
    pla = await _spool(db, material="PLA")
    petg = await _spool(db, material="PETG")
    await log_print(
        db,
        filename="1",
        started_at="2026-10-02T10:00:00",
        duration_s=3600,
        filament_used_g=50,
        filament_id=pla.id,
        cost_breakdown_json=json.dumps({"subtotal": 40.5}),
    )
    await log_print(
        db,
        filename="2",
        started_at="2026-10-05T10:00:00",
        duration_s=7200,
        filament_used_g=20,
        filament_id=petg.id,
        status="failed",
        cost_breakdown_json=json.dumps({"subtotal": 9.5}),
    )
    await log_print(
        db,
        filename="3",
        started_at="2026-10-09T10:00:00",
        duration_s=1800,
        filament_used_g=10,
        filament_id=pla.id,
        status="cancelled",
    )
    await log_print(
        db,
        filename="4",
        started_at="2026-10-10T10:00:00",
        duration_s=1800,
        filament_used_g=10,
        filament_id=pla.id,
    )
    await log_print(
        db,
        filename="quote",
        started_at="2026-10-11T10:00:00",
        duration_s=9999,
        filament_used_g=999,
        filament_id=pla.id,
        is_quote=1,
    )
    await log_print(
        db,
        filename="nov",
        started_at="2026-11-01T10:00:00",
        duration_s=3600,
        filament_used_g=5,
        filament_id=pla.id,
    )

    r = await monthly_report(db, "2026-10")
    assert r["month"] == "2026-10"
    assert r["prints"] == 4
    assert r["completed"] == 2
    assert r["failed"] == 1
    assert r["cancelled"] == 1
    assert r["failure_rate_pct"] == 25.0
    assert r["print_hours"] == 4.0
    assert r["filament_g"] == 90.0
    assert r["cost"] == 50.0
    assert r["by_material"]["PLA"] == {"prints": 3, "filament_g": 70.0}
    assert r["by_material"]["PETG"] == {"prints": 1, "filament_g": 20.0}


@pytest.mark.asyncio
async def test_monthly_report_empty_month(db):
    r = await monthly_report(db, "2020-01")
    assert r["prints"] == 0
    assert r["failure_rate_pct"] == 0
    assert r["print_hours"] == 0
    assert r["filament_g"] == 0
    assert r["cost"] is None
    assert r["by_material"] == {}


@pytest.mark.asyncio
async def test_monthly_report_defaults_to_current_month(db):
    r = await monthly_report(db)
    assert r["month"] == date.today().strftime("%Y-%m")


@pytest.mark.asyncio
async def test_monthly_report_rejects_bad_month(db):
    with pytest.raises(ValueError):
        await monthly_report(db, "October")
