import json
import zipfile
from pathlib import Path

import pandas as pd

from paper_intake.config import Settings
from paper_intake.export_package import (
    clean_export_runs,
    export_selected_package,
    safe_pdf_filename,
)
from paper_intake.models import ChineseSummary, Paper


def _settings() -> Settings:
    return Settings(None, "https://api.deepseek.com", "", None)


def _paper(tmp_path: Path) -> Paper:
    existing_pdf = tmp_path / "existing.pdf"
    existing_pdf.write_bytes(b"%PDF-1.4\n" + b"x" * 256)
    return Paper(
        title="Dry electrode fabrication for ZrTe5 transport devices",
        authors=["Jane Smith"],
        year=2025,
        doi="10.1000/package",
        pdf_url="https://example.org/package.pdf",
        pdf_status="downloaded",
        local_pdf_path=str(existing_pdf),
        relevance_score=8.0,
        relevance_reason="Relevant",
        tags=["ZrTe5"],
        chinese_summary=ChineseSummary(problem="研究问题", reading_priority="High"),
        source="OpenAlex",
    )


def test_export_package_creates_single_run_folder_manifest_zip_and_relative_paths(tmp_path):
    exports_dir = tmp_path / "exports"
    db_path = tmp_path / "papers.db"
    paper = _paper(tmp_path)

    result = export_selected_package(
        [paper],
        settings=_settings(),
        prompt="prompt",
        language="zh",
        download_pdfs=False,
        exports_dir=exports_dir,
        db_path=db_path,
    )

    assert result.export_dir.exists()
    assert result.csv_path.parent == result.export_dir
    assert result.bibtex_path.parent == result.export_dir
    assert result.ris_path.parent == result.export_dir
    assert result.markdown_path.parent == result.export_dir
    assert result.manifest_path.parent == result.export_dir
    assert result.zip_path.exists()
    assert (result.export_dir / "pdfs").is_dir()
    copied_pdf = result.export_dir / result.papers[0].local_pdf_path
    assert copied_pdf.exists()
    assert result.papers[0].local_pdf_path.startswith("pdfs/")

    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert manifest["export_id"] == result.export_id
    assert manifest["selected_count"] == 1
    assert manifest["files"]["csv"] == "selected_papers.csv"
    assert manifest["zotero_import"]["mode"] == "ris_manual"
    assert manifest["zotero_import"]["ris_file"] == "selected_papers.ris"
    assert manifest["zotero_import"]["summary_file"] == "selected_summary.md"
    assert manifest["zotero_import"]["pdf_dir"] == "pdfs/"
    assert manifest["papers"][0]["pdf_status"] == "downloaded"
    assert manifest["papers"][0]["pdf_file"] == result.papers[0].local_pdf_path

    frame = pd.read_csv(result.csv_path)
    assert frame.loc[0, "local_pdf_path"] == result.papers[0].local_pdf_path
    assert frame.loc[0, "pdf_status"] == "downloaded"
    assert result.papers[0].local_pdf_path in result.bibtex_path.read_text(encoding="utf-8")
    assert f"L1  - {result.papers[0].local_pdf_path}" in result.ris_path.read_text(encoding="utf-8")
    assert f"本地路径：{result.papers[0].local_pdf_path}" in result.markdown_path.read_text(encoding="utf-8")

    with zipfile.ZipFile(result.zip_path) as archive:
        names = set(archive.namelist())
    assert "selected_papers.csv" in names
    assert "manifest.json" in names
    assert result.papers[0].local_pdf_path in names


def test_repeated_export_creates_new_run_folder(tmp_path):
    exports_dir = tmp_path / "exports"
    db_path = tmp_path / "papers.db"
    paper = _paper(tmp_path)

    first = export_selected_package([paper], _settings(), exports_dir=exports_dir, db_path=db_path)
    second = export_selected_package(first.papers, _settings(), exports_dir=exports_dir, db_path=db_path)

    assert first.export_dir != second.export_dir
    assert first.export_dir.exists()
    assert second.export_dir.exists()
    assert (second.export_dir / second.papers[0].local_pdf_path).exists()
    assert second.papers[0].local_pdf_path.startswith("pdfs/")


def test_safe_pdf_filename_removes_windows_illegal_characters():
    paper = Paper(title='A bad: title / with * invalid ? chars', authors=[], year=2025)

    filename = safe_pdf_filename(1, paper)

    assert filename.startswith("01_unknown_2025_")
    assert not any(char in filename for char in '<>:"/\\|?*')
    assert len(filename) <= 120


def test_clean_export_runs_keeps_non_export_files(tmp_path):
    exports_dir = tmp_path / "exports"
    exports_dir.mkdir()
    (exports_dir / ".gitkeep").write_text("", encoding="utf-8")
    (exports_dir / "run_0001_20260705_031200").mkdir()
    (exports_dir / "run_0001_20260705_031200.zip").write_bytes(b"zip")
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    env_path = tmp_path / ".env"
    db_path = data_dir / "papers.db"
    env_path.write_text("SECRET=keep", encoding="utf-8")
    db_path.write_text("db", encoding="utf-8")

    removed = clean_export_runs(exports_dir)

    assert len(removed) == 2
    assert (exports_dir / ".gitkeep").exists()
    assert env_path.exists()
    assert db_path.exists()
    assert not (exports_dir / "run_0001_20260705_031200").exists()