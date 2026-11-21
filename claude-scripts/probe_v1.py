"""Probe PrusaLink v1 endpoints. Reads host/key from .mcp.json; never prints the key.

Run it while a print is running to see the job and file-metadata shapes.
"""

import json
import sys
import urllib.error
import urllib.parse
import urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

with open(r"C:\Users\MarcelMachanec\Projects\prusa-mcp\.mcp.json") as fh:
    cfg = json.load(fh)
env = cfg["mcpServers"]["prusa-3d"]["env"]
host, key = env["PRUSA_HOST"], env["PRUSA_API_KEY"]


def get(path: str, limit: int = 3000):
    req = urllib.request.Request(f"http://{host}{path}", headers={"X-Api-Key": key})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            body = r.read().decode("utf-8", "replace")
            print(f"== {path} {r.status}\n{body[:limit] or '(empty)'}\n")
            return json.loads(body) if body else None
    except urllib.error.HTTPError as e:
        print(f"== {path} {e.code}\n")
    except Exception as e:  # noqa: BLE001
        print(f"== {path} ERROR {type(e).__name__}\n")
    return None


get("/api/v1/status")
job = get("/api/v1/job")
get("/api/job")

if job and isinstance(job.get("file"), dict):
    f = job["file"]
    print("== job.file keys:", sorted(f), "\n")
    refs = f.get("refs") or {}
    candidates = []
    if f.get("path") and f.get("name"):
        candidates.append(f"/api/v1/files{f['path'].rstrip('/')}/{f['name']}")
    for ref in refs.values():
        if isinstance(ref, str) and ref.startswith("/api/v1/files"):
            candidates.append(ref)
    for path in dict.fromkeys(candidates):
        get(urllib.parse.quote(path), limit=6000)
