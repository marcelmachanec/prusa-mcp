import asyncio
import subprocess
from pathlib import Path

import aiosqlite
import httpx
import pytest
import pytest_asyncio

from prusa_mcp import db, slicer
from prusa_mcp.prusalink import PrusaLinkClient

SAMPLE_TAIL = """\
; filament used [mm] = 1266.93
; filament used [cm3] = 3.05
; filament used [g] = 3.78
; filament cost = 0.11
; total filament used [g] = 3.78
; estimated printing time (normal mode) = 15m 34s
; estimated printing time (silent mode) = 16m 14s

; prusaslicer_config = begin
; filament_type = PLA
; prusaslicer_config = end
"""


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


# --- build_command ---------------------------------------------------------


def test_build_command_exact_list() -> None:
    cmd = slicer.build_command(
        "C:/slicer.exe",
        "C:/m/cube.stl",
        "C:/out/cube.gcode",
        printer_profile="Printer X",
        print_profile="Print Y",
        material_profile="Mat Z",
    )
    assert cmd == [
        "C:/slicer.exe",
        "--export-gcode",
        "--no-binary-gcode",
        "--printer-profile",
        "Printer X",
        "--print-profile",
        "Print Y",
        "--material-profile",
        "Mat Z",
        "--output",
        "C:/out/cube.gcode",
        "C:/m/cube.stl",
    ]
    assert isinstance(cmd, list)


# --- parse_gcode_stats -----------------------------------------------------


def test_parse_gcode_stats_real_format(tmp_path: Path) -> None:
    f = _write(tmp_path / "a.gcode", "G1 X1\n" + SAMPLE_TAIL)
    assert slicer.parse_gcode_stats(f) == {
        "filament_g": 3.78,
        "print_time_s": 15 * 60 + 34,
        "filament_type": "PLA",
    }


def test_parse_gcode_stats_days_form(tmp_path: Path) -> None:
    f = _write(
        tmp_path / "a.gcode",
        "; estimated printing time (normal mode) = 1d 2h 3m 4s\n",
    )
    assert slicer.parse_gcode_stats(f)["print_time_s"] == 86400 + 7200 + 180 + 4


def test_parse_gcode_stats_minutes_seconds_form(tmp_path: Path) -> None:
    f = _write(
        tmp_path / "a.gcode", "; estimated printing time (normal mode) = 5m 6s\n"
    )
    assert slicer.parse_gcode_stats(f)["print_time_s"] == 306


def test_parse_gcode_stats_missing_values_are_none(tmp_path: Path) -> None:
    f = _write(tmp_path / "a.gcode", "G1 X1\n")
    assert slicer.parse_gcode_stats(f) == {
        "filament_g": None,
        "print_time_s": None,
        "filament_type": None,
    }


# --- slice_model -----------------------------------------------------------


class FakeProc:
    def __init__(self, returncode=0, stdout=b"", stderr=b"", hang=False, on_run=None):
        self.returncode = returncode
        self._out = (stdout, stderr)
        self._hang = hang
        self._on_run = on_run
        self.killed = False

    async def communicate(self):
        if self._on_run:
            self._on_run()
        if self._hang:
            await asyncio.sleep(5)
        return self._out

    def kill(self):
        self.killed = True

    async def wait(self):
        return self.returncode


@pytest_asyncio.fixture
async def conn(tmp_path: Path):
    c = await aiosqlite.connect(str(tmp_path / "t.db"))
    await db.init_db(c)
    yield c
    await c.close()


@pytest.fixture
def exe(tmp_path: Path) -> str:
    p = tmp_path / "slicer.exe"
    p.write_bytes(b"x")
    return str(p)


@pytest.fixture
def model(tmp_path: Path) -> Path:
    return _write(tmp_path / "cube.stl", "solid c\nendsolid c\n")


PROFILES = {
    "printer_profile": "P",
    "print_profile": "Q",
    "material_profile": "M",
}


def _patch_exec(monkeypatch, proc_factory):
    calls = []

    async def fake_exec(*args, **kwargs):
        calls.append((args, kwargs))
        return proc_factory(args)

    monkeypatch.setattr(slicer.asyncio, "create_subprocess_exec", fake_exec)
    return calls


