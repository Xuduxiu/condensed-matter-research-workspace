from paper_intake.dedup import deduplicate_papers
from paper_intake.models import Paper


def test_deduplicates_by_doi_and_keeps_more_complete_metadata():
    papers = [
        Paper(title="Short title", doi="https://doi.org/10.1234/example"),
        Paper(
            title="Short title",
            doi="10.1234/example",
            authors=["Ada Lovelace"],
            abstract="Detailed abstract",
            journal="Journal",
            year=2025,
        ),
    ]

    result = deduplicate_papers(papers)

    assert len(result) == 1
    assert result[0].doi == "10.1234/example"
    assert result[0].abstract == "Detailed abstract"
    assert result[0].authors == ["Ada Lovelace"]


def test_deduplicates_by_fuzzy_normalized_title():
    papers = [
        Paper(title="Air sensitive ZrTe5 device fabrication for transport"),
        Paper(title="Air-sensitive ZrTe5 device fabrication for transport"),
    ]

    result = deduplicate_papers(papers)

    assert len(result) == 1


def test_merge_authors_uses_best_source_without_doubling():
    papers = [
        Paper(
            title="Origin of the quasi-quantized Hall effect in ZrTe5",
            doi="10.1000/zrte5",
            source="arXiv",
            authors=["S. Galeski", "A. Markou", "C. Felser"],
        ),
        Paper(
            title="Origin of the quasi-quantized Hall effect in ZrTe5",
            doi="10.1000/zrte5",
            source="Crossref",
            authors=["Galeski, Stanislaw", "Markou, Anna", "Felser, Claudia"],
            journal="Nature Communications",
        ),
    ]

    result = deduplicate_papers(papers)

    assert len(result) == 1
    assert result[0].authors == [
        "Galeski, Stanislaw",
        "Markou, Anna",
        "Felser, Claudia",
    ]
    assert len(result[0].authors) == 3