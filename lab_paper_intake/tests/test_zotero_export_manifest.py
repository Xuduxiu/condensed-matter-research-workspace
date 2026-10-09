import json
from pathlib import Path

from paper_intake.config import Settings
from paper_intake.export_package import export_selected_package
from paper_intake.models import Paper


def test_export_manifest_records_ris_manual_zotero_import_mode(tmp_path: Path):
    exports_dir = tmp_path / "exports"
    db_path = tmp_path / "papers.db"
    paper = Paper(title="ZrTe5 paper", authors=["Jane Smith"], doi="10.1000/zotero")

    export_result = export_selected_package(
        [paper],
        Settings(None, "https://api.deepseek.com", "", None),
        exports_dir=exports_dir,
        db_path=db_path,
    )

    manifest = json.loads(export_result.manifest_path.read_text(encoding="utf-8"))
    assert manifest["zotero_import"]["mode"] == "ris_manual"
    assert manifest["zotero_import"]["ris_file"] == "selected_papers.ris"
    assert manifest["zotero_import"]["summary_file"] == "selected_summary.md"
    assert manifest["zotero_import"]["pdf_dir"] == "pdfs/"
    assert "connection checks" in manifest["zotero_import"]["reason"]
    assert "collection_key" not in manifest["zotero_import"]
    assert "created" not in manifest["zotero_import"]
    assert export_result.ris_path.exists()
    assert export_result.markdown_path.exists()
    assert (export_result.export_dir / "pdfs").is_dir()