@pytest.mark.asyncio
async def test_slice_model_success(monkeypatch, tmp_path, model, exe, conn) -> None:
    out_dir = tmp_path / "out"

    def factory(args):
        out = Path(args[args.index("--output") + 1])
        return FakeProc(on_run=lambda: _write(out, "G1\n" + SAMPLE_TAIL))

    calls = _patch_exec(monkeypatch, factory)
    result = await slicer.slice_model(
        str(model), **PROFILES, output_dir=str(out_dir), db=conn, slicer_path=exe
    )
    assert result["gcode_path"] == str(out_dir / "cube.gcode")
    assert result["print_time_s"] == 934
    assert result["filament_g"] == 3.78
    assert result["filament_type"] == "PLA"
    assert "suggested_price" in result["cost_estimate"]
    assert "shell" not in calls[0][1]
    assert calls[0][0][0] == exe


@pytest.mark.asyncio
async def test_slice_model_nonzero_exit_reports_stderr_tail(
    monkeypatch, model, exe, conn
) -> None:
    _patch_exec(monkeypatch, lambda a: FakeProc(1, stderr=b"x" * 5000 + b"BOOM"))
    with pytest.raises(slicer.SlicerError) as e:
        await slicer.slice_model(str(model), **PROFILES, db=conn, slicer_path=exe)
    assert "BOOM" in str(e.value)
    assert len(str(e.value)) < 2500


@pytest.mark.asyncio
async def test_slice_model_missing_exe(model, conn, tmp_path) -> None:
    with pytest.raises(slicer.SlicerError, match="not found"):
        await slicer.slice_model(
            str(model), **PROFILES, db=conn, slicer_path=str(tmp_path / "nope.exe")
        )


@pytest.mark.asyncio
async def test_slice_model_missing_model(exe, conn, tmp_path) -> None:
    with pytest.raises(slicer.SlicerError, match="Model"):
        await slicer.slice_model(
            str(tmp_path / "nope.stl"), **PROFILES, db=conn, slicer_path=exe
        )


@pytest.mark.asyncio
async def test_slice_model_timeout(monkeypatch, model, exe, conn) -> None:
    proc = FakeProc(hang=True)
    _patch_exec(monkeypatch, lambda a: proc)
    with pytest.raises(slicer.SlicerError, match="timed out"):
        await slicer.slice_model(
            str(model), **PROFILES, db=conn, slicer_path=exe, timeout=0.05
        )
    assert proc.killed


@pytest.mark.asyncio
@pytest.mark.parametrize("suffix", [".3mf", ".amf", ".gcode"])
async def test_slice_model_refuses_non_geometry_models(
    monkeypatch, tmp_path, exe, conn, suffix
) -> None:
    calls = _patch_exec(monkeypatch, lambda a: FakeProc())
    bad = _write(tmp_path / f"project{suffix}", "x")
    with pytest.raises(slicer.SlicerError, match="Unsupported model type"):
        await slicer.slice_model(str(bad), **PROFILES, db=conn, slicer_path=exe)
    assert calls == []


@pytest.mark.asyncio
async def test_slice_model_dash_names_cannot_become_options(
    monkeypatch, tmp_path, exe, conn
) -> None:
    monkeypatch.chdir(tmp_path)
    _write(tmp_path / "-evil.stl", "solid c\nendsolid c\n")
    calls = _patch_exec(monkeypatch, lambda a: FakeProc(returncode=1))
    with pytest.raises(slicer.SlicerError):
        await slicer.slice_model(
            "-evil.stl", **PROFILES, output_dir="-out", db=conn, slicer_path=exe
        )
    args = calls[0][0]
    assert args[-1] == str(tmp_path / "-evil.stl")
    assert Path(args[args.index("--output") + 1]).is_absolute()
    assert not any(
        a.startswith("-") for a in (args[-1], args[args.index("--output") + 1])
    )


