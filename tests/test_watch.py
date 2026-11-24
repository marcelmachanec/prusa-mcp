import asyncio
import sqlite3
from datetime import datetime, timedelta

import aiosqlite
import httpx
import pytest
import pytest_asyncio

from prusa_mcp import db, watch
from prusa_mcp.prusalink import PrusaLinkClient
from prusa_mcp.watch import NOTE_UNSEEN, JobTracker

T0 = datetime(2026, 11, 23, 10, 0, 0)
FILE = {
    "name": "SOCK_C~2.BGC",
    "display_name": "Sock_clip_PETG_1h23m.bgcode",
    "path": "/usb",
    "refs": {"download": "/usb/SOCK_C~2.BGC"},
}


def job(state="PRINTING", job_id=7, progress=40.0, time_printing=600, file=FILE):
    return {
        "id": job_id,
        "state": state,
        "progress": progress,
        "time_printing": time_printing,
        "file": file,
    }


def feed(tracker, bodies):
    ended = []
    for i, body in enumerate(bodies):
        ended += tracker.observe(body, T0 + timedelta(seconds=30 * i))
    return ended


# --- JobTracker --------------------------------------------------------------


@pytest.mark.parametrize(
    ("state", "status"),
    [("FINISHED", "completed"), ("STOPPED", "cancelled"), ("ERROR", "failed")],
)
def test_end_state_records_once(state, status):
    ended = feed(
        JobTracker(),
        [job(), job(state=state, progress=100), job(state=state), job(state=state)],
    )
    assert [e.status for e in ended] == [status]
    assert ended[0].notes == []
    assert ended[0].filename == "Sock_clip_PETG_1h23m.bgcode"
    assert ended[0].download_path == "/usb/SOCK_C~2.BGC"


def test_end_state_then_204_records_once():
    ended = feed(JobTracker(), [job(), job(state="FINISHED"), None, None])
    assert len(ended) == 1


def test_vanished_at_99_is_completed_with_note():
    ended = feed(JobTracker(), [job(progress=50), job(progress=99), None])
    assert [(e.status, e.notes) for e in ended] == [("completed", [NOTE_UNSEEN])]


def test_vanished_at_40_is_cancelled_with_note():
    ended = feed(JobTracker(), [job(progress=40), None])
    assert [(e.status, e.notes) for e in ended] == [("cancelled", [NOTE_UNSEEN])]


def test_job_id_change_ends_old_job():
    ended = feed(JobTracker(), [job(job_id=1, progress=100), job(job_id=2)])
    assert [(e.job_id, e.status) for e in ended] == [(1, "completed")]


def test_idle_records_nothing():
    assert feed(JobTracker(), [None, None, None]) == []


def test_sparse_end_reply_keeps_tracked_fields():
    sparse = {"id": 7, "state": "FINISHED"}
    ended = feed(JobTracker(), [job(progress=98, time_printing=900), sparse, sparse])
    assert [(e.filename, e.duration_s, e.progress) for e in ended] == [
        ("Sock_clip_PETG_1h23m.bgcode", 900, 98.0)
    ]


def test_partial_file_object_keeps_nested_fields():
    partial = {"id": 7, "state": "FINISHED", "file": {"name": "SOCK_C~2.BGC"}}
    ended = feed(JobTracker(), [job(), partial])
    assert [(e.filename, e.download_path) for e in ended] == [
        ("Sock_clip_PETG_1h23m.bgcode", "/usb/SOCK_C~2.BGC")
    ]


def test_reused_job_id_with_new_file_is_recorded():
    other = {"name": "B.BGC", "display_name": "B.bgcode", "path": "/usb"}
    ended = feed(
        JobTracker(),
        [
            job(state="FINISHED"),
            None,
            job(file=other),
            job(state="FINISHED", file=other),
        ],
    )
    assert [(e.job_id, e.filename) for e in ended] == [
        (7, "Sock_clip_PETG_1h23m.bgcode"),
        (7, "B.bgcode"),
    ]
    assert ended[-1].download_path == "/usb/B.BGC"


