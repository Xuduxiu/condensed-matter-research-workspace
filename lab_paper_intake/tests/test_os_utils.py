import platform
import subprocess

from paper_intake import os_utils


def test_open_path_missing_returns_false(tmp_path):
    ok, message = os_utils.open_path(str(tmp_path / "missing.ris"))

    assert ok is False
    assert "does not exist" in message


def test_open_path_uses_subprocess_on_non_windows(monkeypatch, tmp_path):
    target = tmp_path / "selected_papers.ris"
    target.write_text("TY  - JOUR\nER  -\n", encoding="utf-8")
    calls = []

    monkeypatch.setattr(platform, "system", lambda: "Linux")
    monkeypatch.setattr(subprocess, "run", lambda args, check: calls.append((args, check)))

    ok, message = os_utils.open_path(str(target))

    assert ok is True
    assert message == ""
    assert calls == [(["xdg-open", str(target)], True)]


def test_open_path_catches_subprocess_failure(monkeypatch, tmp_path):
    target = tmp_path / "selected_papers.ris"
    target.write_text("TY  - JOUR\nER  -\n", encoding="utf-8")

    def fail(_args, check):
        raise OSError("cannot open")

    monkeypatch.setattr(platform, "system", lambda: "Linux")
    monkeypatch.setattr(subprocess, "run", fail)

    ok, message = os_utils.open_path(str(target))

    assert ok is False
    assert "cannot open" in message
