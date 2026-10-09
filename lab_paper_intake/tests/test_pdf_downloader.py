import sqlite3

from paper_intake.config import Settings
from paper_intake.db import upsert_papers
from paper_intake.models import Paper
from paper_intake import pdf_downloader


class FakeResponse:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def raise_for_status(self):
        return None

    def iter_bytes(self):
        yield b"%PDF-1.4\n" + (b"x" * 256)


def test_pdf_download_success_updates_status_and_db(tmp_path, monkeypatch):
    monkeypatch.setattr(pdf_downloader.httpx, "stream", lambda *args, **kwargs: FakeResponse())
    db_path = tmp_path / "papers.db"
    pdf_dir = tmp_path / "pdfs"
    settings = Settings(None, "https://api.deepseek.com", "", None)
    paper = Paper(title="Downloadable", pdf_url="https://example.org/paper.pdf")
    upsert_papers([paper], db_path)

    updated = pdf_downloader.download_pdf(paper, settings, pdf_dir=pdf_dir, db_path=db_path)

    assert updated.pdf_status == "downloaded"
    assert updated.local_pdf_path
    assert (pdf_dir / updated.local_pdf_path.split("\\")[-1]).exists()
    with sqlite3.connect(db_path) as conn:
        paper_row = conn.execute(
            "SELECT pdf_status, local_pdf_path FROM papers WHERE id = ?", (paper.id,)
        ).fetchone()
        download_row = conn.execute(
            "SELECT status, local_path FROM downloaded_pdfs WHERE paper_id = ?", (paper.id,)
        ).fetchone()
    assert paper_row == ("downloaded", updated.local_pdf_path)
    assert download_row == ("downloaded", updated.local_pdf_path)

def test_upsert_downloaded_status_merges_duplicate_paper_rows(tmp_path):
    from paper_intake.db import upsert_papers

    db_path = tmp_path / "papers.db"
    old = Paper(
        id="old-id",
        title="Same DOI Paper",
        doi="10.1000/same",
        pdf_url="https://example.org/paper.pdf",
        pdf_status="oa_available",
    )
    downloaded = Paper(
        id="new-id",
        title="Same DOI Paper",
        doi="10.1000/same",
        pdf_url="https://example.org/paper.pdf",
        pdf_status="downloaded",
        local_pdf_path="pdfs/01_same.pdf",
    )

    upsert_papers([old], db_path)
    upsert_papers([downloaded], db_path)

    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT id, pdf_status, local_pdf_path FROM papers WHERE doi = ? ORDER BY id",
            ("10.1000/same",),
        ).fetchall()

    assert rows == [("old-id", "downloaded", "pdfs/01_same.pdf")]
    with sqlite3.connect(db_path) as conn:
        alias = conn.execute(
            "SELECT paper_id FROM paper_aliases WHERE alias_id = ?", ("new-id",)
        ).fetchone()
    assert alias == ("old-id",)