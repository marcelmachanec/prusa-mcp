import math
import re
from urllib.parse import quote

import httpx

# Big ASCII G-code: the printer may need a while to write it to USB.
UPLOAD_TIMEOUT = httpx.Timeout(10.0, read=120.0, write=120.0)
DOWNLOAD_TIMEOUT = httpx.Timeout(10.0, read=60.0)
MAX_GRAMS_SCAN_BYTES = 200 * 1024 * 1024
# Matches the .bgcode header ("filament used [g]=53.01") and ASCII G-code
# comments ("; filament used [g] = 3.87").
_GRAMS_RE = re.compile(rb"filament used \[g\]\s*=\s*([\d.]+)")
GRAMS_TAIL_KEEP = 64  # bytes carried over so a match split across chunks is found


def _parse_grams(match: re.Match[bytes]) -> float | None:
    try:
        grams = float(match.group(1))
    except ValueError:
        return None
    return grams if math.isfinite(grams) else None


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

    async def get_job_v1(self) -> dict | None:
        """Current job from the v1 API, or None when no job exists (HTTP 204)."""
        resp = await self.client.get("/api/v1/job")
        resp.raise_for_status()
        if resp.status_code == 204:
            return None
        if not resp.content:
            # Only 204 means "no job"; an empty 200 is a bad reply, not idle.
            raise ValueError("empty /api/v1/job reply")
        job = resp.json()
        if not isinstance(job, dict) or not isinstance(job.get("id"), int):
            raise ValueError("missing job id in /api/v1/job reply")
        return job

    async def read_file_grams(
        self, download_path: str, max_bytes: int = MAX_GRAMS_SCAN_BYTES
    ) -> float | None:
        """Stream a print file and return PrusaSlicer's 'filament used [g]' value.

        .bgcode keeps it in the header (found fast); ASCII .gcode keeps it in the
        trailing comments, so the whole file may be read, up to max_bytes.
        PrusaLink ignores Range requests, so the stream is cut off on a match.
        """
        url = self.client.base_url.join(download_path)
        base = self.client.base_url
        if url.scheme != base.scheme or url.netloc != base.netloc:
            return None
        buf = b""
        read = 0
        async with self.client.stream("GET", url, timeout=DOWNLOAD_TIMEOUT) as resp:
            resp.raise_for_status()
            async for chunk in resp.aiter_bytes():
                chunk = chunk[: max_bytes - read]
                buf = buf[-GRAMS_TAIL_KEEP:] + chunk
                match = _GRAMS_RE.search(buf)
                # A match touching the end may be a number cut by the chunk border.
                if match and match.end() < len(buf):
                    return _parse_grams(match)
                read += len(chunk)
                if read >= max_bytes:
                    return None
        match = _GRAMS_RE.search(buf)
        return _parse_grams(match) if match else None

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
