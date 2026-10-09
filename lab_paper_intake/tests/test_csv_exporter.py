import pandas as pd

from paper_intake.exporters.csv_exporter import export_csv
from paper_intake.models import ChineseSummary, Paper


def test_csv_export_contains_local_pdf_path_and_summary_status(tmp_path):
    paper = Paper(
        title="Downloaded paper",
        pdf_status="downloaded",
        local_pdf_path="pdfs/01_test.pdf",
        chinese_summary=ChineseSummary(problem="研究问题"),
    )
    path = export_csv([paper], tmp_path / "selected_papers.csv")

    frame = pd.read_csv(path)

    assert "local_pdf_path" in frame.columns
    assert "summary_status" in frame.columns
    assert frame.loc[0, "local_pdf_path"] == "pdfs/01_test.pdf"
    assert frame.loc[0, "pdf_status"] == "downloaded"
    assert frame.loc[0, "summary_status"] == "llm_generated"