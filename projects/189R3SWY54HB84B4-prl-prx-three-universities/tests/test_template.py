from pathlib import Path
import csv
import unittest

ROOT=Path(__file__).resolve().parents[1]

class TestInitialAuditTemplate(unittest.TestCase):
    def test_66_records_are_uncollected(self):
        with (ROOT / "data/annual_audit_template_2016_2026.csv").open(encoding="utf-8-sig", newline="") as fh:
            rows=list(csv.DictReader(fh))
        self.assertEqual(len(rows),66)
        self.assertEqual(len({(r["year"],r["university"],r["journal"]) for r in rows}),66)
        self.assertEqual({int(r["year"]) for r in rows},set(range(2016,2027)))
        self.assertEqual(sum(r["year"]=="2026" for r in rows),6)
        for r in rows:
            for f in ("participation_count","corresponding_count","unknown_corr_count"):
                self.assertEqual(r[f],"")
            self.assertEqual(r["data_status"],"NOT_COLLECTED_2026_YTD" if r["year"]=="2026" else "NOT_COLLECTED")
            self.assertEqual(r["as_of_date"],"2026-11-06" if r["year"]=="2026" else "")

if __name__=="__main__": unittest.main()
