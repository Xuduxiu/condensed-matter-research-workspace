from paper_intake.exporters.ris_exporter import papers_to_ris
from paper_intake.models import ChineseSummary, Paper


def test_ris_contains_zotero_friendly_fields():
    paper = Paper(
        title="Low Temperature &amp; Transport",
        authors=["Smith, Jane", "Jane Smith", "Alan Turing"],
        year=2023,
        journal="Physics Letters",
        doi="10.1000/ris",
        url="https://example.org/item",
        pdf_url="https://example.org/paper.pdf",
        local_pdf_path="pdfs/01_smith_2023_low.pdf",
        pdf_status="downloaded",
        abstract="A useful &amp; decoded abstract",
        relevance_score=8.5,
        relevance_reason="Matches &amp; transport devices",
        chinese_summary=ChineseSummary(
            problem="研究问题",
            method="实验方法",
            key_results="关键结果",
            relation_to_lab="相关",
            reading_priority="High",
        ),
        tags=["transport", "device", "transport"],
    )

    output = papers_to_ris([paper])

    assert "TY  - JOUR" in output
    assert output.count("AU  -") == 2
    assert "AU  - Smith, Jane" in output
    assert "AU  - Alan Turing" in output
    assert "TI  - Low Temperature & Transport" in output
    assert "DO  - 10.1000/ris" in output
    assert "UR  - https://example.org/item" in output
    assert "L1  - pdfs/01_smith_2023_low.pdf" in output
    assert "AB  - A useful & decoded abstract" in output
    assert output.count("KW  - transport") == 1
    assert "KW  - device" in output
    assert "N1  - 中文摘要：" in output
    assert "N1  - 相关性评分：8.5" in output
    assert "N1  - 相关性理由：Matches & transport devices" in output
    assert "N1  - 阅读优先级：High" in output
    assert "N1  - 本地 PDF：pdfs/01_smith_2023_low.pdf" in output
    assert "None" not in output
    assert "nan" not in output.lower()
    assert "&amp;" not in output
    assert output.strip().endswith("ER  -")


def test_ris_omits_missing_optional_fields():
    paper = Paper(title="Metadata Only")

    output = papers_to_ris([paper])

    assert "DO  -" not in output
    assert "AB  -" not in output
    assert "L1  -" not in output
    assert "None" not in output
    assert "nan" not in output.lower()