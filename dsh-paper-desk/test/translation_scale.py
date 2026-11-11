"""Exercise the 100,000-version enrollment path in a disposable SQLite library, without inference."""

from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
from time import perf_counter

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "condmat-trend-radar"))
from backend.db.database import init_db
from backend.library.translations import ensure_translation_schema, estimate, set_automatic
from backend.migrations.unified_library import apply_unified_schema


def main():
    total = 100_000
    abstract = "This is a synthetic record for a local queue throughput test. " * 9
    with TemporaryDirectory(prefix="paper-translation-scale-") as tmp:
        with closing(sqlite3.connect(Path(tmp) / "fixture.sqlite")) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            init_db(db)
            apply_unified_schema(db)
            ensure_translation_schema(db)
            db.execute("INSERT INTO papers(id,title,data_mode) VALUES ('fixture','Queue benchmark','real')")
            db.executemany("""INSERT INTO paper_versions(id,canonical_paper_id,version_type,title,
                abstract,source,source_record_id,first_seen_at,last_seen_at)
                VALUES (?,'fixture','preprint',?,?,'fixture',?,'2026-01-01','2026-01-01')""",
                ((str(i), f"Fixture title {i // 2}", f"{abstract}{i // 2}" if (i // 2) % 5 else "", str(i))
                 for i in range(total)))
            db.commit()
            start = perf_counter()
            plan = estimate(db, "all", [], total, 8, 16000)
            assert plan["total"] == total and plan["unique_inputs"] == total // 2
            estimated = perf_counter()
            with db:
                state = set_automatic(db, True, 8, 16000)
            seeded = perf_counter()
            with db:
                again = set_automatic(db, True, 8, 16000)
            assert state["total"] == again["total"] == total
            assert db.execute("SELECT COUNT(*) FROM translation_work").fetchone()[0] == total // 2
            assert db.execute("SELECT COUNT(*) FROM translation_jobs").fetchone()[0] == 1
            db.execute("UPDATE paper_versions SET abstract='A corrected abstract.' WHERE id='0'")
            assert db.execute("SELECT version_id FROM translation_dirty").fetchall()[0][0] == '0'
            assert db.execute("SELECT COUNT(*) FROM translation_usage").fetchone()[0] == 0
            print(json.dumps({"versions": total, "distinct_texts": total // 2, "estimate_s": round(estimated-start, 2),
                              "seed_s": round(seeded-estimated, 2), "idempotent": True,
                              "incremental_update_detected": True, "model_calls": 0}))


if __name__ == "__main__":
    main()
