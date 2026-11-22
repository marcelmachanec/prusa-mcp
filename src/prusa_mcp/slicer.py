"""Slice STL models with the PrusaSlicer CLI and upload G-code to the printer.

Nothing here starts a print: uploads always send ``Print-After-Upload: ?0``.
"""

import asyncio
import contextlib
import os
import re
import secrets
from pathlib import Path

import aiosqlite
import httpx

from prusa_mcp import db
from prusa_mcp.prusalink import PrusaLinkClient

DEFAULT_SLICER_PATH = "C:/Program Files/Prusa3D/PrusaSlicer/prusa-slicer-console.exe"
SLICE_TIMEOUT_S = 600.0
UPLOAD_STORAGE = "usb"
STDERR_TAIL_CHARS = 1500
STATS_TAIL_BYTES = 512 * 1024
# Geometry-only formats. .3mf/.amf can embed slicer settings such as a
# post-processing script, which PrusaSlicer would run: never accept them.
MODEL_SUFFIXES = (".stl", ".obj")

_TIME_RE = re.compile(r"^; estimated printing time \(normal mode\) = (.+)$", re.M)
_TIME_PART_RE = re.compile(r"(\d+)\s*([dhms])")
_GRAMS_RE = re.compile(r"^; filament used \[g\] = ([\d.]+)", re.M)
_TYPE_RE = re.compile(r"^; filament_type = (.+)$", re.M)
_UNIT_SECONDS = {"d": 86400, "h": 3600, "m": 60, "s": 1}


class SlicerError(Exception):
    """A user-facing slicing or upload error (never contains secrets)."""


def get_slicer_path() -> str:
    return os.environ.get("PRUSA_SLICER_PATH") or DEFAULT_SLICER_PATH


def build_command(
    exe: str,
    model_path: str,
    output_path: str,
    *,
    printer_profile: str,
    print_profile: str,
    material_profile: str,
) -> list[str]:
    """Argument list for the PrusaSlicer CLI (ASCII G-code forced)."""
    return [
        exe,
        "--export-gcode",
        "--no-binary-gcode",
        "--printer-profile",
        printer_profile,
        "--print-profile",
        print_profile,
        "--material-profile",
        material_profile,
        "--output",
        output_path,
        model_path,
    ]


def _parse_time(text: str) -> int | None:
    parts = _TIME_PART_RE.findall(text)
    if not parts:
        return None
    return sum(int(n) * _UNIT_SECONDS[u] for n, u in parts)


def parse_gcode_stats(path: str | Path) -> dict:
    """Read PrusaSlicer's trailing comments. Missing values become None."""
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        f.seek(max(0, size - STATS_TAIL_BYTES))
        text = f.read().decode("utf-8", errors="replace")

    time_m = _TIME_RE.findall(text)
    grams_m = _GRAMS_RE.findall(text)
    type_m = _TYPE_RE.findall(text)
    return {
        "filament_g": float(grams_m[-1]) if grams_m else None,
        "print_time_s": _parse_time(time_m[-1]) if time_m else None,
        "filament_type": type_m[-1].strip() if type_m else None,
    }


