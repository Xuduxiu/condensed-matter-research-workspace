from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pandas as pd
import streamlit as st

from paper_intake.db import (
    count_papers,
    fetch_papers,
    init_db,
    sync_paper_pdf_status,
    upsert_papers,
)
from paper_intake.export_package import export_selected_package
from paper_intake.i18n import status_label, t
from paper_intake.models import Paper
from paper_intake.pipeline import run_intake
from paper_intake.config import get_settings
from paper_intake.os_utils import open_path
from paper_intake.task_importer import (
    import_task_file,
    summarize_task_queue,
    task_import_status,
)
from paper_intake.zotero import ZoteroLocalClient


LANGUAGE_OPTIONS = {"zh": "中文", "en": "English"}
STATUS_ORDER = ["downloaded", "oa_available", "metadata_only", "failed"]

st.set_page_config(page_title=t("app_title"), layout="wide")


def main() -> None:
    settings = get_settings()
    init_db()
    lang = _get_language()

    st.title(t("app_title", lang))
    st.caption(t("app_description", lang))
    prompt = st.text_area(
        t("research_prompt_label", lang),
        value=t("example_prompt", lang),
        placeholder=t("research_prompt_placeholder", lang),
        height=140,
        key=f"research_prompt_{lang}",
    )
    st.session_state["last_prompt"] = prompt

    current_year = datetime.now().year
    st.sidebar.header(t("settings", lang))
    max_papers = st.sidebar.slider(t("max_papers", lang), 5, 100, 25, 5)
    year_from, year_to = st.sidebar.slider(
        t("year_range", lang),
        min_value=1990,
        max_value=current_year,
        value=(max(1990, current_year - 5), current_year),
    )
    download_oa_pdfs = st.sidebar.checkbox(
        t("enable_pdf_download", lang),
        value=False,
    )
    enable_llm_summaries = st.sidebar.checkbox(
        t("enable_llm_summaries", lang),
        value=settings.llm_available,
        disabled=not settings.llm_available,
    )
    if settings.llm_available and enable_llm_summaries:
        st.sidebar.success(t("llm_enabled_status", lang))
    elif not settings.llm_available:
        st.sidebar.info(t("no_llm_warning", lang))
    if not settings.unpaywall_email:
        st.sidebar.info(t("unpaywall_warning", lang))

    if "papers" not in st.session_state:
        st.session_state["papers"] = []
    if "plan" not in st.session_state:
        st.session_state["plan"] = None
    if "errors" not in st.session_state:
        st.session_state["errors"] = []

    _render_library_controls(lang)
    _render_task_inbox(lang)

    if st.button(t("run_search", lang), type="primary", disabled=not prompt.strip()):
        with st.spinner(t("search_running", lang)):
            result = run_intake(
                prompt=prompt,
                settings=settings,
                max_papers=max_papers,
                year_from=year_from,
                year_to=year_to,
                enable_llm_summaries=enable_llm_summaries,
                download_oa_pdfs=download_oa_pdfs,
            )
        st.session_state["papers"] = result.papers
        st.session_state["plan"] = result.plan
        st.session_state["errors"] = result.errors
        st.success(t("search_completed", lang, count=len(result.papers)))

    _render_plan_and_errors(lang)
    _render_results(settings, lang)


def _render_library_controls(lang: str) -> None:
    st.sidebar.divider()
    st.sidebar.subheader(t("library_title", lang))
    saved_count = count_papers()
    st.sidebar.caption(t("library_saved_count", lang, count=saved_count))
    load_column, clear_column = st.sidebar.columns(2)

    if load_column.button(
        t("library_load", lang),
        key="load_saved_library",
        disabled=saved_count == 0,
        use_container_width=True,
    ):
        papers = fetch_papers()
        st.session_state["papers"] = papers
        st.session_state["plan"] = None
        st.session_state["errors"] = []
        st.session_state["last_prompt"] = t("library_prompt", lang)
        st.sidebar.success(t("library_loaded", lang, count=len(papers)))

    if clear_column.button(
        t("library_clear_view", lang),
        key="clear_library_view",
        disabled=not st.session_state.get("papers"),
        use_container_width=True,
    ):
        st.session_state["papers"] = []
        st.session_state["plan"] = None
        st.session_state["errors"] = []
        st.sidebar.info(t("library_view_cleared", lang))


