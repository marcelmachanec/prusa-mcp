import httpx


class PrusaLinkClient:
    def __init__(
        self,
        host: str,
        api_key: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.base_url = f"http://{host}"
        self.client = httpx.AsyncClient(
            base_url=self.base_url,
            headers={"X-Api-Key": api_key},
            timeout=10.0,
            transport=transport,
        )

    async def get_printer(self) -> dict:
        resp = await self.client.get("/api/printer")
        resp.raise_for_status()
        return resp.json()

    async def get_job(self) -> dict:
        resp = await self.client.get("/api/job")
        resp.raise_for_status()
        return resp.json()

    async def get_version(self) -> dict:
        resp = await self.client.get("/api/version")
        resp.raise_for_status()
        return resp.json()

    async def is_online(self) -> bool:
        try:
            resp = await self.client.get("/api/version")
            return resp.status_code == 200
        except httpx.HTTPError:
            return False

    async def close(self) -> None:
        await self.client.aclose()
