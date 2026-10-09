from paper_intake.i18n import TRANSLATIONS, status_label, t


def test_translation_defaults_to_chinese():
    assert t("run_search") == "开始检索"


def test_translation_falls_back_to_chinese_for_missing_english_key(monkeypatch):
    monkeypatch.setitem(TRANSLATIONS["zh"], "zh_only_test_key", "中文回退")
    assert t("zh_only_test_key", "en") == "中文回退"


def test_translation_falls_back_to_key_when_missing_entirely():
    assert t("missing_key", "en") == "missing_key"


def test_translation_interpolates_values():
    assert t("selected_count", "en", count=3) == "3 selected"


def test_status_label_uses_language():
    assert status_label("metadata_only", "zh") == "仅元数据"
    assert status_label("metadata_only", "en") == "metadata_only"

def test_library_controls_are_translated_and_explicitly_non_destructive():
    assert t("library_saved_count", "zh", count=12) == "已保存 12 篇；网页刷新后可从这里恢复。"
    assert "未删除" in t("library_view_cleared", "zh")
    assert "not deleted" in t("library_view_cleared", "en")
