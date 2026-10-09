from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Sequence
from urllib.error import URLError
from urllib.request import urlopen


ROOT = Path(__file__).resolve().parent
RADAR = ROOT / "condmat-trend-radar"
INTAKE = ROOT / "lab_paper_intake"
FRONTEND = RADAR / "frontend"
CURRENT_INBOX = INTAKE / "data" / "inbox" / "trend_radar_download_tasks"
LEGACY_INBOX = ROOT / "data" / "inbox" / "trend_radar_download_tasks"
RELEASE_EXE = (
    INTAKE / "releases" / "LabPaperIntake_v0.3_release" / "LabPaperIntake.exe"
)


def project_python(project: Path) -> Path:
    candidate = project / ".venv" / "Scripts" / "python.exe"
    return candidate if candidate.exists() else Path(sys.executable)


def npm_command() -> str:
    return shutil.which("npm.cmd") or shutil.which("npm") or "npm.cmd"


def child_env() -> dict[str, str]:
    env = os.environ.copy()
    env.setdefault("PYTHONUTF8", "1")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    for path in [ROOT / ".env"]:
        for key, value in read_env(path).items():
            env.setdefault(key, value)
    return env


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
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


def configured_path(name: str, default: Path) -> Path:
    value = (child_env().get(name) or "").strip()
    if not value:
        return default
    path = Path(value).expanduser()
    return path if path.is_absolute() else ROOT / path


def intake_data_dir() -> Path:
    return configured_path("PAPER_INTAKE_DATA_DIR", INTAKE / "data")


def intake_inbox_dir() -> Path:
    return configured_path(
        "PAPER_INTAKE_INBOX",
        intake_data_dir() / "inbox" / "trend_radar_download_tasks",
    )


def intake_db_path() -> Path:
    return configured_path("PAPER_INTAKE_DB", intake_data_dir() / "papers.db")


def latest_task_file() -> Path | None:
    candidates: list[Path] = []
    patterns = ("download_tasks_*.json", "trend_radar_download_tasks_*.json")
    for inbox in (intake_inbox_dir(), LEGACY_INBOX):
        if inbox.exists():
            for pattern in patterns:
                candidates.extend(
                    path
                    for path in inbox.glob(pattern)
                    if ".manifest." not in path.name.lower()
                )
    return max(candidates, key=lambda path: path.stat().st_mtime_ns) if candidates else None


def task_count(path: Path | None) -> int | None:
    if path is None:
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return None
    if isinstance(payload, list):
        return len(payload)
    if isinstance(payload, dict) and isinstance(payload.get("tasks"), list):
        return len(payload["tasks"])
    return None


def sqlite_paper_count(path: Path) -> int | None:
    if not path.exists():
        return 0
    try:
        with sqlite3.connect(path) as conn:
            row = conn.execute("SELECT COUNT(*) FROM papers").fetchone()
        return int(row[0]) if row else 0
    except sqlite3.Error:
        return None


def latest_intake_source_mtime() -> float | None:
    candidates = [INTAKE / "app.py"]
    source_dir = INTAKE / "paper_intake"
    if source_dir.exists():
        candidates.extend(source_dir.rglob("*.py"))
    mtimes: list[float] = []
    for path in candidates:
        try:
            mtimes.append(path.stat().st_mtime)
        except OSError:
            continue
    return max(mtimes) if mtimes else None


def intake_release_status() -> dict[str, Any]:
    source_mtime = latest_intake_source_mtime()
    try:
        release_mtime = RELEASE_EXE.stat().st_mtime
    except OSError:
        release_mtime = None
    return {
        "path": str(RELEASE_EXE),
        "exists": release_mtime is not None,
        "stale": (
            release_mtime is not None
            and source_mtime is not None
            and source_mtime > release_mtime
        ),
        "source_mtime": source_mtime,
        "release_mtime": release_mtime,
    }


