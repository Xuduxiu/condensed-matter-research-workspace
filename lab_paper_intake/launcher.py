from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
import webbrowser
from pathlib import Path


DEFAULT_PORT = 8501
MAX_PORT = 8510


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def runtime_base_dir() -> Path:
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def bundle_base_dir() -> Path:
    if is_frozen():
        return Path(getattr(sys, "_MEIPASS", runtime_base_dir())).resolve()
    return Path(__file__).resolve().parent


def resolve_app_path() -> Path:
    app_path = bundle_base_dir() / "app.py"
    if not app_path.exists():
        raise FileNotFoundError(f"Cannot find app.py at {app_path}")
    return app_path


def candidate_config_paths(base_dir: Path | None = None) -> list[Path]:
    root = base_dir or runtime_base_dir()
    return [root / "config" / ".env", root / ".env"]


def load_external_env(base_dir: Path | None = None) -> None:
    for path in reversed(candidate_config_paths(base_dir)):
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                stripped = line.strip()
                if not stripped or stripped.startswith("#") or "=" not in stripped:
                    continue
                key, value = stripped.split("=", 1)
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key:
                    os.environ[key] = value


def find_available_port(start: int = DEFAULT_PORT, end: int = MAX_PORT) -> int:
    for port in range(start, end + 1):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.2)
            if sock.connect_ex(("127.0.0.1", port)) != 0:
                return port
    raise RuntimeError(f"No available localhost port in range {start}-{end}.")


def build_streamlit_command(app_path: Path, port: int) -> list[str]:
    common = [
        "run",
        str(app_path),
        "--server.headless=true",
        f"--server.port={port}",
        "--browser.gatherUsageStats=false",
        "--global.developmentMode=false",
    ]
    if is_frozen():
        return [sys.executable, "--streamlit-child", *common]
    return [sys.executable, "-m", "streamlit", *common]


def run_streamlit_child() -> None:
    from streamlit.web import cli as streamlit_cli

    sys.argv = ["streamlit", *sys.argv[2:]]
    streamlit_cli.main()


def main() -> int:
    if "--streamlit-child" in sys.argv:
        run_streamlit_child()
        return 0

    base_dir = runtime_base_dir()
    load_external_env(base_dir)
    app_path = resolve_app_path()
    port = find_available_port()
    command = build_streamlit_command(app_path, port)
    process = subprocess.Popen(command, cwd=str(base_dir))
    url = f"http://localhost:{port}"
    time.sleep(2)
    webbrowser.open(url)
    try:
        return process.wait()
    except KeyboardInterrupt:
        return 130
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                process.kill()


if __name__ == "__main__":
    raise SystemExit(main())
