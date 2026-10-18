"""The deployment verifier must accept content-hashed web assets."""
from pathlib import Path
import runpy

import pytest


VERIFY = runpy.run_path(
    str(Path(__file__).resolve().parents[1] / "scripts/verify-deployed-release.py")
)["verify_web_assets"]


def _login_markup(version: str = "0.7.81", *, hashed: bool = True) -> str:
    suffix = "&amp;h=abc123" if hashed else ""
    return (
        '<small class="brand-version">v' + version + "</small>"
        '<link rel="stylesheet" href="/nv/static/styles.css?v=' + version + suffix + '">'
        '<script src="/nv/static/i18n.js?v=' + version + suffix + '"></script>'
        '<script src="/nv/static/app.js?v=' + version + suffix + '"></script>'
    )


@pytest.mark.parametrize("hashed", [False, True])
def test_deployment_verifier_accepts_real_asset_url_formats(hashed: bool) -> None:
    VERIFY(_login_markup(hashed=hashed), "0.7.81")


def test_deployment_verifier_rejects_old_version() -> None:
    with pytest.raises(RuntimeError, match="Rendered web version"):
        VERIFY(_login_markup("0.7.80"), "0.7.81")


def test_deployment_verifier_rejects_missing_asset() -> None:
    with pytest.raises(RuntimeError, match="i18n.js"):
        VERIFY(_login_markup().replace('<script src="/nv/static/i18n.js?v=0.7.81&amp;h=abc123"></script>', ""), "0.7.81")
