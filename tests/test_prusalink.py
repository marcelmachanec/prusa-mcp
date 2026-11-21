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
