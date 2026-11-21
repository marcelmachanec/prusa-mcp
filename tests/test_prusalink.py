import httpx
import pytest

from prusa_mcp.prusalink import PrusaLinkClient


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method_name", "expected_path", "payload"),
    [
        ("get_printer", "/api/printer", {"state": "idle"}),
        ("get_job", "/api/job", {"id": 42}),
        ("get_version", "/api/version", {"version": "1.0"}),
    ],
)
async def test_get_methods_send_expected_request_and_return_json(
    method_name: str, expected_path: str, payload: dict
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.scheme == "http"
        assert request.url.host == "printer.test"
        assert request.url.path == expected_path
        assert request.headers["X-Api-Key"] == "test-key"
        return httpx.Response(200, json=payload)

    client = PrusaLinkClient(
        "printer.test",
        "test-key",
        transport=httpx.MockTransport(handler),
    )
    try:
        result = await getattr(client, method_name)()
    finally:
        await client.close()

    assert result == payload


@pytest.mark.asyncio
async def test_get_printer_raises_for_server_error() -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(500))
    client = PrusaLinkClient("printer.test", "test-key", transport=transport)
    try:
        with pytest.raises(httpx.HTTPStatusError):
            await client.get_printer()
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(("status_code", "expected"), [(200, True), (503, False)])
async def test_is_online_reflects_status(status_code: int, expected: bool) -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(status_code, request=request)
    )
    client = PrusaLinkClient("printer.test", "test-key", transport=transport)
    try:
        result = await client.is_online()
    finally:
        await client.close()

    assert result is expected


@pytest.mark.asyncio
async def test_is_online_returns_false_on_connection_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection failed", request=request)

    client = PrusaLinkClient(
        "printer.test",
        "test-key",
        transport=httpx.MockTransport(handler),
    )
    try:
        result = await client.is_online()
    finally:
        await client.close()

    assert result is False


@pytest.mark.asyncio
async def test_client_can_be_created_without_transport() -> None:
    client = PrusaLinkClient("printer.test", "test-key")
    await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("overwrite", [False, True])
async def test_upload_file_request_shape(overwrite: bool) -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["headers"] = dict(request.headers)
        seen["body"] = request.content
        return httpx.Response(201)

    client = PrusaLinkClient(
        "printer.test", "test-key", transport=httpx.MockTransport(handler)
    )
    try:
        await client.upload_file("usb", "a.gcode", b"G1 X1\n", overwrite=overwrite)
    finally:
        await client.close()

    assert seen["method"] == "PUT"
    assert seen["path"] == "/api/v1/files/usb/a.gcode"
    assert seen["headers"]["content-type"] == "application/octet-stream"
    assert seen["headers"]["overwrite"] == ("?1" if overwrite else "?0")
    assert seen["headers"]["print-after-upload"] == "?0"
    assert seen["headers"]["x-api-key"] == "test-key"
    assert seen["body"] == b"G1 X1\n"


@pytest.mark.asyncio
async def test_upload_file_raises_on_conflict() -> None:
    client = PrusaLinkClient(
        "printer.test",
        "test-key",
        transport=httpx.MockTransport(lambda r: httpx.Response(409, request=r)),
    )
    try:
        with pytest.raises(httpx.HTTPStatusError):
            await client.upload_file("usb", "a.gcode", b"x")
    finally:
        await client.close()