def test_paused_is_still_tracked():
    ended = feed(JobTracker(), [job(), job(state="PAUSED"), job(state="FINISHED")])
    assert [e.status for e in ended] == ["completed"]


def test_times_come_from_time_printing():
    (ended,) = feed(JobTracker(), [job(state="FINISHED", time_printing=3600)])
    assert ended.finished_at == T0
    assert ended.started_at == T0 - timedelta(hours=1)
    assert ended.duration_s == 3600


def test_download_path_falls_back_to_path_and_name():
    file = {"name": "A.BGC", "path": "/usb/"}
    (ended,) = feed(JobTracker(), [job(state="FINISHED", file=file)])
    assert ended.download_path == "/usb/A.BGC"
    assert ended.filename == "A.BGC"


# --- spool choice and recording ---------------------------------------------


@pytest_asyncio.fixture
async def conn():
    c = await aiosqlite.connect(":memory:")
    await db.init_db(c)
    yield c
    await c.close()


async def add_spool(conn, material, status):
    f = await db.add_filament(conn, brand="X", material=material, color="c")
    await db.update_filament(conn, f.id, status=status)
    return f.id


@pytest.mark.asyncio
async def test_one_active_spool_is_chosen(conn):
    sid = await add_spool(conn, "PETG", "active")
    await add_spool(conn, "PETG", "sealed")  # sealed spools are never guessed
    assert await watch.choose_spool(conn, "petg") == (sid, None)


@pytest.mark.asyncio
async def test_low_spool_counts(conn):
    sid = await add_spool(conn, "PLA", "low")
    assert await watch.choose_spool(conn, "PLA") == (sid, None)


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [0, 2])
async def test_zero_or_two_spools_pick_none(conn, count):
    for _ in range(count):
        await add_spool(conn, "PETG", "active")
    spool, note = await watch.choose_spool(conn, "PETG")
    assert spool is None
    assert note == f"watcher: spool? ({count} candidates for PETG)"


def make_client(grams_body=b"; filament used [g]=53.01\n", material="PETG"):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/api/printer":
            return httpx.Response(200, json={"telemetry": {"material": material}})
        if request.url.path == "/usb/SOCK_C~2.BGC":
            return httpx.Response(200, content=grams_body)
        return httpx.Response(404)

    client = PrusaLinkClient(
        "printer.test", "k", transport=httpx.MockTransport(handler)
    )
    return client, calls


def finished(job_id=7, **kw):
    (ended,) = feed(JobTracker(), [job(state="FINISHED", job_id=job_id, **kw)])
    return ended


@pytest.mark.asyncio
async def test_record_deducts_matching_spool(conn):
    sid = await add_spool(conn, "PETG", "active")
    client, _ = make_client()
    assert await watch.record(conn, client, finished()) is True

    (row,) = await db.list_prints(conn)
    assert row.status == "completed"
    assert row.filament_used_g == 53.01
    assert row.filament_id == sid
    assert row.printer_job_id == 7
    assert row.notes == "watcher: grams from slicer estimate"
    spool = await db.get_filament(conn, sid)
    assert spool.remaining_g == pytest.approx(1000 - 53.01)
    await client.close()


@pytest.mark.asyncio
async def test_material_is_read_before_file_download(conn):
    client, calls = make_client()
    await watch.record(conn, client, finished())
    assert calls == ["/api/printer", "/usb/SOCK_C~2.BGC"]
    await client.close()


@pytest.mark.asyncio
async def test_same_job_twice_creates_one_row(conn):
    client, _ = make_client()
    assert await watch.record(conn, client, finished()) is True
    assert await watch.record(conn, client, finished()) is False
    sparse = feed(JobTracker(), [{"state": "FINISHED", "id": 7}])[0]
    assert not await watch.record(conn, client, sparse)
    assert len(await db.list_prints(conn)) == 1
    await client.close()


