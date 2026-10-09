from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import json
import os
import secrets
import signal
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


APP_NAME = "CondMat Radar"
APP_DIR_NAME = "CondMatRadar"
DEFAULT_PORT = 8765


def parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            values[key] = value
    return values


def app_home() -> Path:
    configured = os.getenv("CONDMAT_RADAR_APP_HOME", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    local_app_data = os.getenv("LOCALAPPDATA", "").strip()
    base = Path(local_app_data) if local_app_data else Path.home() / "AppData" / "Local"
    return base / APP_DIR_NAME


def bundle_root() -> Path:
    frozen_root = getattr(sys, "_MEIPASS", None)
    return Path(frozen_root).resolve() if frozen_root else Path(__file__).resolve().parents[1]


def configure_environment(home: Path) -> tuple[Path, int]:
    config_path = home / "config" / ".env.local"
    os.environ.setdefault("CONDMAT_RADAR_CONFIG_FILE", str(config_path))
    for key, value in parse_env_file(config_path).items():
        os.environ.setdefault(key, value)

    data = Path(os.environ.get("CONDMAT_RADAR_DATA_DIR", "") or home / "data").resolve()
    os.environ.setdefault("CONDMAT_RADAR_DATA_DIR", str(data))
    os.environ.setdefault("CONDMAT_RADAR_DB", str(data / "condmat_radar.sqlite"))
    os.environ.setdefault("CONDMAT_RADAR_CACHE", str(data / "cache"))
    os.environ.setdefault("CONDMAT_RADAR_EXPORT", str(data / "exports"))
    os.environ.setdefault("CONDMAT_RADAR_LOGS", str(data / "logs"))
    os.environ.setdefault("CONDMAT_RADAR_LOCKS", str(data / "locks"))
    os.environ.setdefault("CONDMAT_RADAR_API_HOST", "127.0.0.1")
    os.environ.setdefault("CONDMAT_RADAR_API_PORT", str(DEFAULT_PORT))

    frontend = bundle_root() / "frontend_dist"
    if not (frontend / "index.html").is_file():
        frontend = bundle_root() / "frontend" / "dist"
    os.environ.setdefault("CONDMAT_RADAR_FRONTEND_DIST", str(frontend))

    try:
        port = int(os.environ["CONDMAT_RADAR_API_PORT"])
    except (KeyError, ValueError):
        port = DEFAULT_PORT
        os.environ["CONDMAT_RADAR_API_PORT"] = str(port)
    if not 1 <= port <= 65535:
        port = DEFAULT_PORT
        os.environ["CONDMAT_RADAR_API_PORT"] = str(port)
    return config_path, port


def runtime_state_path(home: Path) -> Path:
    return home / "runtime" / "server.json"


def _radar_online(port: int, timeout: float = 1.5) -> bool:
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/openapi.json",
            timeout=timeout,
        ) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return payload.get("info", {}).get("title") == "Condensed Matter Trend Radar"
    except (OSError, ValueError, urllib.error.URLError):
        return False


def _port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client:
        client.settimeout(0.5)
        return client.connect_ex(("127.0.0.1", port)) == 0


def _open_when_ready(port: int) -> None:
    for _ in range(80):
        if _radar_online(port, timeout=0.5):
            webbrowser.open(f"http://127.0.0.1:{port}/")
            return
        time.sleep(0.25)


def _show_message(message: str, title: str = APP_NAME, error: bool = False) -> None:
    if os.name == "nt" and getattr(sys, "frozen", False):
        flags = 0x10 if error else 0x40
        ctypes.windll.user32.MessageBoxW(0, message, title, flags)
    else:
        print(message, file=sys.stderr if error else sys.stdout)


def _process_image_path(pid: int) -> Path | None:
    if os.name != "nt" or pid <= 0:
        return None
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    process = kernel32.OpenProcess(0x1000, False, pid)
    if not process:
        return None
    try:
        size = wintypes.DWORD(32768)
        buffer = ctypes.create_unicode_buffer(size.value)
        ok = kernel32.QueryFullProcessImageNameW(
            process,
            0,
            buffer,
            ctypes.byref(size),
        )
        return Path(buffer.value).resolve() if ok else None
    finally:
        kernel32.CloseHandle(process)


def _terminate_verified_process(pid: int) -> bool:
    if os.name != "nt":
        try:
            os.kill(pid, signal.SIGTERM)
            return True
        except OSError:
            return False
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.TerminateProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    process = kernel32.OpenProcess(0x0001, False, pid)
    if not process:
        return False
    try:
        return bool(kernel32.TerminateProcess(process, 0))
    finally:
        kernel32.CloseHandle(process)


