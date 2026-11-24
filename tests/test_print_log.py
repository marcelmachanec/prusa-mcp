import asyncio
import json
from types import SimpleNamespace

import aiosqlite
import pytest
import pytest_asyncio

from prusa_mcp import db as queries
from prusa_mcp import server
from prusa_mcp.db import (
    add_filament,
    get_filament,
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


@pytest_asyncio.fixture(params=["file", ":memory:"])
async def server_db(tmp_path, monkeypatch, request):
    path = str(tmp_path / "test.db") if request.param == "file" else ":memory:"
    monkeypatch.setattr(server, "DB_PATH", path)
    monkeypatch.delenv("PRUSA_HOST", raising=False)
    async with server.lifespan(server.mcp) as app:
        context = SimpleNamespace(request_context=SimpleNamespace(lifespan_context=app))
        monkeypatch.setattr(server.mcp, "get_context", lambda: context)
        yield app.db


@pytest.mark.asyncio
async def test_concurrent_manual_and_logged_deductions(server_db, monkeypatch):
    spool = await add_filament(server_db, brand="Test", material="PLA", color="Red")
    log_inserted = asyncio.Event()
    manual_started = asyncio.Event()
    deduct = queries._deduct_filament_no_commit

    async def overlap_deductions(conn, filament_id, grams_used):
        if grams_used == 200:
            assert conn is not server_db
            log_inserted.set()
            await manual_started.wait()
        else:
            manual_started.set()
        await deduct(conn, filament_id, grams_used)

    async def deduct_manually():
        await log_inserted.wait()
        await server.use_filament(spool.id, 100)

    monkeypatch.setattr(queries, "_deduct_filament_no_commit", overlap_deductions)
    async with asyncio.timeout(10), asyncio.TaskGroup() as requests:
        requests.create_task(
            server.log_print("test.gcode", filament_id=spool.id, filament_used_g=200)
        )
        requests.create_task(deduct_manually())

    updated = await get_filament(server_db, spool.id)
    assert updated.remaining_g == 700
    prints = await list_prints(server_db)
    assert [job.filament_used_g for job in prints] == [200]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("record_print", "usage"),
    [
        (server.log_print, {"filament_used_g": 100}),
        (server.calc_print_cost, {"weight_g": 100, "print_time_h": 1}),
    ],
)
async def test_print_write_rolls_back_after_deduction_failure(
    server_db, monkeypatch, record_print, usage
):
    spool = await add_filament(server_db, brand="Test", material="PLA", color="Red")
    deduct = queries._deduct_filament_no_commit

    async def fail_after_deduction(conn, filament_id, grams_used):
        assert conn is not server_db
        await deduct(conn, filament_id, grams_used)
        raise RuntimeError("forced deduction failure")

    with monkeypatch.context() as patch:
        patch.setattr(queries, "_deduct_filament_no_commit", fail_after_deduction)
        with pytest.raises(RuntimeError, match="forced deduction failure"):
            await record_print(filename="failed.gcode", filament_id=spool.id, **usage)

    assert await list_prints(server_db) == []
    assert await get_filament(server_db, spool.id) == spool

    result = await record_print(filename="valid.gcode", filament_id=spool.id, **usage)
    assert json.loads(result)["logged"]["filename"] == "valid.gcode"
    prints = await list_prints(server_db)
    assert [job.filename for job in prints] == ["valid.gcode"]
    updated = await get_filament(server_db, spool.id)
    assert updated.remaining_g == 900


@pytest.mark.asyncio
async def test_cancelled_print_write_rolls_back_and_closes(server_db, monkeypatch):
    spool = await add_filament(server_db, brand="Test", material="PLA", color="Red")
    deduct = queries._deduct_filament_no_commit
    deduction_done = asyncio.Event()
    print_conn = None

    async def pause_after_deduction(conn, filament_id, grams_used):
        nonlocal print_conn
        print_conn = conn
        await deduct(conn, filament_id, grams_used)
        deduction_done.set()
        await asyncio.Future()

    monkeypatch.setattr(queries, "_deduct_filament_no_commit", pause_after_deduction)
    async with asyncio.timeout(10), asyncio.TaskGroup() as requests:
        request = requests.create_task(
            server.log_print(
                "cancelled.gcode", filament_id=spool.id, filament_used_g=100
            )
        )
        await deduction_done.wait()
        request.cancel()

    assert request.cancelled()
    assert await list_prints(server_db) == []
    assert await get_filament(server_db, spool.id) == spool
    assert print_conn is not None
    with pytest.raises(ValueError, match="no active connection"):
        await print_conn.execute("SELECT 1")
    monkeypatch.setattr(queries, "_deduct_filament_no_commit", deduct)
    await server.log_print("retry.gcode", filament_id=spool.id, filament_used_g=100)
    assert (await get_filament(server_db, spool.id)).remaining_g == 900
    assert [job.filename for job in await list_prints(server_db)] == ["retry.gcode"]


@pytest.mark.asyncio
async def test_print_write_connection_enforces_foreign_keys(server_db):
    with pytest.raises(aiosqlite.IntegrityError, match="FOREIGN KEY constraint failed"):
        await server.log_print("invalid.gcode", filament_id=99999)

    assert await list_prints(server_db) == []