def configured_port(name: str, default: int) -> int:
    raw_value = (child_env().get(name) or "").strip()
    try:
        port = int(raw_value) if raw_value else default
    except ValueError:
        return default
    return port if 1 <= port <= 65535 else default


def service_status() -> dict[str, dict[str, Any]]:
    targets = {
        "trend_radar_api": (
            configured_port("CONDMAT_RADAR_API_PORT", 8000),
            radar_api_running,
        ),
        "trend_radar_frontend": (5173, trend_frontend_running),
        "paper_intake": (
            configured_port("STREAMLIT_SERVER_PORT", 8501),
            streamlit_running,
        ),
    }
    services: dict[str, dict[str, Any]] = {}
    for name, (port, recognizer) in targets.items():
        listening = port_open(port)
        recognized = recognizer(port) if listening else False
        current = (
            radar_api_current(port)
            if name == "trend_radar_api" and recognized
            else recognized
        )
        state = (
            "running"
            if recognized and current
            else "stale"
            if recognized
            else "occupied"
            if listening
            else "stopped"
        )
        services[name] = {
            "port": port,
            "listening": listening,
            "recognized": recognized,
            "current": current if recognized else None,
            "state": state,
        }
    return services


def intake_queue_summary() -> dict[str, Any]:
    command = [
        str(project_python(INTAKE)),
        "-m",
        "paper_intake.cli",
        "queue-status",
    ]
    result = subprocess.run(
        command,
        cwd=str(INTAKE),
        env=child_env(),
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )
    if result.returncode:
        return {
            "ok": False,
            "error": result.stderr.strip() or f"exit_code_{result.returncode}",
        }
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return {"ok": False, "error": "invalid_queue_status_output"}
    return {"ok": True, **payload}