def _render_task_inbox(lang: str) -> None:
    st.sidebar.divider()
    st.sidebar.subheader(t("task_inbox_title", lang))
    summary = summarize_task_queue()
    st.sidebar.caption(
        t(
            "task_inbox_summary",
            lang,
            pending=summary.pending_batches,
            imported=summary.imported_batches,
            invalid=summary.invalid_batches,
        )
    )
    lifecycle = summary.lifecycle_counts
    st.sidebar.caption(
        t(
            "task_lifecycle_summary",
            lang,
            oa_resolved=lifecycle.get("oa_resolved", 0),
            downloaded=lifecycle.get("downloaded", 0),
            exported=lifecycle.get("exported", 0),
        )
    )
    latest = summary.latest_path
    if latest is None:
        st.sidebar.caption(t("task_inbox_empty", lang))
        return

    st.sidebar.caption(t("task_inbox_latest", lang, path=str(latest)))
    status = task_import_status(latest)
    if status.imported:
        st.sidebar.success(
            t(
                "task_inbox_already_imported",
                lang,
                imported_at=status.imported_at or "-",
            )
        )
        button_label = t("task_inbox_reload", lang)
    else:
        button_label = t("task_inbox_load", lang)

    if not st.sidebar.button(button_label, key="load_latest_radar_tasks"):
        return
    try:
        result = import_task_file(latest, selected=True)
    except Exception as exc:
        st.sidebar.error(t("task_inbox_failed", lang, error=exc))
        return
    st.session_state["papers"] = result.papers
    st.session_state["plan"] = None
    st.session_state["errors"] = result.warnings
    st.session_state["last_prompt"] = t("task_inbox_prompt", lang)
    st.sidebar.success(t("task_inbox_loaded", lang, count=len(result.papers)))

def _get_language() -> str:
    if "language" not in st.session_state:
        st.session_state["language"] = "zh"
    return st.sidebar.selectbox(
        t("language_selector"),
        options=list(LANGUAGE_OPTIONS.keys()),
        index=list(LANGUAGE_OPTIONS.keys()).index(st.session_state["language"]),
        format_func=lambda code: LANGUAGE_OPTIONS[code],
        key="language",
    )


def _render_plan_and_errors(lang: str) -> None:
    plan = st.session_state.get("plan")
    if plan:
        with st.expander(t("query_plan", lang), expanded=False):
            st.json(plan.model_dump(mode="json"))
    for error in st.session_state.get("errors", []):
        st.warning(t("search_error", lang, error=error))


def _render_results(settings, lang: str) -> None:
    papers: list[Paper] = st.session_state.get("papers", [])
    if papers:
        papers = sync_paper_pdf_status(papers)
        st.session_state["papers"] = papers
    if not papers:
        st.caption(t("no_results", lang))
        return

    filtered = _filter_papers(papers, lang)
    st.subheader(t("results_title", lang, shown=len(filtered), total=len(papers)))
    edited = st.data_editor(
        _papers_to_frame(filtered, lang),
        hide_index=True,
        width="stretch",
        column_config={
            "id": st.column_config.TextColumn("ID"),
            "selected": st.column_config.CheckboxColumn(t("selected", lang)),
            "relevance_score": st.column_config.NumberColumn(
                t("relevance_score", lang),
                min_value=0,
                max_value=10,
                format="%.1f",
            ),
            "title": st.column_config.TextColumn(t("title", lang)),
            "year": st.column_config.NumberColumn(t("year", lang), format="%d"),
            "journal": st.column_config.TextColumn(t("journal", lang)),
            "doi": st.column_config.TextColumn(t("doi", lang)),
            "pdf_status": st.column_config.TextColumn(t("pdf_status", lang)),
            "local_pdf_path": st.column_config.TextColumn(t("local_pdf_path", lang)),
            "tags": st.column_config.TextColumn(t("tags", lang)),
            "short_summary": st.column_config.TextColumn(t("summary", lang)),
            "relevance_reason": st.column_config.TextColumn(t("relevance_reason", lang)),
            "source": st.column_config.TextColumn(t("source", lang)),
            "authors": st.column_config.TextColumn(t("authors", lang)),
        },
        disabled=[
            "id",
            "relevance_score",
            "title",
            "year",
            "journal",
            "doi",
            "pdf_status",
            "local_pdf_path",
            "tags",
            "short_summary",
            "relevance_reason",
            "source",
            "authors",
        ],
        key=f"paper_editor_{lang}",
    )

    selected_ids = set(edited.loc[edited["selected"], "id"].astype(str).tolist())
    updated_papers = [
        paper.model_copy(update={"selected": paper.id in selected_ids})
        for paper in papers
    ]
    st.session_state["papers"] = updated_papers
    upsert_papers(updated_papers)

    selected = [paper for paper in updated_papers if paper.selected]
    st.caption(t("selected_count", lang, count=len(selected)))
    _render_actions(selected, settings, lang)


