# tests/test_cost.py
import aiosqlite
import pytest
import pytest_asyncio

from prusa_mcp.db import get_all_settings, get_preset, get_setting, init_db, set_setting


@pytest_asyncio.fixture
async def db():
    conn = await aiosqlite.connect(":memory:")
    await init_db(conn)
    yield conn
    await conn.close()


@pytest.mark.asyncio
async def test_get_default_setting(db):
    val = await get_setting(db, "energy_czk_kwh")
    assert val == 3.6


@pytest.mark.asyncio
async def test_set_setting(db):
    await set_setting(db, "energy_czk_kwh", 4.5)
    val = await get_setting(db, "energy_czk_kwh")
    assert val == 4.5


@pytest.mark.asyncio
async def test_get_all_settings(db):
    settings = await get_all_settings(db)
    assert "energy_czk_kwh" in settings
    assert "labor_czk_h" in settings
    assert settings["default_markup_pct"] == 33


@pytest.mark.asyncio
async def test_get_nonexistent_setting(db):
    val = await get_setting(db, "nonexistent_key")
    assert val is None


@pytest.mark.asyncio
async def test_get_preset_moderate(db):
    preset = await get_preset(db, "moderate")
    assert preset["prep_min"] == 22
    assert preset["post_min"] == 38


@pytest.mark.asyncio
async def test_get_preset_none_level(db):
    preset = await get_preset(db, "none")
    assert preset["prep_min"] == 5
    assert preset["post_min"] == 3


@pytest.mark.asyncio
async def test_get_preset_invalid(db):
    preset = await get_preset(db, "extreme")
    assert preset is None
