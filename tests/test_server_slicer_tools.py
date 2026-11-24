import json
from types import SimpleNamespace

import pytest

from prusa_mcp import server


def _context(*, db=None, prusa=None):
    app = SimpleNamespace(db=db, prusa=prusa)
    return SimpleNamespace(request_context=SimpleNamespace(lifespan_context=app))


@pytest.mark.asyncio
async def test_slice_model_returns_error_json_on_oserror(monkeypatch) -> None:
    async def fail(*args, **kwargs):
        raise OSError("slice file error")

    monkeypatch.setattr(server.mcp, "get_context", lambda: _context(db=object()))
    monkeypatch.setattr(server.slicer, "slice_model", fail)

    result = await server.slice_model("a.stl", "P", "Q", "M")

    assert json.loads(result) == {"error": "slice file error"}


@pytest.mark.asyncio
async def test_upload_gcode_returns_error_json_on_oserror(monkeypatch) -> None:
    async def fail(*args, **kwargs):
        raise OSError("upload file error")

    monkeypatch.setattr(server.mcp, "get_context", lambda: _context(prusa=object()))
    monkeypatch.setattr(server.slicer, "upload_gcode_file", fail)

    result = await server.upload_gcode("a.gcode")

    assert json.loads(result) == {"error": "upload file error"}