def _filter_papers(papers: list[Paper], lang: str) -> list[Paper]:
    scores = [
        paper.relevance_score
        for paper in papers
        if paper.relevance_score is not None
    ]
    min_score_default = float(min(scores)) if scores else 0.0
    score_min = st.slider(
        t("filter_relevance_score", lang),
        0.0,
        10.0,
        min_score_default,
        0.5,
    )
    years = [paper.year for paper in papers if paper.year]
    if years:
        min_year = min(years)
        max_year = max(years)
        if min_year == max_year:
            year_filter = (min_year, max_year)
            st.caption(t("single_year_notice", lang, year=min_year))
        else:
            year_filter = st.slider(
                t("filter_year", lang),
                min_value=min_year,
                max_value=max_year,
                value=(min_year, max_year),
            )
    else:
        year_filter = (0, 9999)

    raw_statuses = {paper.pdf_status for paper in papers}
    statuses = [status for status in STATUS_ORDER if status in raw_statuses]
    statuses.extend(sorted(raw_statuses.difference(statuses)))
    status_filter = st.multiselect(
        t("filter_pdf_status", lang),
        statuses,
        default=statuses,
        format_func=lambda status: status_label(status, lang),
    )
    tags = sorted({tag for paper in papers for tag in paper.tags})
    tag_filter = st.multiselect(t("filter_tags", lang), tags, default=[])

    filtered: list[Paper] = []
    for paper in papers:
        if (paper.relevance_score or 0.0) < score_min:
            continue
        if paper.year and not (year_filter[0] <= paper.year <= year_filter[1]):
            continue
        if status_filter and paper.pdf_status not in status_filter:
            continue
        if tag_filter and not set(tag_filter).intersection(paper.tags):
            continue
        filtered.append(paper)
    return filtered


def _papers_to_frame(papers: list[Paper], lang: str) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "id": paper.id,
                "selected": paper.selected,
                "relevance_score": paper.relevance_score,
                "title": paper.title,
                "year": paper.year,
                "journal": paper.journal,
                "doi": paper.doi,
                "pdf_status": status_label(paper.pdf_status, lang),
                "local_pdf_path": paper.local_pdf_path,
                "tags": ", ".join(paper.tags),
                "short_summary": _short_summary(paper),
                "relevance_reason": paper.relevance_reason,
                "source": paper.source,
                "authors": _authors_text(paper),
            }
            for paper in papers
        ]
    )


def _short_summary(paper: Paper) -> str:
    summary = paper.chinese_summary
    text = summary.relation_to_lab or summary.problem or paper.relevance_reason or ""
    return text[:220]


def _authors_text(paper: Paper) -> str:
    authors = ", ".join(paper.authors[:6])
    if len(paper.authors) > 6:
        authors += ", et al."
    return authors


def _render_actions(selected: list[Paper], settings, lang: str) -> None:
    include_pdfs = st.checkbox(
        t("include_pdfs_in_export", lang),
        value=False,
        disabled=not selected,
        key=f"include_pdfs_export_{lang}",
    )
    if st.button(t("export_package", lang), disabled=not selected, key=f"export_package_{lang}"):
        try:
            result = export_selected_package(
                selected,
                settings=settings,
                prompt=st.session_state.get("last_prompt", ""),
                language=lang,
                download_pdfs=include_pdfs,
            )
            _store_export_result(result)
            st.success(t("package_success", lang, export_id=result.export_id))
        except Exception as exc:
            st.error(t("export_failed", lang, error=exc))

    _render_zotero_import(selected, settings, lang, include_pdfs)
    _render_last_export(lang)


