from pathlib import Path
import threading
from typing import Any

import requests
from requests_toolbelt import MultipartEncoder, MultipartEncoderMonitor
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from netvault.cli.config import require_credentials

_local = threading.local()


def http_session() -> requests.Session:
    session = getattr(_local, "session", None)
    if session is None:
        session = requests.Session()
        retries = Retry(
            total=3,
            connect=3,
            read=2,
            backoff_factor=0.4,
            status_forcelist=(429, 502, 503, 504),
            allowed_methods=frozenset({"GET", "HEAD", "OPTIONS"}),
            respect_retry_after_header=True,
        )
        session.mount("http://", HTTPAdapter(max_retries=retries))
        session.mount("https://", HTTPAdapter(max_retries=retries))
        _local.session = session
    return session


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
    hint = " Run `nv login` again." if response.status_code in {401, 403} else ""
    raise RuntimeError(f"{response.status_code}: {detail}{hint}")


def api_get(path: str, **params: Any) -> Any:
    response = http_session().get(
        f"{server_url()}{path}",
        headers=auth_headers(),
        params={key: value for key, value in params.items() if value is not None},
        timeout=60,
    )
    raise_for_api_error(response)
    return response.json()


def api_post(path: str, json: dict[str, Any] | None = None) -> Any:
    response = http_session().post(
        f"{server_url()}{path}",
        headers=auth_headers(),
        json=json,
        timeout=60,
    )
    raise_for_api_error(response)
    return response.json()


def api_delete(path: str) -> Any:
    response = http_session().delete(f"{server_url()}{path}", headers=auth_headers(), timeout=60)
    raise_for_api_error(response)
    return response.json()


def upload_pdf(
    path: Path,
    doi: str | None = None,
    no_crossref: bool = False,
    progress_callback: Any | None = None,
    sha256: str | None = None,
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
        if sha256:
            headers["Idempotency-Key"] = sha256
        response = http_session().post(
            f"{server_url()}/pdfs/upload",
            headers=headers,
            data=monitor,
            timeout=300,
        )
    raise_for_api_error(response)
    return response.json()
