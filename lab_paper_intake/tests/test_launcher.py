import socket
from pathlib import Path

import launcher


def test_find_available_port_skips_used_port():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    used_port = sock.getsockname()[1]
    try:
        assert launcher.find_available_port(used_port, used_port + 1) == used_port + 1
    finally:
        sock.close()


def test_candidate_config_paths_prefers_config_env():
    base = Path("C:/app")

    paths = launcher.candidate_config_paths(base)

    assert paths[0] == base / "config" / ".env"
    assert paths[1] == base / ".env"


def test_build_files_do_not_reference_secret_inputs():
    spec = Path("LabPaperIntake.spec").read_text(encoding="utf-8")
    build = Path("build_exe.bat").read_text(encoding="utf-8")

    assert "deepseek_api.txt" not in spec
    assert "data/exports" not in spec.replace("\\", "/")
    assert "data/pdfs" not in spec.replace("\\", "/")
    assert ".env\"" not in spec
    assert "config\\.env.example" in build
