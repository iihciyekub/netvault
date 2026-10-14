#!/usr/bin/env python3
"""Verify the public API, rendered UI and asset versions after deployment."""
import json
import sys
from urllib.request import Request, urlopen


def verify(base_url: str, version: str) -> None:
    base_url = base_url.rstrip("/")

    def read(path: str) -> str:
        request = Request(base_url + path, headers={"Cache-Control": "no-cache"})
        with urlopen(request, timeout=20) as response:
            return response.read().decode("utf-8")

    if json.loads(read("/health")).get("status") != "ok":
        raise RuntimeError("Health check failed")
    if json.loads(read("/ready")).get("status") != "ready":
        raise RuntimeError("Readiness check failed")
    deployed = json.loads(read("/openapi.json"))["info"]["version"]
    if deployed != version:
        raise RuntimeError(f"API version is {deployed}, expected {version}")
    page = read("/web/login")
    for marker in (f'NetVault v{version}"', f"styles.css?v={version}\"",
                   f"app.js?v={version}\""):
        if marker not in page:
            raise RuntimeError(f"Rendered web version is inconsistent: missing {marker}")
    print(f"Deployed API, web UI and static assets match NetVault {version}")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("Usage: verify-deployed-release.py BASE_URL VERSION")
    verify(sys.argv[1], sys.argv[2])
