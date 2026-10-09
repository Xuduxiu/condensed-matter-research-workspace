from paper_intake.exporters.bibtex_exporter import papers_to_bibtex
from paper_intake.models import Paper


def test_bibtex_uses_expected_key_and_article_entry():
    paper = Paper(
        title="Dry Electrode Preparation for ZrTe5 Devices",
        authors=["Jane Smith", "Alan Turing"],
        year=2024,
        journal="Applied Materials",
        doi="10.1000/test",
    )

    output = papers_to_bibtex([paper])

    assert "@article{smith_2024_dry" in output
    assert "author = {Jane Smith and Alan Turing}" in output
    assert "doi = {10.1000/test}" in output


def test_bibtex_keys_are_unique():
    paper = Paper(title="Same Title", authors=["Jane Smith"], year=2024)

    output = papers_to_bibtex([paper, paper])

    assert "smith_2024_same," in output
    assert "smith_2024_same_2," in output


def test_bibtex_includes_arxiv_and_relative_file_without_duplicates():
    paper = Paper(
        title="Origin of the quasi-quantized Hall effect in ZrTe5",
        authors=["Galeski, Stanislaw", "Galeski, Stanislaw", "S. Galeski"],
        year=2021,
        arxiv_id="2101.12345",
        local_pdf_path="pdfs/01_galeski_2021_origin.pdf",
    )

    output = papers_to_bibtex([paper])

    assert "eprint = {2101.12345}" in output
    assert "archivePrefix = {arXiv}" in output
    assert "file = {pdfs/01_galeski_2021_origin.pdf}" in output
    assert output.count("Galeski") == 1
    assert "None" not in output
    assert "nan" not in output.lower()