@pytest.mark.asyncio
async def test_malformed_file_grams_leave_usage_unknown(conn):
    client, _ = make_client(grams_body=b"; filament used [g]=1.2.3\n")
    try:
        assert await watch.record(conn, client, finished())
        (row,) = await db.list_prints(conn)
        assert row.filament_used_g is None
        assert "grams unknown" in row.notes
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_unknown_grams_and_spool_are_noted(conn):
    client, _ = make_client(grams_body=b"no stats here", material="---")
    await watch.record(conn, client, finished())
    (row,) = await db.list_prints(conn)
    assert row.filament_used_g is None
    assert row.filament_id is None
    assert row.notes == "watcher: grams unknown; watcher: spool? (material unknown)"
    await client.close()


@pytest.mark.asyncio
async def test_stopped_print_deducts_share_by_progress(conn):
    sid = await add_spool(conn, "PETG", "active")
    client, _ = make_client()  # file plans 53.01 g
    (ended,) = feed(JobTracker(), [job(state="STOPPED", progress=40)])
    await watch.record(conn, client, ended)

    (row,) = await db.list_prints(conn)
    assert row.status == "cancelled"
    assert row.filament_used_g == pytest.approx(21.2)
    assert row.notes == "watcher: grams estimated at 40% of 53.01 g planned"
    spool = await db.get_filament(conn, sid)
    assert spool.remaining_g == pytest.approx(1000 - 21.2)
    await client.close()


def test_incomplete_without_progress_records_no_grams():
    (ended,) = feed(JobTracker(), [job(state="ERROR", progress=None)])
    assert watch.used_grams(ended, 53.01) == (
        None,
        "watcher: grams unknown (incomplete print, no progress)",
    )


@pytest.mark.asyncio
async def test_late_detected_job_never_picks_a_spool(conn):
    sid = await add_spool(conn, "PETG", "active")
    client, calls = make_client()
    # Old job vanished at 99 % and a new job is already running.
    ended = feed(JobTracker(), [job(job_id=1, progress=99), job(job_id=2)])
    await watch.record(conn, client, ended[0])

    (row,) = await db.list_prints(conn)
    assert row.status == "completed"
    assert row.filament_used_g == 53.01
    assert row.filament_id is None
    assert (
        row.notes == f"{NOTE_UNSEEN}; watcher: grams from slicer estimate; "
        "watcher: spool? (end not observed)"
    )
    assert "/api/printer" not in calls
    spool = await db.get_filament(conn, sid)
    assert spool.remaining_g == 1000
    await client.close()


# --- PrusaLink client --------------------------------------------------------


@pytest.mark.asyncio
async def test_get_job_v1_204_is_none():
    client = PrusaLinkClient(
        "printer.test",
        "k",
        transport=httpx.MockTransport(lambda r: httpx.Response(204)),
    )
    assert await client.get_job_v1() is None
    await client.close()


@pytest.mark.asyncio
async def test_get_job_v1_returns_body():
    def handler(request):
        assert request.url.path == "/api/v1/job"
        return httpx.Response(200, json={"id": 3, "state": "PRINTING"})

    client = PrusaLinkClient(
        "printer.test", "k", transport=httpx.MockTransport(handler)
    )
    assert (await client.get_job_v1())["id"] == 3
    await client.close()


@pytest.mark.asyncio
async def test_get_job_v1_empty_200_raises():
    client = PrusaLinkClient(
        "printer.test",
        "k",
        transport=httpx.MockTransport(lambda r: httpx.Response(200)),
    )
    with pytest.raises(ValueError, match="empty"):
        await client.get_job_v1()
    await client.close()