def stop_existing(home: Path) -> bool:
    state_path = runtime_state_path(home)
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
        pid = int(state["pid"])
        port = int(state["port"])
        token = str(state["stop_token"])
    except (OSError, ValueError, KeyError, json.JSONDecodeError):
        return False

    expected = Path(str(state.get("executable") or sys.executable)).resolve()
    actual = _process_image_path(pid)
    if actual is None:
        state_path.unlink(missing_ok=True)
        return False
    try:
        same_process = os.path.samefile(actual, expected)
    except OSError:
        same_process = os.path.normcase(str(actual)) == os.path.normcase(str(expected))
    if not same_process:
        state_path.unlink(missing_ok=True)
        return False

    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/desktop/shutdown",
        data=b"",
        method="POST",
        headers={"X-CondMat-Stop-Token": token},
    )
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            graceful_requested = response.status == 200
    except (OSError, urllib.error.URLError):
        graceful_requested = False
    if graceful_requested:
        for _ in range(40):
            if _process_image_path(pid) is None:
                state_path.unlink(missing_ok=True)
                return True
            time.sleep(0.2)

    stopped = _terminate_verified_process(pid)
    if stopped:
        state_path.unlink(missing_ok=True)
    return stopped


def _redirect_frozen_output(home: Path) -> None:
    if not getattr(sys, "frozen", False):
        return
    log_dir = home / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    stream = (log_dir / "desktop.log").open("a", encoding="utf-8", buffering=1)
    sys.stdout = stream
    sys.stderr = stream


def _write_runtime_state(home: Path, port: int, stop_token: str) -> None:
    path = runtime_state_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    process_image = _process_image_path(os.getpid()) or Path(sys.executable).resolve()
    payload: dict[str, Any] = {
        "pid": os.getpid(),
        "port": port,
        "executable": str(process_image),
        "stop_token": stop_token,
        "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def run_server(no_browser: bool = False) -> int:
    home = app_home()
    home.mkdir(parents=True, exist_ok=True)
    _, port = configure_environment(home)
    _redirect_frozen_output(home)

    if _radar_online(port):
        if not no_browser:
            webbrowser.open(f"http://127.0.0.1:{port}/")
        return 0
    if _port_open(port):
        _show_message(
            f"端口 {port} 已被其他程序占用。请关闭占用程序，或在配置文件中修改 "
            "CONDMAT_RADAR_API_PORT。",
            error=True,
        )
        return 2

    try:
        from backend.db.database import connect, init_db
        from backend.db.strict_condmat import apply_strict_condmat_policy_migrations
        from backend.library.workbench import ensure_workbench_schema
        from backend.migrations.unified_library import apply_unified_schema

        with connect() as connection:
            init_db(connection)
            apply_unified_schema(connection)
            ensure_workbench_schema(connection)
            apply_strict_condmat_policy_migrations(connection)

        from backend.api.desktop_control import configure_shutdown
        from backend.api.main import app
        from backend.scheduler.live_scanner import start_live_scanner, stop_live_scanner
        import uvicorn

        server = uvicorn.Server(uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            reload=False,
            access_log=False,
        ))
        stop_token = secrets.token_urlsafe(32)
        configure_shutdown(lambda: setattr(server, "should_exit", True), stop_token)
        _write_runtime_state(home, port, stop_token)
        if not no_browser:
            threading.Thread(target=_open_when_ready, args=(port,), daemon=True).start()
        start_live_scanner()
        try:
            server.run()
        finally:
            stop_live_scanner()
    except Exception as exc:
        _show_message(
            f"CondMat Radar 启动失败。\n\n{type(exc).__name__}: {exc}\n\n"
            f"日志：{home / 'logs' / 'desktop.log'}",
            error=True,
        )
        return 1
    finally:
        state_path = runtime_state_path(home)
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
            if int(state.get("pid", -1)) == os.getpid():
                state_path.unlink(missing_ok=True)
        except (OSError, ValueError, json.JSONDecodeError):
            pass
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="CondMat Radar Windows desktop launcher")
    parser.add_argument("--stop", action="store_true", help="Stop the installed local server.")
    parser.add_argument("--no-browser", action="store_true", help="Start without opening a browser.")
    args = parser.parse_args()
    home = app_home()
    if args.stop:
        stop_existing(home)
        return 0
    return run_server(no_browser=args.no_browser)


if __name__ == "__main__":
    raise SystemExit(main())