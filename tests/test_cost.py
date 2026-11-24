# tests/test_cost.py
import aiosqlite
import pytest
import pytest_asyncio

from prusa_mcp.db import (
    add_filament,
    calculate_cost,
    get_all_settings,
    get_preset,
    get_setting,
    init_db,
    set_setting,
)


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


@pytest.mark.asyncio
async def test_calculate_cost_basic(db):
    """Benchy example from spreadsheet: 224g PLA Colorfil, 8.4h, moderate prep/post."""
    filament = await add_filament(
        db, brand="Colorfil", material="PLA", color="Generic", weight_g=1000, price=339
    )

    result = await calculate_cost(
        db,
        weight_g=224,
        print_time_h=8.4,
        filament_id=filament.id,
        prep_level="moderate",
        post_level="moderate",
    )
    assert result["filament_cost"] == pytest.approx(75.94, abs=0.5)
    assert result["electricity_cost"] == pytest.approx(4.54, abs=0.1)
    assert result["depreciation_cost"] == pytest.approx(4.41, abs=0.5)
    assert result["prep_cost"] == pytest.approx(91.67, abs=0.5)
    assert result["post_cost"] == pytest.approx(158.33, abs=0.5)
    assert result["consumables_cost"] == pytest.approx(1.0)
    assert "subtotal" in result
    assert "with_failures" in result
    assert "suggested_price" in result
    assert result["suggested_price"] > result["subtotal"]


@pytest.mark.asyncio
async def test_calculate_cost_custom_markup(db):
    filament = await add_filament(
        db, brand="Colorfil", material="PLA", color="Test", weight_g=1000, price=339
    )

    result = await calculate_cost(
        db,
        weight_g=100,
        print_time_h=1,
        filament_id=filament.id,
        prep_level="none",
        post_level="none",
        markup_pct=50,
    )
    expected_subtotal = (
        33.9  # filament: 100/1000 * 339
        + 0.54  # electricity: 3.6 * 0.15 * 1
        + 0.52  # depreciation: 0.52 * 1
        + 20.83  # prep: 5/60 * 250
        + 12.5  # post: 3/60 * 250
        + 1.0  # consumables
    )
    with_failures = expected_subtotal * 1.10
    suggested = with_failures * 1.50
    assert result["suggested_price"] == pytest.approx(suggested, abs=1.0)


@pytest.mark.asyncio
async def test_calculate_cost_by_material(db):
    """When no filament_id, use average price for material."""
    await add_filament(
        db, brand="Colorfil", material="PLA", color="Black", weight_g=1000, price=339
    )
    await add_filament(
        db, brand="Nebula", material="PLA", color="White", weight_g=1000, price=409
    )

    result = await calculate_cost(
        db,
        weight_g=500,
        print_time_h=4,
        material="PLA",
        prep_level="simple",
        post_level="simple",
    )
    assert result["filament_cost"] > 0
    assert result["suggested_price"] > 0


@pytest.mark.asyncio
async def test_calculate_cost_returns_all_keys(db):
    await add_filament(
        db, brand="Test", material="PETG", color="Green", weight_g=1000, price=438
    )

    result = await calculate_cost(
        db,
        weight_g=350,
        print_time_h=6,
        material="PETG",
        prep_level="moderate",
        post_level="moderate",
    )
    expected_keys = {
        "filament_cost",
        "electricity_cost",
        "depreciation_cost",
        "prep_cost",
        "post_cost",
        "consumables_cost",
        "subtotal",
        "with_failures",
        "suggested_price",
        "currency",
        "failure_rate_pct",
        "markup_pct",
        "prep_min",
        "post_min",
    }
    assert set(result.keys()) == expected_keys
    assert result["currency"] == "CZK"


@pytest.mark.asyncio
async def test_calculate_cost_warns_when_no_price(db):
    filament = await add_filament(
        db, brand="NoPrice", material="PLA", color="Clear", weight_g=1000, price=None
    )
    result = await calculate_cost(
        db, weight_g=100, print_time_h=1, filament_id=filament.id
    )
    assert result["filament_cost"] == 0
    assert "warning" in result


@pytest.mark.asyncio
async def test_calculate_cost_zero_weight_returns_error(db):
    filament = await add_filament(
        db, brand="Broken", material="PLA", color="Red", weight_g=0, price=300
    )
    result = await calculate_cost(
        db, weight_g=100, print_time_h=1, filament_id=filament.id
    )
    assert "error" in result


@pytest.mark.asyncio
async def test_cost_rejects_selected_non_czk_spool(db):
    spool = await add_filament(
        db, brand="Test", material="PLA", color="Blue", price=25, currency="EUR"
    )
    result = await calculate_cost(
        db, weight_g=100, print_time_h=1, filament_id=spool.id
    )
    assert "EUR" in result["error"]
    assert "CZK" in result["error"]


@pytest.mark.asyncio
async def test_material_cost_only_uses_czk_prices(db):
    await add_filament(
        db, brand="Test", material="PLA", color="Blue", price=25, currency="EUR"
    )
    result = await calculate_cost(db, weight_g=100, print_time_h=1, material="PLA")
    assert result["filament_cost"] == 0
    assert "No CZK spool price" in result["warning"]

    spool = await add_filament(
        db, brand="Test", material="PLA", color="Red", price=400, currency="\tczk\n"
    )
    for selection in ({"filament_id": spool.id}, {"material": "PLA"}):
        result = await calculate_cost(db, weight_g=100, print_time_h=1, **selection)
        assert result["filament_cost"] == 40
        assert "warning" not in result
