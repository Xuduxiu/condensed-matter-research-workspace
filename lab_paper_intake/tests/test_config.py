from paper_intake.config import _env_float


def test_env_float_accepts_bounded_number(monkeypatch):
    monkeypatch.setenv("TEST_TIMEOUT", "15.5")
    assert _env_float("TEST_TIMEOUT", 30.0, minimum=1.0, maximum=300.0) == 15.5


def test_env_float_falls_back_for_invalid_or_out_of_range_values(monkeypatch):
    for value in ("invalid", "nan", "inf", "0", "301"):
        monkeypatch.setenv("TEST_TIMEOUT", value)
        assert _env_float("TEST_TIMEOUT", 30.0, minimum=1.0, maximum=300.0) == 30.0
