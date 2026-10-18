#!/usr/bin/env python3
"""Verify the public API, rendered UI and asset versions after deployment."""
import json
import subprocess
import sys
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlsplit


class _WebAssetPaths(HTMLParser):
    """Read HTML's actual asset URLs, decoding &amp; query separators."""

    def __init__(self) -> None:
        super().__init__()
        self.urls: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attribute = "src" if tag == "script" else "href" if tag == "link" else None
        if attribute is None:
            return
        self.urls.extend(value for key, value in attrs if key == attribute and value)


def verify_web_assets(page: str, version: str) -> None:
    marker = f'class="brand-version">v{version}</small>'
    if marker not in page:
        raise RuntimeError(f"Rendered web version is inconsistent: missing {marker}")
    parser = _WebAssetPaths()
    parser.feed(page)
    for asset in ("styles.css", "app.js", "i18n.js"):
        if not any(
            urlsplit(url).path.endswith("/static/" + asset)
            and parse_qs(urlsplit(url).query).get("v") == [version]
            for url in parser.urls
        ):
            raise RuntimeError(f"Rendered web asset is missing expected version: {asset} v{version}")


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
    verify_web_assets(page, version)
    print(f"Deployed API, web UI and static assets match NetVault {version}")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("Usage: verify-deployed-release.py BASE_URL VERSION")
    verify(sys.argv[1], sys.argv[2])
