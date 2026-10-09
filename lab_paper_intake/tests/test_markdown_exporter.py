from paper_intake.exporters.markdown_exporter import papers_to_markdown
from paper_intake.models import ChineseSummary, Paper


def test_markdown_chinese_export_uses_chinese_field_names():
    paper = Paper(
        title="中文字段测试",
        authors=["Jane Smith"],
        year=2024,
        journal="Journal",
        doi="10.1000/md",
        pdf_status="downloaded",
        local_pdf_path="pdfs/01_smith_2024_test.pdf",
        relevance_score=9.0,
        relevance_reason="高度相关",
        tags=["ZrTe5"],
        chinese_summary=ChineseSummary(
            problem="研究问题",
            method="方法",
            key_results="关键结果",
            relation_to_lab="实验室关系",
            reading_priority="High",
        ),
    )

    output = papers_to_markdown([paper], language="zh")

    assert "# Zotero 导入说明" in output
    assert "selected_papers.ris" in output
    assert "Local API 仅用于连接检测" in output
    assert "# 选中文献摘要" in output
    assert "## 高优先级" in output
    assert "- 相关性评分：9.0" in output
    assert "- 年份：2024" in output
    assert "- 作者：Jane Smith" in output
    assert "- PDF：已下载，本地路径：pdfs/01_smith_2024_test.pdf" in output
    assert "- 研究问题：研究问题" in output
    assert "- 与实验室课题关系：实验室关系" in output


def test_markdown_open_access_pdf_line_when_not_downloaded():
    paper = Paper(
        title="OA paper",
        pdf_status="oa_available",
        pdf_url="https://example.org/oa.pdf",
    )

    output = papers_to_markdown([paper], language="zh")

    assert "PDF：开放获取可用，链接：https://example.org/oa.pdf" in output