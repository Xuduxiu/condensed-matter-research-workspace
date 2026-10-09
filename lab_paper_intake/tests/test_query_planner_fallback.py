from paper_intake.config import Settings
from paper_intake.query_planner import fallback_plan, plan_queries


def test_fallback_plan_generates_three_queries():
    plan = fallback_plan(
        "Find recent papers about ZrTe5 devices, dry electrodes, low-temperature transport",
        2021,
        2026,
    )

    assert plan.year_from == 2021
    assert plan.year_to == 2026
    assert len(plan.queries) == 3
    assert "zrte5" in plan.main_topics


def test_plan_queries_runs_without_llm_key(tmp_path):
    settings = Settings(
        deepseek_api_key=None,
        deepseek_base_url="https://api.deepseek.com",
        deepseek_model="",
        unpaywall_email=None,
    )

    plan = plan_queries(
        "ZrTe5 air sensitive device fabrication",
        settings,
        year_from=2022,
        year_to=2026,
        db_path=tmp_path / "papers.db",
    )

    assert plan.queries
    assert plan.year_from == 2022
    assert plan.year_to == 2026