def _store_export_result(result) -> None:
    by_id = {paper.id: paper for paper in result.papers}
    st.session_state["papers"] = [
        by_id.get(paper.id, paper) for paper in st.session_state.get("papers", [])
    ]
    upsert_papers(st.session_state["papers"])
    st.session_state["last_export"] = {
        "export_id": result.export_id,
        "export_dir": str(result.export_dir),
        "zip_path": str(result.zip_path),
        "csv_path": str(result.csv_path),
        "bibtex_path": str(result.bibtex_path),
        "ris_path": str(result.ris_path),
        "markdown_path": str(result.markdown_path),
    }


def _render_zotero_import(selected: list[Paper], settings, lang: str, include_pdfs: bool) -> None:
    st.divider()
    st.subheader(t("zotero_import_title", lang))
    st.info(t("zotero_ris_mode_description", lang))
    status = st.session_state.get("zotero_status")
    if status and status.get("connected"):
        st.success(t("zotero_status_connected", lang))
    elif status:
        st.warning(status.get("error_message") or t("zotero_status_disconnected", lang))
    else:
        st.caption(t("zotero_status_unknown", lang))

    if st.button(t("test_zotero_connection", lang), key=f"test_zotero_{lang}"):
        status_obj = ZoteroLocalClient().health_check()
        st.session_state["zotero_status"] = {
            "connected": status_obj.connected,
            "error_message": status_obj.error_message,
        }
        if status_obj.connected:
            st.success(t("zotero_status_connected", lang))
        else:
            st.warning(status_obj.error_message or t("zotero_status_disconnected", lang))

    if st.button(t("zotero_package_button", lang), disabled=not selected, key=f"zotero_package_{lang}"):
        try:
            with st.spinner(t("zotero_package_running", lang)):
                result = export_selected_package(
                    selected,
                    settings=settings,
                    prompt=st.session_state.get("last_prompt", ""),
                    language=lang,
                    download_pdfs=include_pdfs,
                )
                _store_export_result(result)
            st.success(t("zotero_package_success", lang, export_id=result.export_id))
            st.info(t("zotero_ris_path", lang, path=str(result.ris_path)))
        except Exception as exc:
            st.error(t("export_failed", lang, error=exc))


def _open_last_export_path(path_key: str, success_key: str, failure_key: str, lang: str) -> None:
    last_export = st.session_state.get("last_export") or {}
    path = last_export.get(path_key)
    if not path:
        st.warning(t("zotero_no_export", lang))
        return
    ok, message = open_path(str(path))
    if ok:
        st.success(t(success_key, lang))
    else:
        st.warning(t(failure_key, lang, path=path, error=message))

def _render_last_export(lang: str) -> None:
    last_export = st.session_state.get("last_export")
    if not last_export:
        return
    st.info(t("export_folder_path", lang, path=last_export["export_dir"]))
    st.info(t("export_zip_path", lang, path=last_export["zip_path"]))
    st.info(t("zotero_ris_path", lang, path=last_export["ris_path"]))
    open_cols = st.columns(2)
    if open_cols[0].button(t("open_ris_file", lang), key=f"open_ris_{last_export['export_id']}_{lang}"):
        _open_last_export_path(
            "ris_path",
            "open_ris_success",
            "open_ris_failed",
            lang,
        )
    if open_cols[1].button(t("open_export_folder", lang), key=f"open_folder_{last_export['export_id']}_{lang}"):
        _open_last_export_path(
            "export_dir",
            "open_folder_success",
            "open_folder_failed",
            lang,
        )
    cols = st.columns(5)
    downloads = [
        ("csv_path", t("download_csv", lang), "text/csv"),
        ("bibtex_path", t("download_bibtex", lang), "application/x-bibtex"),
        ("ris_path", t("download_ris", lang), "application/x-research-info-systems"),
        ("markdown_path", t("download_markdown", lang), "text/markdown"),
        ("zip_path", t("download_zip", lang), "application/zip"),
    ]
    for col, (path_key, label, mime) in zip(cols, downloads):
        path = Path(last_export[path_key])
        if path.exists():
            col.download_button(
                label,
                data=path.read_bytes(),
                file_name=path.name,
                mime=mime,
                key=f"download_{path_key}_{last_export['export_id']}_{lang}",
            )


if __name__ == "__main__":
    main()
