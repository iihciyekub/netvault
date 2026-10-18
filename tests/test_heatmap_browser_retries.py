"""Keep real Chromium geometry assertions while handling transient CI startup stalls."""
import json
from html import escape
import subprocess

import pytest

import test_heatmap_layout_browser as browser


def test_chromium_startup_timeout_retries_with_new_profile(tmp_path, monkeypatch):
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        if len(commands) == 1:
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        payload = escape(json.dumps({"ready": True}), quote=True)
        return subprocess.CompletedProcess(
            command, 0, stdout=f'<body data-test-geometry="{payload}"></body>', stderr="",
        )

    monkeypatch.setattr(browser.subprocess, "run", run)
    assert browser._geometry(tmp_path, "chromium", 1440) == {"ready": True}
    assert len(commands) == 2
    assert any("profile-1440x950-dark-idle-open-0" in arg for arg in commands[0])
    assert any("profile-1440x950-dark-idle-open-1" in arg for arg in commands[1])
    assert all("--disable-dev-shm-usage" in cmd for cmd in commands)


def test_chromium_repeated_timeout_fails_without_skipping(tmp_path, monkeypatch):
    attempted = []

    def timeout(command, **kwargs):
        attempted.append(command)
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(browser.subprocess, "run", timeout)
    with pytest.raises(subprocess.TimeoutExpired):
        browser._geometry(tmp_path, "chromium", 1440)
    assert len(attempted) == 2


def test_chromium_missing_geometry_remains_a_failure(tmp_path, monkeypatch):
    calls = []

    def invalid(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="<body></body>", stderr="")

    monkeypatch.setattr(browser.subprocess, "run", invalid)
    with pytest.raises(AssertionError):
        browser._geometry(tmp_path, "chromium", 1440)
    assert len(calls) == 1
