from paper_intake.i18n import TRANSLATIONS, t


def test_zotero_ui_text_uses_ris_package_language():
    assert t("zotero_package_button", "zh") == "生成 Zotero 导入包"
    assert "RIS" in t("zotero_ris_mode_description", "zh")
    assert "不用于直接写入 Zotero" in t("zotero_ris_mode_description", "zh")


def test_zotero_ui_text_does_not_claim_local_api_one_click_write():
    rendered = "\n".join(
        str(value)
        for lang in TRANSLATIONS.values()
        for value in lang.values()
    )

    assert "一键导入 Zotero" not in rendered
    assert "Import to Zotero" not in rendered
    assert "Creating export package and importing to Zotero" not in rendered