@pytest.mark.asyncio
async def test_direct_print_log_rolls_back_failed_deduction(db):
    spool = await add_filament(db, brand="Test", material="PLA", color="Red")
    await db.execute(
        """CREATE TRIGGER fail_deduction BEFORE UPDATE OF remaining_g ON filament
           BEGIN
               SELECT RAISE(ABORT, 'forced deduction failure');
           END"""
    )

    with pytest.raises(aiosqlite.IntegrityError, match="forced deduction failure"):
        await log_print(
            db,
            filename="failed.gcode",
            started_at="2026-01-01T10:00:00",
            filament_id=spool.id,
            filament_used_g=100,
        )

    assert await list_prints(db) == []
    assert await get_filament(db, spool.id) == spool


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


async def seed_watcher_print(conn, printer_job_id=7, **fields):
    return await log_print(
        conn,
        filename="watch.gcode",
        started_at="2026-11-23T10:00:00",
        printer_job_id=printer_job_id,
        notes="watcher observation",
        **fields,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("spool_first", [True, False])
async def test_resolve_partial_fields_and_concurrent_retries(server_db, spool_first):
    spool = await add_filament(server_db, brand="Test", material="PLA", color="Red")
    job = await seed_watcher_print(server_db)
    spool_arg, grams_arg = {"filament_id": spool.id}, {"filament_used_g": 60}
    first, second = (spool_arg, grams_arg) if spool_first else (grams_arg, spool_arg)
    partial = json.loads(await server.resolve_print_usage(job.id, **first))["updated"]
    assert first.items() <= partial.items()
    assert (await get_filament(server_db, spool.id)).remaining_g == 1000
    unresolved = json.loads(await server.list_unresolved_prints())
    assert [p["id"] for p in unresolved["prints"]] == [job.id]

    calls = await asyncio.gather(
        server.resolve_print_usage(job.id, **second),
        server.resolve_print_usage(job.id, **second),
    )
    for call in calls:
        resolved = json.loads(call)["updated"]
        assert resolved["filament_id"] == spool.id
        assert resolved["filament_used_g"] == 60
        assert resolved["notes"] == "watcher observation"
    assert json.loads(await server.list_unresolved_prints())["count"] == 0
    assert (await get_filament(server_db, spool.id)).remaining_g == 940
    report = json.loads(await server.monthly_report("2026-11"))
    assert (report["prints"], report["filament_g"]) == (1, 60)


@pytest.mark.asyncio
async def test_unresolved_filter_and_zero_grams(server_db):
    spool = await add_filament(server_db, brand="Test", material="PLA", color="Red")
    spool_missing = await seed_watcher_print(server_db, 1, filament_used_g=0)
    grams_missing = await seed_watcher_print(server_db, 2, filament_id=spool.id)
    manual = await seed_watcher_print(server_db, None)
    quote = await seed_watcher_print(server_db, 3, is_quote=1)
    latest = json.loads(await server.list_unresolved_prints(limit=1))
    assert [p["id"] for p in latest["prints"]] == [grams_missing.id]
    all_missing = json.loads(await server.list_unresolved_prints())
    assert [p["id"] for p in all_missing["prints"]] == [
        grams_missing.id,
        spool_missing.id,
    ]
    for ineligible_id in (manual.id, quote.id, 99999):
        response = await server.resolve_print_usage(ineligible_id, spool.id)
        assert "error" in json.loads(response)
    result = json.loads(await server.resolve_print_usage(spool_missing.id, spool.id))
    assert result["updated"]["filament_used_g"] == 0
    assert (await get_filament(server_db, spool.id)) == spool


@pytest.mark.asyncio
async def test_resolve_rejects_invalid_or_conflicting_updates(server_db):
    spool = await add_filament(server_db, brand="Test", material="PLA", color="Red")
    job = await seed_watcher_print(server_db, filament_used_g=10)
    for args in (
        {},
        {"filament_used_g": -1},
        {"filament_used_g": float("inf")},
        {"filament_used_g": float("nan")},
        {"filament_used_g": 20, "filament_id": spool.id},
        {"filament_id": 99999},
    ):
        assert "error" in json.loads(await server.resolve_print_usage(job.id, **args))
    assert await queries.get_print(server_db, job.id) == job
    assert await get_filament(server_db, spool.id) == spool
    await server.resolve_print_usage(job.id, spool.id)
    other = await add_filament(server_db, brand="Test", material="PLA", color="Blue")
    assert "error" in json.loads(await server.resolve_print_usage(job.id, other.id))
    assert (await queries.get_print(server_db, job.id)).filament_id == spool.id
    assert (await get_filament(server_db, spool.id)).remaining_g == 990
    assert await get_filament(server_db, other.id) == other


@pytest.mark.asyncio
async def test_resolve_rolls_back_row_and_stock_on_failure(server_db, monkeypatch):
    spool = await add_filament(server_db, brand="Test", material="PLA", color="Red")
    job = await seed_watcher_print(server_db)
    deduct = queries._deduct_filament_no_commit

    async def fail_after_deduction(conn, filament_id, grams_used):
        await deduct(conn, filament_id, grams_used)
        raise RuntimeError("test failure after stock update")

    monkeypatch.setattr(queries, "_deduct_filament_no_commit", fail_after_deduction)
    with pytest.raises(RuntimeError, match="after stock update"):
        await server.resolve_print_usage(job.id, spool.id, 100)
    assert await queries.get_print(server_db, job.id) == job
    assert await get_filament(server_db, spool.id) == spool
