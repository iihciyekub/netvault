from pathlib import Path
from typing import Any

import requests
from requests_toolbelt import MultipartEncoder, MultipartEncoderMonitor

from netvault.cli.config import require_credentials


def auth_headers() -> dict[str, str]:
    _, token = require_credentials()
    return {"Authorization": f"Bearer {token}"}


def server_url() -> str:
    url, _ = require_credentials()
    return url


def raise_for_api_error(response: requests.Response) -> None:
    if response.ok:
        return
    detail: Any
    try:
        detail = response.json().get("detail", response.text)
    except ValueError:
        detail = response.text
    raise RuntimeError(f"{response.status_code}: {detail}")


def api_get(path: str, **params: Any) -> Any:
    response = requests.get(
        f"{server_url()}{path}",
        headers=auth_headers(),
        params={key: value for key, value in params.items() if value is not None},
        timeout=60,
    )
    raise_for_api_error(response)
    return response.json()


def api_post(path: str, json: dict[str, Any] | None = None) -> Any:
    response = requests.post(
        f"{server_url()}{path}",
        headers=auth_headers(),
        json=json,
        timeout=60,
    )
    raise_for_api_error(response)
    return response.json()


def api_delete(path: str) -> Any:
    response = requests.delete(f"{server_url()}{path}", headers=auth_headers(), timeout=60)
    raise_for_api_error(response)
    return response.json()


def upload_pdf(
    path: Path,
    doi: str | None = None,
    no_crossref: bool = False,
    progress_callback: Any | None = None,
) -> Any:
    with path.open("rb") as handle:
        fields: dict[str, Any] = {}
        if doi:
            fields["doi"] = doi
        if no_crossref:
            fields["no_crossref"] = "true"
        fields["file"] = (path.name, handle, "application/pdf")

        encoder = MultipartEncoder(fields=fields)

        def notify_upload_progress(monitor: MultipartEncoderMonitor) -> None:
            if progress_callback:
                progress_callback(monitor.bytes_read, monitor.len)

        monitor = MultipartEncoderMonitor(encoder, notify_upload_progress)
        headers = auth_headers()
        headers["Content-Type"] = monitor.content_type
        response = requests.post(
            f"{server_url()}/pdfs/upload",
            headers=headers,
            data=monitor,
            timeout=300,
        )
    raise_for_api_error(response)
    return response.json()
