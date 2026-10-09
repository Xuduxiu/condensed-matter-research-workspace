from __future__ import annotations

import importlib.util
import os
from pathlib import Path


WORKSPACE_MODULE = Path(__file__).resolve().parents[2] / "paper_workspace.py"
spec = importlib.util.spec_from_file_location("paper_workspace_under_test", WORKSPACE_MODULE)
assert spec and spec.loader
paper_workspace = importlib.util.module_from_spec(spec)
spec.loader.exec_module(paper_workspace)


def test_configured_port_falls_back_for_invalid_values(monkeypatch):
    monkeypatch.setenv("TEST_WORKSPACE_PORT", "invalid")
    assert paper_workspace.configured_port("TEST_WORKSPACE_PORT", 8123) == 8123
    monkeypatch.setenv("TEST_WORKSPACE_PORT", "70000")
    assert paper_workspace.configured_port("TEST_WORKSPACE_PORT", 8123) == 8123
    monkeypatch.setenv("TEST_WORKSPACE_PORT", "9123")
    assert paper_workspace.configured_port("TEST_WORKSPACE_PORT", 8123) == 9123


def test_release_status_detects_source_newer_than_executable(tmp_path, monkeypatch):
    intake = tmp_path / "intake"
    source_dir = intake / "paper_intake"
    release_exe = intake / "releases" / "LabPaperIntake.exe"
    source_dir.mkdir(parents=True)
    release_exe.parent.mkdir(parents=True)
    source = source_dir / "module.py"
    source.write_text("value = 1\n", encoding="utf-8")
    release_exe.write_bytes(b"old executable")
    os.utime(release_exe, (1000, 1000))
    os.utime(source, (2000, 2000))

    monkeypatch.setattr(paper_workspace, "INTAKE", intake)
    monkeypatch.setattr(paper_workspace, "RELEASE_EXE", release_exe)

    status = paper_workspace.intake_release_status()
    assert status["exists"] is True
    assert status["stale"] is True


def test_radar_api_current_requires_unified_library_status_route(monkeypatch):
    monkeypatch.setattr(
        paper_workspace,
        "radar_api_openapi",
        lambda _port: {
            "info": {"title": "Condensed Matter Trend Radar"},
            "paths": {"/api/overview": {}},
        },
    )
    assert paper_workspace.radar_api_running(8000) is True
    assert paper_workspace.radar_api_current(8000) is False

    monkeypatch.setattr(
        paper_workspace,
        "radar_api_openapi",
        lambda _port: {
            "info": {"title": "Condensed Matter Trend Radar"},
            "paths": {"/api/library/status": {}},
        },
    )
    assert paper_workspace.radar_api_current(8000) is True