from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from backend.db.database import init_db
from backend.library.material_discovery import sync_materials_from_papers
from backend.library.repository import LibraryRepository
from backend.library.workbench import ensure_workbench_schema, get_live_scan_settings, set_live_scan_settings
from backend.migrations.unified_library import apply_unified_schema
from backend.nlp.material_extract import extract_materials, valid_dynamic_formula


def connection_for(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    init_db(connection)
    apply_unified_schema(connection)
    ensure_workbench_schema(connection)
    connection.commit()
    return connection


class DynamicMaterialTests(unittest.TestCase):
    def test_formula_detector_is_dynamic_and_rejects_method_acronyms(self) -> None:
        items = extract_materials(
            "Magnetic phases of CrGeTe3 and Fe3GeTe2",
            "ARPES and STM reveal a transition in GaAs heterostructures.",
        )
        names = {item["normalized_term"] for item in items}
        self.assertTrue(valid_dynamic_formula("CrGeTe3"))
        self.assertIn("CrGeTe3", names)
        self.assertIn("Fe3GeTe2", names)
        self.assertIn("GaAs", names)
        self.assertNotIn("ARPES", names)
        self.assertNotIn("STM", names)

    def test_formula_markup_is_flattened_without_partial_materials(self) -> None:
        items = extract_materials(
            '<mml:math><mml:msub><mml:mi>SrTiO</mml:mi><mml:mn>3</mml:mn></mml:msub></mml:math>',
            r'${\mathrm{LaCoO}}_{3}$ and MoSe$_2$; Bi 2 Se 3 and MnBi 2 Te 4; FeSCs, CISS and CNTs are acronyms.',
        )
        names = {item["normalized_term"] for item in items}
        self.assertIn("SrTiO3", names)
        self.assertIn("LaCoO3", names)
        self.assertIn("MoSe2", names)
        self.assertIn("Bi2Se3", names)
        self.assertIn("MnBi2Te4", names)
        self.assertNotIn("SrTiO", names)
        self.assertNotIn("MoSe", names)
        self.assertNotIn("MnBi", names)
        self.assertNotIn("FeSCs", names)
        self.assertNotIn("CISS", names)
        self.assertNotIn("CNTs", names)

    def test_polymorph_suffixes_from_production_abstract_keep_base_formula(self) -> None:
        abstract = (
            "Focusing on the AgBeF4 stoichiometry, we identify the five lowest "
            "enthalpy polymorphs. All polymorphs show an antiferromagnetic ground "
            "state, with AgBeF4_4 and AgBeF4_5 exhibiting unprecedented strong "
            "superexchange interactions. [Ag2F7] occurs for AgBeF4_4."
        )
        names = {item["normalized_term"] for item in extract_materials("", abstract)}
        self.assertIn("AgBeF4", names)
        self.assertNotIn("AgBeF44", names)
        self.assertNotIn("AgBeF45", names)

    def test_polymorph_and_true_subscripts_in_latex_and_mathml(self) -> None:
        title = (
            "<mml:math><mml:msub><mml:mi>AgBeF4</mml:mi><mml:mn>4</mml:mn>"
            "</mml:msub></mml:math> and "
            "<mml:math><mml:msub><mml:mi>SrTiO</mml:mi><mml:mn>3</mml:mn>"
            "</mml:msub></mml:math>"
        )
        abstract = (
            r"${\mathrm{AgBeF4}}_{5}$ and ${\mathrm{SrTiO}}_{3}$; "
            r"Bi_2Te_3 remains a true multi-subscript formula."
        )
        names = {item["normalized_term"] for item in extract_materials(title, abstract)}
        self.assertIn("AgBeF4", names)
        self.assertIn("SrTiO3", names)
        self.assertIn("Bi2Te3", names)
        self.assertNotIn("AgBeF44", names)
        self.assertNotIn("AgBeF45", names)

    def test_materials_are_created_from_paper_text_with_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            connection = connection_for(Path(directory) / "radar.sqlite")
            paper = LibraryRepository(connection).upsert_version(
                {
                    "title": "Magnetism in CrGeTe3",
                    "abstract": "We compare CrGeTe3 with Fe3GeTe2 thin crystals.",
                    "publication_date": "2026-07-15",
                },
                source="manual",
                source_record_id="dynamic-material",
                default_condmat_eligible=True,
            )
            result = sync_materials_from_papers(connection, paper_ids=[paper.canonical_paper_id])
            connection.commit()
            names = {row[0] for row in connection.execute("SELECT canonical_name FROM materials")}
            sources = {row[0] for row in connection.execute("SELECT source FROM paper_materials")}
            context = connection.execute("SELECT context_json FROM paper_materials WHERE source='paper_text_formula' LIMIT 1").fetchone()
            self.assertGreaterEqual(result["materials_discovered"], 2)
            self.assertIn("CrGeTe3", names)
            self.assertIn("Fe3GeTe2", names)
            self.assertIn("paper_text_formula", sources)
            self.assertIn("derived_from_paper_text", context[0])
            connection.close()


class LiveScanSettingsTests(unittest.TestCase):
    def test_live_scan_settings_are_persistent_and_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            connection = connection_for(Path(directory) / "radar.sqlite")
            initial = get_live_scan_settings(connection)
            updated = set_live_scan_settings(connection, enabled=False, interval_seconds=60, abstract_backfill_limit=99)
            connection.commit()
            migration = connection.execute("SELECT name FROM workbench_schema_migrations ORDER BY version DESC LIMIT 1").fetchone()[0]
            self.assertTrue(initial["enabled"])
            self.assertFalse(updated["enabled"])
            self.assertEqual(updated["interval_seconds"], 300)
            self.assertEqual(updated["abstract_backfill_limit"], 25)
            self.assertEqual(migration, "radar_dashboard_material_index_v5")
            connection.close()


if __name__ == "__main__":
    unittest.main()
