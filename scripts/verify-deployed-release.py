#!/usr/bin/env python3
"""Verify the public API, rendered UI and asset versions after deployment."""
import json
import subprocess
import sys


def verify(base_url: str, version: str) -> None:
    base_url = base_url.rstrip("/")

    def read(path: str) -> str:
        # Use the same curl/TLS transport as the installer through the public proxy.
        response = subprocess.run(
            ["curl", "--fail", "--silent", "--show-error", "--location", "--max-time", "20",
             "--header", "Cache-Control: no-cache", base_url + path],
            capture_output=True, text=True, check=True,
        )
        return response.stdout

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
