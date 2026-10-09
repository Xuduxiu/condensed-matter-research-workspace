from paper_intake.models import ChineseSummary, Paper
from paper_intake.zotero.mapper import build_zotero_note_html, paper_to_zotero_item


def test_paper_to_zotero_item_maps_fields_tags_and_authors_without_duplicates():
    paper = Paper(
        title="Dry electrode preparation for ZrTe5",
        authors=["Smith, Jane", "Jane Smith", "Alan Turing"],
        year=2025,
        journal="Applied Materials",
        doi="10.1000/test",
        abstract="abstract",
        url="https://example.org/item",
        tags=["ZrTe5", "ZrTe5"],
        chinese_summary=ChineseSummary(problem="问题"),
    )

    item = paper_to_zotero_item(paper, collection_key="COLL1")

    assert item["itemType"] == "journalArticle"
    assert item["title"] == paper.title
    assert item["abstractNote"] == "abstract"
    assert item["date"] == "2025"
    assert item["publicationTitle"] == "Applied Materials"
    assert item["DOI"] == "10.1000/test"
    assert item["collections"] == ["COLL1"]
    assert item["creators"] == [
        {"firstName": "Jane", "lastName": "Smith", "creatorType": "author"},
        {"firstName": "Alan", "lastName": "Turing", "creatorType": "author"},
    ]
    tag_names = [tag["tag"] for tag in item["tags"]]
    assert tag_names == ["ZrTe5", "Lab Paper Intake", "LLM Summary"]


def test_paper_to_zotero_item_handles_missing_doi_and_title():
    paper = Paper(title="Untitled preprint", arxiv_id="2401.12345", authors=["Team"])

    item = paper_to_zotero_item(paper)

    assert item["itemType"] == "preprint"
    assert item["archiveID"] == "2401.12345"
    assert "DOI" not in item
    assert "None" not in str(item)
    assert "nan" not in str(item).lower()


def test_zotero_note_html_escapes_text_and_contains_summary_sections():
    paper = Paper(
        title="Unsafe",
        doi="10.1/x",
        pdf_url="https://example.org/a?x=<bad>",
        local_pdf_path="pdfs/01_file.pdf",
        relevance_score=8.5,
        relevance_reason="<script>alert(1)</script>",
        chinese_summary=ChineseSummary(
            problem="<b>problem</b>",
            method="method",
            key_results="results",
            relation_to_lab="relation",
            reading_priority="High",
        ),
    )

    html = build_zotero_note_html(paper, export_id="run_0001")

    assert "<h2>Lab Paper Intake 中文摘要</h2>" in html
    assert "<h3>研究问题</h3>" in html
    assert "&lt;b&gt;problem&lt;/b&gt;" in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "Export ID：run_0001" in html


def test_zotero_note_html_has_no_summary_fallback():
    paper = Paper(title="Metadata only")

    html = build_zotero_note_html(paper)

    assert "未生成中文摘要。" in html
