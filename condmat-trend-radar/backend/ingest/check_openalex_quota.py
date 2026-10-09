from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any

from backend.config import logs_dir, mask_secret, openalex_api_key, openalex_mailto
from backend.ingest.openalex_client import OPENALEX_BASE, redact_params


def _first_present(payload: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in payload:
            return payload[key]
    meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
    for key in keys:
        if key in meta:
            return meta[key]
    return None


def _write_probe_log(result: dict[str, Any]) -> str:
    directory = logs_dir() / "openalex_quota"
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    path = directory / f"openalex_quota_{stamp}.json"
    safe = dict(result)
    if isinstance(safe.get("request_params"), dict):
        safe["request_params"] = redact_params(safe["request_params"])
    path.write_text(json.dumps(safe, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(path)


def check_quota(timeout: int = 15) -> dict[str, Any]:
    api_key = openalex_api_key()
    if not api_key:
        result = {"ok": False, "message": "OPENALEX_API_KEY not set.", "api_key_masked": "", "remaining_credits": None, "used_credits": None, "reset_time": None}
        result["log_path"] = _write_probe_log(result)
        return result
    params = {"api_key": api_key, "per-page": "1"}
    mailto = openalex_mailto()
    if mailto:
        params["mailto"] = mailto
    url = f"{OPENALEX_BASE}/works?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(url, headers={"User-Agent": "condmat-trend-radar/0.1", "Accept": "application/json"})
    result: dict[str, Any]
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
            headers = {key: value for key, value in response.headers.items() if key.lower().startswith(("x-", "ratelimit", "retry"))}
        remaining = _first_present(payload, "remaining", "remaining_credits", "dailyRemainingUsd", "daily_remaining", "requests_remaining")
        used = _first_present(payload, "used", "used_credits", "dailyUsedUsd", "daily_used", "requests_used")
        reset = _first_present(payload, "reset", "reset_time", "resetAt", "resets_at")
        result = {
            "ok": True,
            "status": 200,
            "api_key_masked": mask_secret(api_key),
            "remaining_credits": remaining,
            "used_credits": used,
            "reset_time": reset,
            "headers": headers,
            "meta": payload.get("meta"),
            "request_params": redact_params(params),
        }
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:1000]
        result = {"ok": False, "status": exc.code, "message": f"OpenAlex probe HTTP {exc.code}", "api_key_masked": mask_secret(api_key), "raw_error_first_1000": body, "request_params": redact_params(params)}
    except Exception as exc:
        result = {"ok": False, "message": f"OpenAlex probe failed: {type(exc).__name__}: {exc}", "api_key_masked": mask_secret(api_key), "request_params": redact_params(params)}
    result["log_path"] = _write_probe_log(result)
    return result


def main() -> None:
    print(json.dumps(check_quota(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()