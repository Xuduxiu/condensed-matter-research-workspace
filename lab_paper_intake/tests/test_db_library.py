from paper_intake.db import count_papers, fetch_papers, upsert_papers
from paper_intake.models import Paper


def test_count_and_restore_local_library(tmp_path):
    db_path = tmp_path / "papers.db"

    assert count_papers(db_path) == 0
    upsert_papers(
        [
            Paper(id="library:one", title="First saved paper", relevance_score=7.0),
            Paper(id="library:two", title="Second saved paper", relevance_score=9.0),
        ],
        db_path,
    )

    assert count_papers(db_path) == 2
    restored = fetch_papers(db_path)
    assert [paper.id for paper in restored] == ["library:two", "library:one"]
