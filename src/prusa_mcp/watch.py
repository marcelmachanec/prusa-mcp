"""Print watcher: poll PrusaLink and log every print that ends.

Run as ``prusa-watch`` (a separate process; the MCP server only lives while
Claude Code is open). Config: PRUSA_HOST, PRUSA_API_KEY, PRUSA_MCP_DB_PATH.
Read-only towards the printer: it never uploads, starts or stops anything.
"""

import asyncio
import contextlib
import logging
import os
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

import aiosqlite
import httpx

from prusa_mcp import db
from prusa_mcp.prusalink import PrusaLinkClient

POLL_INTERVAL_S = 30
# PrusaLink job state -> print_log status
END_STATES = {"FINISHED": "completed", "STOPPED": "cancelled", "ERROR": "failed"}
VANISHED_COMPLETE_PCT = 99.0
NOTE_UNSEEN = "watcher: end state not observed"
RECENT_IDS_KEPT = 100


@dataclass
class Usage:
    filament_id: int | None
    grams: float | None
    notes: str | None


@dataclass
class FinishedJob:
    job_id: int
    filename: str
    status: str
    finished_at: datetime
    duration_s: int | None
    download_path: str | None
    progress: float | None = None  # last seen, 0-100
    end_seen: bool = True  # False: job vanished, the end state was never polled
    notes: list[str] = field(default_factory=list)
    usage: Usage | None = None

    @property
    def started_at(self) -> datetime:
        return self.finished_at - timedelta(seconds=self.duration_s or 0)


def _filename(job: dict) -> str:
    file = job.get("file") or {}
    return file.get("display_name") or file.get("name") or f"job-{job.get('id')}"


def _download_path(job: dict) -> str | None:
    file = job.get("file") or {}
    download = (file.get("refs") or {}).get("download")
    if download:
        return download
    path, name = file.get("path"), file.get("name")
    if path and name:
        return f"{path.rstrip('/')}/{name}"
    return None


def _duration(job: dict) -> int | None:
    value = job.get("time_printing")
    return int(value) if isinstance(value, (int, float)) else None


def _merge(previous: dict | None, job: dict) -> dict:
    """job on top of the previous body of the same id; missing/None keep old.

    Nested dicts (file, file.refs) are merged the same way.
    """
    if not previous:
        return job
    merged = dict(previous)
    for key, value in job.items():
        if value is None:
            continue
        old = merged.get(key)
        merged[key] = (
            _merge(old, value)
            if isinstance(old, dict) and isinstance(value, dict)
            else value
        )
    return merged


class JobTracker:
    """Turns a stream of /api/v1/job bodies into finished jobs. No I/O."""

    def __init__(self) -> None:
        self._current: dict | None = None  # last body of the job being tracked
        self._last: dict[int, dict] = {}  # last merged body per job id
        # (job id, filename) already reported, newest last - same key as the DB
        self._done: list[tuple[int, str]] = []
        self.pending: list[FinishedJob] = []

    def observe(self, job: dict | None, now: datetime) -> list[FinishedJob]:
        """Feed one poll result (None = HTTP 204, no job). Returns ended jobs."""
        ended: list[FinishedJob] = []
        job_id = job.get("id") if job else None

        previous = self._last.get(job_id)
        old_file = (previous or {}).get("file") or {}
        new_file = (job or {}).get("file") or {}
        changed_file = any(
            old_file.get(key) and new_file.get(key) and old_file[key] != new_file[key]
            for key in ("name", "display_name")
        )
        if self._current is not None and (
            self._current.get("id") != job_id or changed_file
        ):
            ended.append(self._vanished(self._current, now))
            self._current = None

        if job is None:
            return ended
        # A sparse reply (e.g. the end state without file/time) keeps earlier fields.
        job = _merge(None if changed_file else previous, job)
        self._last[job_id] = job
        while len(self._last) > RECENT_IDS_KEPT:
            del self._last[next(iter(self._last))]
        if (job_id, _filename(job)) in self._done:
            return ended

        state = job.get("state")
        if state in END_STATES:
            ended.append(self._finish(job, END_STATES[state], now))
            self._current = None
        else:
            self._current = job
        return ended

    def _vanished(self, job: dict, now: datetime) -> FinishedJob:
        progress = job.get("progress") or 0
        status = "completed" if progress >= VANISHED_COMPLETE_PCT else "cancelled"
        return self._finish(job, status, now, end_seen=False)

    def _finish(
        self, job: dict, status: str, now: datetime, *, end_seen: bool = True
    ) -> FinishedJob:
        self._done.append((job.get("id"), _filename(job)))
        del self._done[:-RECENT_IDS_KEPT]
        progress = job.get("progress")
        return FinishedJob(
            job_id=job.get("id"),
            filename=_filename(job),
            status=status,
            finished_at=now,
            duration_s=_duration(job),
            download_path=_download_path(job),
            progress=float(progress) if isinstance(progress, (int, float)) else None,
            end_seen=end_seen,
            notes=[] if end_seen else [NOTE_UNSEEN],
        )


def used_grams(job: FinishedJob, planned_g: float | None) -> tuple[float | None, str]:
    """Grams to record: the full file for a completed print, else a share by progress.

    The file holds the PLANNED total, so a stopped or failed print gets
    planned * progress / 100 (noted as an estimate), or None if progress is unknown.
    """
    if planned_g is None:
        return None, "watcher: grams unknown"
    if job.status == "completed":
        return planned_g, "watcher: grams from slicer estimate"
    if job.progress is None:
        return None, "watcher: grams unknown (incomplete print, no progress)"
    share = max(0.0, min(job.progress, 100.0))
    return (
        round(planned_g * share / 100, 2),
        f"watcher: grams estimated at {share:g}% of {planned_g:g} g planned",
    )