@pytest.mark.asyncio
async def test_slice_model_keeps_symlink_name(monkeypatch, tmp_path, exe, conn) -> None:
    (tmp_path / "library").mkdir()
    target = _write(tmp_path / "library" / "cube.stl", "solid c\nendsolid c\n")
    link = tmp_path / "imports" / "job.stl"
    link.parent.mkdir()
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symlinks not permitted here")

    def factory(args):
        out = Path(args[args.index("--output") + 1])
        return FakeProc(on_run=lambda: _write(out, "G1\n"))

    calls = _patch_exec(monkeypatch, factory)
    result = await slicer.slice_model(str(link), **PROFILES, db=conn, slicer_path=exe)
    assert result["gcode_path"] == str(tmp_path / "imports" / "job.gcode")
    assert calls[0][0][-1] == str(link)


@pytest.mark.asyncio
async def test_slice_model_ignores_stale_output(monkeypatch, model, exe, conn) -> None:
    _write(model.parent / "cube.gcode", "G1\n" + SAMPLE_TAIL)
    _patch_exec(monkeypatch, lambda a: FakeProc())  # exits 0, writes nothing
    with pytest.raises(slicer.SlicerError, match="wrote no"):
        await slicer.slice_model(str(model), **PROFILES, db=conn, slicer_path=exe)


@pytest.mark.asyncio
async def test_slice_model_failure_keeps_last_good_gcode(
    monkeypatch, model, exe, conn
) -> None:
    good = _write(model.parent / "cube.gcode", "G1\n" + SAMPLE_TAIL)

    def factory(args):
        out = Path(args[args.index("--output") + 1])
        return FakeProc(1, stderr=b"BOOM", on_run=lambda: _write(out, "half"))

    _patch_exec(monkeypatch, factory)
    with pytest.raises(slicer.SlicerError, match="BOOM"):
        await slicer.slice_model(str(model), **PROFILES, db=conn, slicer_path=exe)
    assert good.read_text(encoding="utf-8") == "G1\n" + SAMPLE_TAIL
    assert list(model.parent.glob("*.partial.gcode")) == []


@pytest.mark.asyncio
async def test_slice_model_launch_failure_is_slicer_error(
    monkeypatch, model, exe, conn
) -> None:
    async def broken_exec(*args, **kwargs):
        raise PermissionError("Access is denied")

    monkeypatch.setattr(slicer.asyncio, "create_subprocess_exec", broken_exec)
    with pytest.raises(slicer.SlicerError, match="Cannot start PrusaSlicer"):
        await slicer.slice_model(str(model), **PROFILES, db=conn, slicer_path=exe)


@pytest.mark.asyncio
async def test_slice_model_uses_unique_partial_path(
    monkeypatch, model, exe, conn
) -> None:
    def factory(args):
        out = Path(args[args.index("--output") + 1])
        return FakeProc(on_run=lambda: _write(out, "G1\n" + SAMPLE_TAIL))

    calls = _patch_exec(monkeypatch, factory)
    for _ in range(2):
        await slicer.slice_model(str(model), **PROFILES, db=conn, slicer_path=exe)
    outs = [c[0][c[0].index("--output") + 1] for c in calls]
    assert outs[0] != outs[1]
    assert all(o.endswith(".partial.gcode") for o in outs)


@pytest.mark.asyncio
async def test_slice_model_bad_output_dir_is_slicer_error(
    monkeypatch, tmp_path, model, exe, conn
) -> None:
    blocker = _write(tmp_path / "afile", "x")  # a file where a folder must go
    calls = _patch_exec(monkeypatch, lambda a: FakeProc())
    with pytest.raises(slicer.SlicerError, match="output_dir"):
        await slicer.slice_model(
            str(model),
            **PROFILES,
            output_dir=str(blocker / "sub"),
            db=conn,
            slicer_path=exe,
        )
    assert calls == []


@pytest.mark.asyncio
async def test_slice_model_cancel_kills_process(monkeypatch, model, exe, conn) -> None:
    proc = None

    def factory(args):
        nonlocal proc
        out = Path(args[args.index("--output") + 1])
        proc = FakeProc(hang=True, on_run=lambda: _write(out, "half"))
        return proc

    _patch_exec(monkeypatch, factory)
    task = asyncio.create_task(
        slicer.slice_model(str(model), **PROFILES, db=conn, slicer_path=exe)
    )
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert proc.killed
    assert list(model.parent.glob("*.partial.gcode")) == []


