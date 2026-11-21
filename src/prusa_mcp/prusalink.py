from urllib.parse import quote

import httpx

# Big ASCII G-code: the printer may need a while to write it to USB.
UPLOAD_TIMEOUT = httpx.Timeout(10.0, read=120.0, write=120.0)


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

    async def upload_file(
        self,
        storage: str,
        remote_name: str,
        data: bytes,
        overwrite: bool = False,
    ) -> None:
        """Upload a file to PrusaLink storage. Never starts a print."""
        resp = await self.client.put(
            f"/api/v1/files/{quote(storage, safe='')}/{quote(remote_name, safe='')}",
            content=data,
            headers={
                "Content-Type": "application/octet-stream",
                "Overwrite": "?1" if overwrite else "?0",
                "Print-After-Upload": "?0",
            },
            timeout=UPLOAD_TIMEOUT,
        )
        resp.raise_for_status()

    async def close(self) -> None:
        await self.client.aclose()