async def slice_model(
    model_path: str,
    printer_profile: str,
    print_profile: str,
    material_profile: str,
    output_dir: str | None = None,
    *,
    db: aiosqlite.Connection | None = None,
    slicer_path: str | None = None,
    timeout: float = SLICE_TIMEOUT_S,
) -> dict:
    exe = slicer_path or get_slicer_path()
    if not Path(exe).is_file():
        raise SlicerError(
            f"PrusaSlicer not found at {exe}. Set PRUSA_SLICER_PATH to the "
            "prusa-slicer-console.exe location."
        )
    # Absolute paths never start with '-', so the CLI can't read them as options.
    model = Path(model_path).absolute()
    if not model.is_file():
        raise SlicerError(f"Model file not found: {model_path}")
    if model.suffix.lower() not in MODEL_SUFFIXES:
        raise SlicerError(
            f"Unsupported model type {model.suffix!r}; need .stl or .obj "
            "(.3mf/.amf can carry scripts and are refused - export an STL)."
        )
    out_dir = Path(output_dir).absolute() if output_dir else model.parent
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise SlicerError(f"Cannot use output_dir {output_dir}: {e}") from None
    out_path = out_dir / f"{model.stem}.gcode"
    # Slice into a temp file and swap it in only on success: a failed run keeps
    # the last good G-code, and a run that writes nothing can't report stale stats.
    # Unique per call, so two slices of the same model can't share it.
    tmp_path = out_dir / f"{model.stem}.{secrets.token_hex(4)}.partial.gcode"

    cmd = build_command(
        exe,
        str(model),
        str(tmp_path),
        printer_profile=printer_profile,
        print_profile=print_profile,
        material_profile=material_profile,
    )
    try:
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as e:
            raise SlicerError(f"Cannot start PrusaSlicer at {exe}: {e}") from None
        try:
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            raise SlicerError(f"Slicing timed out after {timeout:g} s") from None
        except asyncio.CancelledError:
            proc.kill()
            await proc.wait()
            raise

        if proc.returncode != 0:
            tail = stderr.decode("utf-8", errors="replace")[-STDERR_TAIL_CHARS:]
            raise SlicerError(f"PrusaSlicer failed (exit {proc.returncode}): {tail}")
        if not tmp_path.is_file():
            raise SlicerError(f"PrusaSlicer reported success but wrote no {out_path}")
        # Parse this call's own file before publishing: a parallel slice may
        # replace out_path right after.
        stats = parse_gcode_stats(tmp_path)
        try:
            os.replace(tmp_path, out_path)
        except OSError as e:
            raise SlicerError(
                f"Cannot write {out_path} (open in another program?): {e}"
            ) from None
    finally:
        # Every exit path (error, timeout, cancel) drops the partial file; a
        # failed cleanup must not hide the real error.
        with contextlib.suppress(OSError):
            tmp_path.unlink(missing_ok=True)

    cost = None
    if db is not None and stats["filament_g"] is not None and stats["print_time_s"]:
        result = await _calculate_cost(db, stats)
        cost = None if "error" in result else result
    return {
        "gcode_path": str(out_path),
        "print_time_s": stats["print_time_s"],
        "filament_g": stats["filament_g"],
        "filament_type": stats["filament_type"],
        "cost_estimate": cost,
    }


async def _calculate_cost(conn: aiosqlite.Connection, stats: dict) -> dict:
    return await db.calculate_cost(
        conn,
        weight_g=stats["filament_g"],
        print_time_h=stats["print_time_s"] / 3600,
        material=stats["filament_type"],
    )


async def upload_gcode_file(
    client: PrusaLinkClient, gcode_path: str, overwrite: bool = False
) -> dict:
    """Upload a G-code file to the printer's USB storage. Never starts a print."""
    path = Path(gcode_path)
    if not path.is_file():
        raise SlicerError(f"G-code file not found: {gcode_path}")
    if path.suffix.lower() not in (".gcode", ".bgcode"):
        raise SlicerError(f"Unsupported suffix {path.suffix!r}; need .gcode or .bgcode")
    try:
        data = path.read_bytes()
    except OSError as e:
        raise SlicerError(f"Cannot read {gcode_path}: {e}") from None
    if not data:
        raise SlicerError(f"G-code file is empty: {gcode_path}")

    try:
        await client.upload_file(UPLOAD_STORAGE, path.name, data, overwrite=overwrite)
    except httpx.HTTPStatusError as e:
        code = e.response.status_code
        if code == 409:
            raise SlicerError(
                f"{path.name} already exists on the printer; "
                "retry with overwrite=True to replace it."
            ) from None
        if code in (401, 403):
            raise SlicerError(
                f"PrusaLink rejected the request (HTTP {code}); check PRUSA_API_KEY."
            ) from None
        raise SlicerError(f"PrusaLink upload failed (HTTP {code})") from None
    except httpx.HTTPError as e:
        raise SlicerError(f"PrusaLink upload failed: {type(e).__name__}") from None
    return {
        "uploaded": path.name,
        "storage": UPLOAD_STORAGE,
        "size_bytes": len(data),
        "overwrite": overwrite,
        "note": "Upload only - start the print on the printer.",
    }