@pytest.mark.asyncio
async def test_slice_model_skips_cost_when_stats_missing(
    monkeypatch, model, exe, conn
) -> None:
    def factory(args):
        out = Path(args[args.index("--output") + 1])
        return FakeProc(on_run=lambda: _write(out, "G1\n"))

    _patch_exec(monkeypatch, factory)
    result = await slicer.slice_model(str(model), **PROFILES, db=conn, slicer_path=exe)
    assert result["cost_estimate"] is None
    assert result["filament_g"] is None


def test_slicer_path_env(monkeypatch) -> None:
    monkeypatch.setenv("PRUSA_SLICER_PATH", "D:/x/slicer.exe")
    assert slicer.get_slicer_path() == "D:/x/slicer.exe"
    monkeypatch.delenv("PRUSA_SLICER_PATH")
    assert slicer.get_slicer_path() == slicer.DEFAULT_SLICER_PATH


# --- upload_gcode_file -----------------------------------------------------


def _client(handler) -> PrusaLinkClient:
    return PrusaLinkClient(
        "printer.test", "secret-key", transport=httpx.MockTransport(handler)
    )


@pytest.mark.asyncio
async def test_upload_gcode_file_success(tmp_path: Path) -> None:
    f = _write(tmp_path / "my part.gcode", "G1 X1\n")
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.raw_path.decode()
        seen["overwrite"] = request.headers["Overwrite"]
        return httpx.Response(201)

    client = _client(handler)
    try:
        result = await slicer.upload_gcode_file(client, str(f))
    finally:
        await client.close()
    assert seen["method"] == "PUT"
    assert seen["path"] == "/api/v1/files/usb/my%20part.gcode"
    assert seen["overwrite"] == "?0"
    assert result["uploaded"] == "my part.gcode"
    assert result["storage"] == "usb"


@pytest.mark.asyncio
async def test_upload_gcode_file_conflict_suggests_overwrite(tmp_path: Path) -> None:
    f = _write(tmp_path / "a.gcode", "G1\n")
    client = _client(lambda r: httpx.Response(409, request=r))
    try:
        with pytest.raises(slicer.SlicerError, match="overwrite=True"):
            await slicer.upload_gcode_file(client, str(f))
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_upload_gcode_file_401_hides_key(tmp_path: Path) -> None:
    f = _write(tmp_path / "a.gcode", "G1\n")
    client = _client(lambda r: httpx.Response(401, request=r))
    try:
        with pytest.raises(slicer.SlicerError) as e:
            await slicer.upload_gcode_file(client, str(f))
    finally:
        await client.close()
    assert "secret-key" not in str(e.value)
    assert "401" in str(e.value)


@pytest.mark.asyncio
async def test_upload_gcode_file_rejects_bad_input(tmp_path: Path) -> None:
    client = _client(lambda r: pytest.fail("must not call printer"))
    try:
        with pytest.raises(slicer.SlicerError, match="not found"):
            await slicer.upload_gcode_file(client, str(tmp_path / "no.gcode"))
        txt = _write(tmp_path / "a.txt", "x")
        with pytest.raises(slicer.SlicerError, match="suffix"):
            await slicer.upload_gcode_file(client, str(txt))
        empty = _write(tmp_path / "e.gcode", "")
        with pytest.raises(slicer.SlicerError, match="empty"):
            await slicer.upload_gcode_file(client, str(empty))
    finally:
        await client.close()


# --- optional integration --------------------------------------------------


@pytest.mark.skipif(
    not Path(slicer.get_slicer_path()).is_file(), reason="PrusaSlicer not installed"
)
def test_real_cli_supports_no_binary_gcode_flag() -> None:
    out = subprocess.run(
        [slicer.get_slicer_path(), "--help-fff"],
        capture_output=True,
        text=True,
        timeout=60,
    ).stdout
    assert "--binary-gcode" in out