class ChunkStream(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks

    async def __aiter__(self):
        for c in self.chunks:
            yield c


@pytest.mark.asyncio
async def test_grams_split_across_chunks():
    chunks = [b"x" * 100 + b"; filament used [g] = 3.", b"87\n; more"]
    client = PrusaLinkClient(
        "printer.test",
        "k",
        transport=httpx.MockTransport(
            lambda r: httpx.Response(200, stream=ChunkStream(chunks))
        ),
    )
    assert await client.read_file_grams("/usb/A.GCO") == 3.87
    await client.close()


@pytest.mark.asyncio
async def test_grams_at_very_end_of_file():
    client = PrusaLinkClient(
        "printer.test",
        "k",
        transport=httpx.MockTransport(
            lambda r: httpx.Response(200, content=b"G1 X1\n; filament used [g] = 12.5")
        ),
    )
    assert await client.read_file_grams("/usb/A.GCO") == 12.5
    await client.close()


# --- poll loop ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_failed_poll_does_not_end_tracked_job(conn):
    responses = iter(
        [
            httpx.Response(200, json=job()),
            httpx.ConnectError("down"),
            httpx.Response(200),  # empty 200: a bad reply, not "no job"
            httpx.Response(200, json=job(progress=60)),
        ]
    )

    def handler(request):
        r = next(responses)
        if isinstance(r, Exception):
            raise r
        return r

    client = PrusaLinkClient(
        "printer.test", "k", transport=httpx.MockTransport(handler)
    )
    tracker = JobTracker()
    for _ in range(4):
        assert await watch.poll_once(conn, client, tracker) == 0
    assert await db.list_prints(conn) == []
    await client.close()


@pytest.mark.asyncio
async def test_two_watchers_and_manual_writer_survive_restart(tmp_path):
    path = tmp_path / "pilot.db"
    client, _ = make_client()
    try:
        async with (
            aiosqlite.connect(path) as first,
            aiosqlite.connect(path) as second,
            aiosqlite.connect(path) as manual,
        ):
            await db.init_db(first)
            spool_id = await add_spool(first, "PETG", "active")
            a, b, _ = await asyncio.gather(
                watch.record(first, client, finished()),
                watch.record(second, client, finished()),
                db.log_print(
                    manual,
                    filename="manual",
                    started_at=T0.isoformat(),
                    filament_used_g=10,
                    filament_id=spool_id,
                ),
            )
            assert sorted([a, b]) == [False, True]
        async with aiosqlite.connect(path) as restarted:
            assert not await watch.record(restarted, client, finished())
            assert len(await db.list_prints(restarted)) == 2
            assert (
                await db.get_filament(restarted, spool_id)
            ).remaining_g == pytest.approx(936.99)
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_failed_record_retries_with_original_usage(conn, monkeypatch):
    spool_id = await add_spool(conn, "PETG", "active")
    client, calls = make_client()
    replies = iter([job(state="FINISHED"), None, None])

    async def poll():
        return next(replies)

    monkeypatch.setattr(client, "get_job_v1", poll)
    original = db.log_print

    async def fail_once(*args, **kwargs):
        monkeypatch.setattr(db, "log_print", original)
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(db, "log_print", fail_once)
    tracker = JobTracker()
    try:
        assert await watch.poll_once(conn, client, tracker) == 0
        assert not conn.in_transaction
        assert await db.list_prints(conn) == []
        assert await watch.poll_once(conn, client, tracker) == 1
        assert await watch.poll_once(conn, client, tracker) == 0
        assert calls == ["/api/printer", "/usb/SOCK_C~2.BGC"]
        assert (await db.get_filament(conn, spool_id)).remaining_g == pytest.approx(
            946.99
        )
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_only_download_from_configured_printer():
    client, calls = make_client()
    try:
        for url in ("http://elsewhere.test/file", "//elsewhere.test/file"):
            assert await client.read_file_grams(url) is None
        assert calls == []
    finally:
        await client.close()