async def choose_spool(
    conn: aiosqlite.Connection, material: str | None
) -> tuple[int | None, str | None]:
    """The one in-use spool of this material, else (None, note). Never guesses."""
    if not material:
        return None, "watcher: spool? (material unknown)"
    candidates = [
        f
        for status in ("active", "low")
        for f in await db.list_filaments(conn, status=status)
        if f.material.casefold() == material.casefold()
    ]
    if len(candidates) == 1:
        return candidates[0].id, None
    return None, f"watcher: spool? ({len(candidates)} candidates for {material})"


async def _loaded_material(client: PrusaLinkClient) -> str | None:
    printer = await client.get_printer()
    material = (printer.get("telemetry") or {}).get("material")
    if not material or str(material).strip() in ("", "---"):
        return None
    return str(material).strip()


async def _read_usage(
    conn: aiosqlite.Connection, client: PrusaLinkClient, job: FinishedJob
) -> Usage:
    notes = list(job.notes)

    # Spool first: a full ASCII .gcode scan can take long, and the filament
    # may be swapped meanwhile.
    if job.end_seen:
        # The printer still shows this job, so the loaded material is its material.
        try:
            material = await _loaded_material(client)
        except (httpx.HTTPError, ValueError) as exc:
            _log(f"material read failed: {type(exc).__name__}")
            material = None
        filament_id, spool_note = await choose_spool(conn, material)
    else:
        # Detected late: the material loaded now may belong to a later job.
        filament_id, spool_note = None, "watcher: spool? (end not observed)"

    planned = None
    if job.download_path:
        try:
            planned = await client.read_file_grams(job.download_path)
        except (httpx.HTTPError, httpx.InvalidURL, ValueError) as exc:
            _log(f"grams read failed for {job.filename}: {type(exc).__name__}")
    grams, grams_note = used_grams(job, planned)
    notes += [n for n in (grams_note, spool_note) if n]

    return Usage(filament_id, grams, "; ".join(notes) or None)


async def record(
    conn: aiosqlite.Connection, client: PrusaLinkClient, job: FinishedJob
) -> bool:
    """Write one finished job on the watcher's dedicated connection."""
    if job.usage is None:
        job.usage = await _read_usage(conn, client, job)
    usage = job.usage
    try:
        await conn.execute("BEGIN IMMEDIATE")
        # A sparse terminal replay after restart has only the job ID. Suppress
        # ambiguous replays when either observation lacks a filename.
        fallback = f"job-{job.job_id}"
        duplicate = await conn.execute_fetchall(
            """SELECT 1 FROM print_log WHERE printer_job_id = ?
               AND (filename IN (?, ?) OR ? = ?) LIMIT 1""",
            (job.job_id, job.filename, fallback, job.filename, fallback),
        )
        if duplicate:
            await conn.rollback()
            return False
        await db.log_print(
            conn,
            filename=job.filename,
            started_at=job.started_at.isoformat(timespec="seconds"),
            finished_at=job.finished_at.isoformat(timespec="seconds"),
            duration_s=job.duration_s,
            filament_used_g=usage.grams,
            filament_id=usage.filament_id,
            status=job.status,
            notes=usage.notes,
            printer_job_id=job.job_id,
        )
    except BaseException:
        await conn.rollback()
        raise
    _log(
        f"logged {job.status}: {job.filename} "
        f"({usage.grams} g, spool {usage.filament_id})"
    )
    return True


def _log(message: str) -> None:
    stamp = datetime.now().isoformat(timespec="seconds")
    print(f"{stamp} prusa-watch: {message}", file=sys.stderr, flush=True)


async def poll_once(
    conn: aiosqlite.Connection, client: PrusaLinkClient, tracker: JobTracker
) -> int:
    """One poll. A failed request is logged and never ends a tracked job."""
    try:
        job = await client.get_job_v1()
    except (httpx.HTTPError, ValueError) as exc:
        _log(f"poll failed: {type(exc).__name__}")
    else:
        tracker.pending.extend(tracker.observe(job, datetime.now()))
    count = 0
    for finished in tracker.pending.copy():
        try:
            if await record(conn, client, finished):
                count += 1
        except sqlite3.OperationalError as exc:
            _log(f"record failed for {finished.filename}, will retry: {exc}")
            continue
        tracker.pending.remove(finished)
    return count


async def run(
    conn: aiosqlite.Connection,
    client: PrusaLinkClient,
    interval_s: float = POLL_INTERVAL_S,
) -> None:
    tracker = JobTracker()
    while True:
        await poll_once(conn, client, tracker)
        await asyncio.sleep(interval_s)


async def _amain() -> None:
    host = os.environ.get("PRUSA_HOST")
    api_key = os.environ.get("PRUSA_API_KEY")
    if not host or not api_key:
        sys.exit("prusa-watch: set PRUSA_HOST and PRUSA_API_KEY")
    # httpx logs every request at INFO: one line per poll would flood the log.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    project_dir = Path(__file__).parent.parent.parent
    db_path = os.environ.get(
        "PRUSA_MCP_DB_PATH", str(project_dir / "data" / "prints.db")
    )
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    client = PrusaLinkClient(host, api_key)
    async with aiosqlite.connect(db_path) as conn:
        await db.init_db(conn)
        _log(f"watching every {POLL_INTERVAL_S} s, db {db_path}")
        try:
            await run(conn, client)
        finally:
            await client.close()


def main() -> None:
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(_amain())