def intake_database_summary(db_override: str | Path | None = None) -> dict[str, Any]:
    command = [
        str(project_python(INTAKE)),
        "-m",
        "paper_intake.cli",
        "db-status",
        "--db",
        str(Path(db_override) if db_override else intake_db_path()),
    ]
    result = subprocess.run(
        command,
        cwd=str(INTAKE),
        env=child_env(),
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return {
            "ok": False,
            "error": result.stderr.strip() or "invalid_database_status_output",
        }
    return {"ok": result.returncode == 0, **payload}

def radar_database_summary(
    db_override: str | Path | None = None,
    *,
    full_check: bool = False,
) -> dict[str, Any]:
    command = [
        str(project_python(RADAR)),
        "-m",
        "backend.db.status",
    ]
    if db_override:
        command.extend(["--db", str(db_override)])
    if full_check:
        command.append("--full")
    result = subprocess.run(
        command,
        cwd=str(RADAR),
        env=child_env(),
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return {
            "ok": False,
            "error": result.stderr.strip() or "invalid_radar_database_status_output",
        }
    return {"ok": result.returncode == 0, **payload}


def show_database_status(args: argparse.Namespace) -> int:
    intake = intake_database_summary(getattr(args, "db", None))
    radar = radar_database_summary(
        getattr(args, "radar_db", None),
        full_check=getattr(args, "full", False),
    )
    payload = {
        "healthy": bool(intake.get("healthy") and radar.get("healthy")),
        "intake": intake,
        "radar": radar,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload["healthy"] else 1

def build_status() -> dict[str, Any]:
    queue = intake_queue_summary()
    database = intake_database_summary()
    radar_database = radar_database_summary()
    latest_value = queue.get("latest_path") if queue.get("ok") else None
    latest = Path(latest_value) if latest_value else latest_task_file()
    intake_db = intake_db_path()
    current_inbox = intake_inbox_dir()
    return {
        "workspace": str(ROOT),
        "services": service_status(),
        "release": intake_release_status(),
        "projects": {
            "trend_radar": {
                "path": str(RADAR),
                "python": str(project_python(RADAR)),
                "backend_ready": (RADAR / "backend" / "api" / "main.py").exists(),
                "frontend_ready": (FRONTEND / "package.json").exists(),
                "node_modules_ready": (FRONTEND / "node_modules").exists(),
                "database_health": radar_database,
            },
            "paper_intake": {
                "path": str(INTAKE),
                "python": str(project_python(INTAKE)),
                "app_ready": (INTAKE / "app.py").exists(),
                "database": str(intake_db),
                "paper_count": sqlite_paper_count(intake_db),
                "database_health": database,
            },
        },
        "integration": {
            "current_inbox": str(current_inbox),
            "legacy_inbox": str(LEGACY_INBOX),
            "latest_task_file": str(latest) if latest else None,
            "latest_task_count": task_count(latest),
            "queue": queue,
        },
        "configuration": {
            "workspace_env_exists": (ROOT / ".env").exists(),
            "workspace_env_example_exists": (ROOT / ".env.example").exists(),
            "plaintext_legacy_secret_file_detected": (ROOT / "deepseek_api.txt").exists(),
        },
    }


def print_status(status: dict[str, Any]) -> None:
    radar = status["projects"]["trend_radar"]
    intake = status["projects"]["paper_intake"]
    database = intake.get("database_health") or {}
    radar_database = radar.get("database_health") or {}
    integration = status["integration"]
    services = status["services"]
    release = status["release"]
    print("论文工作台状态")
    print(f"  工作区: {status['workspace']}")
    print(
        "  趋势雷达: "
        f"backend={'ok' if radar['backend_ready'] else 'missing'}, "
        f"frontend={'ok' if radar['frontend_ready'] else 'missing'}, "
        f"node_modules={'ok' if radar['node_modules_ready'] else 'missing'}"
    )
    radar_counts = radar_database.get("counts") or {}
    radar_size_gb = float(radar_database.get("size_bytes") or 0) / (1024 ** 3)
    print(
        "  Radar 数据库: "
        f"healthy={'yes' if radar_database.get('healthy') else 'no'}, "
        f"papers={radar_counts.get('papers', 'unknown')}, "
        f"size={radar_size_gb:.2f}GB, "
        f"check={radar_database.get('check_mode', 'unknown')}"
    )
    print(
        "  论文入库: "
        f"app={'ok' if intake['app_ready'] else 'missing'}, "
        f"papers={intake['paper_count'] if intake['paper_count'] is not None else 'unknown'}"
    )
    duplicate_groups = database.get("duplicate_groups") or {}
    duplicate_total = sum(int(value) for value in duplicate_groups.values())
    print(
        "  Intake 数据库: "
        f"healthy={'yes' if database.get('healthy') else 'no'}, "
        f"schema={database.get('user_version', 'unknown')}/"
        f"{database.get('expected_schema_version', 'unknown')}, "
        f"duplicate_groups={duplicate_total}"
    )
    print(
        "  服务状态: "
        f"API={services['trend_radar_api']['state']}:{services['trend_radar_api']['port']}, "
        f"Radar UI={services['trend_radar_frontend']['state']}:{services['trend_radar_frontend']['port']}, "
        f"Intake={services['paper_intake']['state']}:{services['paper_intake']['port']}"
    )
    release_label = (
        "missing"
        if not release["exists"]
        else "stale"
        if release["stale"]
        else "current"
    )
    print(f"  Windows 发布包: {release_label} ({release['path']})")
    print(
        "  最新任务: "
        f"{integration['latest_task_file'] or 'none'} "
        f"(count={integration['latest_task_count'] if integration['latest_task_count'] is not None else 'unknown'})"
    )
    queue = integration.get("queue") or {}
    if queue.get("ok"):
        print(
            "  任务队列: "
            f"pending={queue.get('pending_batches', 0)}, "
            f"imported={queue.get('imported_batches', 0)}, "
            f"invalid={queue.get('invalid_batches', 0)}"
        )
        lifecycle = queue.get("lifecycle_counts") or {}
        print(
            "  生命周期: "
            f"oa_resolved={lifecycle.get('oa_resolved', 0)}, "
            f"downloaded={lifecycle.get('downloaded', 0)}, "
            f"exported={lifecycle.get('exported', 0)}"
        )
    else:
        print(f"  任务队列: unavailable ({queue.get('error', 'unknown')})")
    if status["configuration"]["plaintext_legacy_secret_file_detected"]:
        print("  警告: 根目录仍存在 deepseek_api.txt；它已被忽略，但建议迁移到 .env 后手动删除。")


def doctor() -> int:
    status = build_status()
    errors: list[str] = []
    warnings: list[str] = []

    for project_name in ("trend_radar", "paper_intake"):
        python_path = Path(status["projects"][project_name]["python"])
        if not python_path.exists():
            errors.append(f"{project_name}: Python interpreter not found: {python_path}")
    database = status["projects"]["paper_intake"].get("database_health") or {}
    if not database.get("ok"):
        errors.append(f"database: status unavailable: {database.get('error', 'unknown')}")
    elif not database.get("healthy"):
        errors.append("database: SQLite quick check or foreign-key check failed.")
    else:
        if database.get("user_version") != database.get("expected_schema_version"):
            warnings.append(
                "database: schema migration is pending; run paper.cmd db-migrate."
            )
        duplicates = sum(
            int(value) for value in (database.get("duplicate_groups") or {}).values()
        )
        if duplicates:
            warnings.append(
                f"database: {duplicates} duplicate identity group(s) await canonical migration."
            )
    radar_database = status["projects"]["trend_radar"].get("database_health") or {}
    if not radar_database.get("ok"):
        errors.append(
            f"radar database: status unavailable: {radar_database.get('error', 'unknown')}"
        )
    elif not radar_database.get("healthy"):
        errors.append("radar database: required tables are missing or the read-only check failed.")
    if not status["projects"]["trend_radar"]["node_modules_ready"]:
        errors.append("trend_radar: frontend/node_modules is missing; run npm install in the frontend.")
    if not status["configuration"]["workspace_env_example_exists"]:
        errors.append("workspace: .env.example is missing.")
    if not status["configuration"]["workspace_env_exists"]:
        warnings.append("workspace: .env is optional and has not been created.")
    if status["configuration"]["plaintext_legacy_secret_file_detected"]:
        warnings.append("security: deepseek_api.txt is a legacy plaintext secret file.")
    for service_name, service in status["services"].items():
        if service["state"] == "occupied":
            warnings.append(
                f"services: {service_name} port {service['port']} is occupied by an unrecognized service."
            )
        elif service["state"] == "stale":
            warnings.append(
                f"services: {service_name} is running older code; restart it to load current capabilities."
            )
    release = status["release"]
    if release["exists"] and release["stale"]:
        warnings.append("release: LabPaperIntake.exe is older than the current source code.")
    if latest_task_file() and task_count(latest_task_file()) is None:
        warnings.append("integration: latest task JSON could not be parsed.")
    queue = status["integration"].get("queue") or {}
    if not queue.get("ok"):
        warnings.append(
            f"integration: queue status unavailable: {queue.get('error', 'unknown')}"
        )
    elif queue.get("invalid_batches", 0):
        warnings.append(
            f"integration: {queue['invalid_batches']} invalid task batch(es)."
        )
    lifecycle = queue.get("lifecycle_counts") or {}
    if lifecycle.get("invalid_events", 0):
        warnings.append(
            f"integration: {lifecycle['invalid_events']} invalid lifecycle event(s)."
        )

    print_status(status)
    for warning in warnings:
        print(f"  WARN: {warning}")
    for error in errors:
        print(f"  ERROR: {error}")
    print(f"  诊断结果: {len(errors)} error(s), {len(warnings)} warning(s)")
    return 1 if errors else 0


def run_command(command: Sequence[str], cwd: Path, label: str) -> int:
    print(f"\n[{label}] {' '.join(str(part) for part in command)}", flush=True)
    result = subprocess.run(
        list(command),
        cwd=str(cwd),
        env=child_env(),
        check=False,
    )
    if result.returncode:
        print(f"[{label}] failed with exit code {result.returncode}")
    else:
        print(f"[{label}] ok")
    return result.returncode


def test_all() -> int:
    with tempfile.TemporaryDirectory(prefix="paper-intake-pytest-") as temp_dir:
        intake_test_command = [
            str(project_python(INTAKE)),
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "--basetemp",
            temp_dir,
        ]
        return run_test_commands(intake_test_command)


def run_test_commands(intake_test_command: list[str]) -> int:
    commands = [
        (
            intake_test_command,
            INTAKE,
            "paper-intake tests",
        ),
        (
            [
                str(project_python(RADAR)),
                "-m",
                "unittest",
                "discover",
                "-s",
                "tests",
                "-v",
            ],
            RADAR,
            "trend-radar backend tests",
        ),
        (
            [str(project_python(RADAR)), "-m", "compileall", "-q", "backend"],
            RADAR,
            "trend-radar backend compile",
        ),
        (
            [npm_command(), "run", "build"],
            FRONTEND,
            "trend-radar frontend build",
        ),
    ]
    for command, cwd, label in commands:
        code = run_command(command, cwd, label)
        if code:
            return code
    return 0


def export_tasks(args: argparse.Namespace, capture: bool = False) -> tuple[int, dict[str, Any] | None]:
    command = [
        str(project_python(RADAR)),
        "-m",
        "backend.integration.export_to_downloader",
        "--from",
        args.from_month,
        "--to",
        args.to_month,
        "--scope",
        args.scope,
        "--limit",
        str(args.limit),
    ]
    if args.concept:
        command.extend(["--concept", args.concept])
    if args.concepts:
        command.extend(["--concepts", args.concepts])
    if args.mode:
        command.extend(["--mode", args.mode])
    if args.min_momentum:
        command.extend(["--min-momentum", str(args.min_momentum)])
    if args.include_arxiv:
        command.append("--include-arxiv")
    if args.dry_run:
        command.append("--dry-run")

    result = subprocess.run(
        command,
        cwd=str(RADAR),
        env=child_env(),
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )
    if result.stdout:
        print(result.stdout.rstrip())
    if result.stderr:
        print(result.stderr.rstrip(), file=sys.stderr)
    payload = None
    if result.returncode == 0 and capture:
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError:
            payload = None
    return result.returncode, payload


def import_tasks(path: str | None = None, unselected: bool = False) -> int:
    command = [
        str(project_python(INTAKE)),
        "-m",
        "paper_intake.cli",
        "import-tasks",
    ]
    if path:
        command.append(path)
    if unselected:
        command.append("--unselected")
    return run_command(command, INTAKE, "import trend-radar tasks")


def run_database_command(args: argparse.Namespace) -> int:
    command = [
        str(project_python(INTAKE)),
        "-m",
        "paper_intake.cli",
        args.command,
    ]
    if args.command == "db-restore":
        command.append(args.backup)
    for attribute, flag in (
        ("db", "--db"),
        ("output_dir", "--output-dir"),
        ("backup_dir", "--backup-dir"),
    ):
        value = getattr(args, attribute, None)
        if value:
            command.extend([flag, value])
    if getattr(args, "no_backup", False):
        command.append("--no-backup")
    if getattr(args, "confirm", False):
        command.append("--confirm")
    return run_command(command, INTAKE, args.command)

def run_flow(args: argparse.Namespace) -> int:
    if args.dry_run:
        print("flow does not import dry-run output; remove --dry-run to continue.")
        return 2
    code, payload = export_tasks(args, capture=True)
    if code:
        return code
    task_path = payload.get("json_path") if payload else None
    if not task_path:
        print("Export succeeded but no JSON task path was returned.", file=sys.stderr)
        return 2
    return import_tasks(task_path, unselected=args.unselected)


def port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def radar_api_openapi(port: int) -> dict[str, Any] | None:
    try:
        with urlopen(f"http://127.0.0.1:{port}/openapi.json", timeout=2) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return payload if isinstance(payload, dict) else None
    except (OSError, URLError, json.JSONDecodeError):
        return None


def radar_api_running(port: int) -> bool:
    payload = radar_api_openapi(port) or {}
    return payload.get("info", {}).get("title") == "Condensed Matter Trend Radar"


def radar_api_current(port: int) -> bool:
    payload = radar_api_openapi(port) or {}
    return "/api/library/status" in (payload.get("paths") or {})


def trend_frontend_running(port: int) -> bool:
    try:
        with urlopen(f"http://127.0.0.1:{port}", timeout=2) as response:
            page = response.read().decode("utf-8", errors="replace")
        return "<title>Condensed Matter Trend Radar</title>" in page
    except (OSError, URLError):
        return False
def streamlit_running(port: int) -> bool:
    try:
        with urlopen(f"http://127.0.0.1:{port}/_stcore/health", timeout=2) as response:
            return response.read().decode("utf-8", errors="replace").strip().lower() == "ok"
    except (OSError, URLError):
        return False


def start_services(only: str) -> int:
    env = child_env()
    api_port = configured_port("CONDMAT_RADAR_API_PORT", 8000)
    frontend_port = 5173
    intake_port = configured_port("STREAMLIT_SERVER_PORT", 8501)
    commands: list[tuple[str, list[str], Path]] = []

    if only in {"all", "radar"}:
        if port_open(api_port):
            if radar_api_running(api_port):
                if not radar_api_current(api_port):
                    print(
                        f"Trend Radar API on port {api_port} is running older code; "
                        "stop that process and run this command again."
                    )
                    return 1
                print(f"[reuse] Trend Radar API already runs on port {api_port}.")
            else:
                print(f"Port {api_port} is occupied by another service; radar API was not started.")
                return 1
        else:
            commands.append(
                (
                    "radar-api",
                    [str(project_python(RADAR)), "-m", "backend.api.main"],
                    RADAR,
                )
            )

        if port_open(frontend_port):
            if trend_frontend_running(frontend_port):
                print(f"[reuse] Trend Radar frontend already runs on port {frontend_port}.")
            else:
                print(f"Port {frontend_port} is occupied by another service; radar UI was not started.")
                return 1
        else:
            commands.append(
                (
                    "radar-ui",
                    [npm_command(), "run", "dev"],
                    FRONTEND,
                )
            )

    if only in {"all", "intake"}:
        if port_open(intake_port):
            if streamlit_running(intake_port):
                print(f"[reuse] Paper Intake already runs on port {intake_port}.")
            else:
                print(f"Port {intake_port} is occupied by another service; Paper Intake was not started.")
                return 1
        else:
            commands.append(
                (
                    "paper-intake",
                    [
                        str(project_python(INTAKE)),
                        "-m",
                        "streamlit",
                        "run",
                        "app.py",
                        "--server.headless=true",
                        f"--server.port={intake_port}",
                        "--browser.gatherUsageStats=false",
                    ],
                    INTAKE,
                )
            )

    processes: list[tuple[str, subprocess.Popen[Any]]] = []
    try:
        for label, command, cwd in commands:
            print(f"[start] {label}: {' '.join(command)}", flush=True)
            process = subprocess.Popen(command, cwd=str(cwd), env=env)
            processes.append((label, process))
        if only in {"all", "radar"}:
            print(f"Trend Radar: http://127.0.0.1:{frontend_port}")
        if only in {"all", "intake"}:
            print(f"Legacy Paper Intake (rollback/comparison only): http://127.0.0.1:{intake_port}")
        if not processes:
            print("All requested services are already running.")
            return 0
        print("Press Ctrl+C to stop services.")
        while processes:
            for label, process in processes:
                code = process.poll()
                if code is not None:
                    print(f"[stop] {label} exited with code {code}")
                    return code
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\nStopping services...")
    finally:
        for _, process in processes:
            if process.poll() is None:
                process.send_signal(signal.SIGTERM)
        deadline = time.monotonic() + 8
        for _, process in processes:
            if process.poll() is None:
                try:
                    process.wait(timeout=max(0.1, deadline - time.monotonic()))
                except subprocess.TimeoutExpired:
                    process.kill()
    return 0

def add_export_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--concept", default="")
    parser.add_argument("--concepts", default="")
    parser.add_argument("--mode", choices=["or", "and"], default="or")
    parser.add_argument("--scope", choices=["core", "core_context", "all"], default="core")
    parser.add_argument("--from", dest="from_month", default="2015-01")
    parser.add_argument("--to", dest="to_month", default=time.strftime("%Y-%m"))
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--min-momentum", type=float, default=0.0)
    parser.add_argument("--include-arxiv", action="store_true")
    parser.add_argument("--dry-run", action="store_true")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Unified controller for the local paper workspace.")
    subparsers = parser.add_subparsers(dest="command")

    status_parser = subparsers.add_parser("status", help="Show project and integration status.")
    status_parser.add_argument("--json", action="store_true", dest="as_json")
    subparsers.add_parser("doctor", help="Check local runtime and configuration.")
    subparsers.add_parser("test", help="Run all local validation commands.")

    start_parser = subparsers.add_parser("start", help="Start the unified Radar app; Intake is legacy-only.")
    start_parser.add_argument("--only", choices=["radar", "intake", "all"], default="radar")

    export_parser = subparsers.add_parser("export-tasks", help="Export radar papers to the intake inbox.")
    add_export_arguments(export_parser)

    import_parser = subparsers.add_parser("import-tasks", help="Import the newest or named task file.")
    import_parser.add_argument("path", nargs="?")
    import_parser.add_argument("--unselected", action="store_true")

    db_status_parser = subparsers.add_parser("db-status", help="Show both database health reports.")
    db_status_parser.add_argument("--db", help="Override the Intake database path.")
    db_status_parser.add_argument("--radar-db", help="Override the Radar database path.")
    db_status_parser.add_argument(
        "--full",
        action="store_true",
        help="Run Radar quick_check and foreign-key verification (can take longer).",
    )

    db_backup_parser = subparsers.add_parser("db-backup", help="Create and verify an Intake database backup.")
    db_backup_parser.add_argument("--db")
    db_backup_parser.add_argument("--output-dir")

    db_migrate_parser = subparsers.add_parser("db-migrate", help="Back up and migrate the Intake database.")
    db_migrate_parser.add_argument("--db")
    db_migrate_parser.add_argument("--backup-dir")
    db_migrate_parser.add_argument("--no-backup", action="store_true")

    db_restore_parser = subparsers.add_parser("db-restore", help="Restore a verified Intake database backup.")
    db_restore_parser.add_argument("backup")
    db_restore_parser.add_argument("--db")
    db_restore_parser.add_argument("--confirm", action="store_true")

    flow_parser = subparsers.add_parser("flow", help="Export radar papers and immediately import them.")
    add_export_arguments(flow_parser)
    flow_parser.add_argument("--unselected", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    command = args.command or "status"
    if command == "status":
        status = build_status()
        if getattr(args, "as_json", False):
            print(json.dumps(status, ensure_ascii=False, indent=2))
        else:
            print_status(status)
        return 0
    if command == "doctor":
        return doctor()
    if command == "test":
        return test_all()
    if command == "start":
        return start_services(args.only)
    if command == "export-tasks":
        return export_tasks(args)[0]
    if command == "import-tasks":
        return import_tasks(args.path, args.unselected)
    if command == "db-status":
        return show_database_status(args)
    if command in {"db-backup", "db-migrate", "db-restore"}:
        return run_database_command(args)
    if command == "flow":
        return run_flow(args)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
