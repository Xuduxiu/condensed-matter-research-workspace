from __future__ import annotations

import json

from backend.config import logs_dir
from backend.ingest.openalex_client import OpenAlexClient, OpenAlexHTTPError


def run() -> dict[str, object]:
    client = OpenAlexClient(timeout=5, max_retries=0)
    try:
        source_id = client.find_source_id("Physical Review Letters")
        if not source_id:
            return {"can_reach_openalex": True, "sample_works_count": 0, "sample_title": None, "error": "source_not_found"}
        payload = client.fetch_journal_page(
            "Physical Review Letters",
            source_id,
            from_date="2024-01-01",
            to_date="2024-01-31",
            per_page=1,
            cursor="*",
        )
        results = payload.get("results", [])
        title = (results[0].get("title") if results else None)
        return {
            "can_reach_openalex": True,
            "sample_works_count": int(payload.get("meta", {}).get("count") or len(results)),
            "sample_title": title,
            "error": None,
        }
    except OpenAlexHTTPError as exc:
        return {
            "can_reach_openalex": False,
            "sample_works_count": 0,
            "sample_title": None,
            "error": exc.to_dict(),
            "log_dir": str(logs_dir()),
        }
    except Exception as exc:
        return {
            "can_reach_openalex": False,
            "sample_works_count": 0,
            "sample_title": None,
            "error": {"type": type(exc).__name__, "message": str(exc)},
            "log_dir": str(logs_dir()),
        }


def main() -> None:
    print(json.dumps(run(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
