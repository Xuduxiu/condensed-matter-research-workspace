from __future__ import annotations

from pathlib import Path

from ..models import Paper


PRIORITY_ORDER = ["High", "Medium", "Low"]
ZH_PRIORITY = {"High": "高优先级", "Medium": "中优先级", "Low": "低优先级"}
EN_PRIORITY = {"High": "High Priority", "Medium": "Medium Priority", "Low": "Low Priority"}


def export_markdown(papers: list[Paper], path: Path, language: str = "zh") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(papers_to_markdown(papers, language=language), encoding="utf-8")
    return path


def papers_to_markdown(papers: list[Paper], language: str = "zh") -> str:
    grouped: dict[str, list[Paper]] = {priority: [] for priority in PRIORITY_ORDER}
    for paper in sorted(
        papers,
        key=lambda p: (p.relevance_score or 0.0, p.year or 0),
        reverse=True,
    ):
        grouped.setdefault(paper.chinese_summary.reading_priority, []).append(paper)

    if language == "en":
        lines = _zotero_import_intro("en") + ["# Selected Paper Summary", ""]
        priority_labels = EN_PRIORITY
    else:
        lines = _zotero_import_intro("zh") + ["# 选中文献摘要", ""]
        priority_labels = ZH_PRIORITY

    for priority in PRIORITY_ORDER:
        priority_papers = grouped.get(priority, [])
        if not priority_papers:
            continue
        lines.extend([f"## {priority_labels[priority]}", ""])
        for paper in priority_papers:
            lines.extend(_paper_lines(paper, language))
    return "\n".join(lines).rstrip() + "\n"



def _zotero_import_intro(language: str) -> list[str]:
    if language == "en":
        return [
            "# Zotero Import Instructions",
            "",
            "1. Open Zotero desktop.",
            "2. In Zotero, choose File → Import.",
            "3. Select `selected_papers.ris` in this folder.",
            "4. After import, use this document for Chinese summaries, relevance scores, and reading priority.",
            "5. PDF files are in the `pdfs/` subfolder.",
            "6. This version does not write directly to Zotero through Zotero Local API; Local API is used only for connection checks.",
            "",
        ]
    return [
        "# Zotero 导入说明",
        "",
        "1. 打开 Zotero 桌面端。",
        "2. 在 Zotero 中选择 File → Import。",
        "3. 选择本文件夹中的 `selected_papers.ris`。",
        "4. 导入后，可参考本文档中的中文摘要、相关性评分和阅读优先级。",
        "5. PDF 文件位于 `pdfs/` 子目录。",
        "6. 当前版本不通过 Zotero Local API 直接写入 Zotero；Local API 仅用于连接检测。",
        "",
    ]

def _paper_lines(paper: Paper, language: str) -> list[str]:
    authors = ", ".join(paper.authors[:6])
    if len(paper.authors) > 6:
        authors += ", et al."
    summary = paper.chinese_summary
    if language == "en":
        return [
            f"### {paper.title}",
            "",
            f"- Score: {_value(paper.relevance_score)}",
            f"- Year: {_value(paper.year)}",
            f"- Authors: {authors or 'N/A'}",
            f"- Journal: {_value(paper.journal)}",
            f"- DOI: {_value(paper.doi)}",
            f"- PDF: {_pdf_line(paper, 'en')}",
            f"- Tags: {', '.join(paper.tags) if paper.tags else 'N/A'}",
            f"- Relevance: {_value(paper.relevance_reason)}",
            f"- Problem: {_value(summary.problem)}",
            f"- Method: {_value(summary.method)}",
            f"- Key results: {_value(summary.key_results)}",
            f"- Relation to lab: {_value(summary.relation_to_lab)}",
            "",
        ]
    return [
        f"### {paper.title}",
        "",
        f"- 相关性评分：{_value(paper.relevance_score)}",
        f"- 年份：{_value(paper.year)}",
        f"- 作者：{authors or 'N/A'}",
        f"- 期刊：{_value(paper.journal)}",
        f"- DOI：{_value(paper.doi)}",
        f"- PDF：{_pdf_line(paper, 'zh')}",
        f"- 标签：{', '.join(paper.tags) if paper.tags else 'N/A'}",
        f"- 相关性理由：{_value(paper.relevance_reason)}",
        f"- 研究问题：{_value(summary.problem)}",
        f"- 方法：{_value(summary.method)}",
        f"- 关键结果：{_value(summary.key_results)}",
        f"- 与实验室课题关系：{_value(summary.relation_to_lab)}",
        "",
    ]


def _pdf_line(paper: Paper, language: str) -> str:
    if paper.pdf_status == "downloaded" and paper.local_pdf_path:
        if language == "en":
            return f"downloaded, local path: {paper.local_pdf_path}"
        return f"已下载，本地路径：{paper.local_pdf_path}"
    if paper.pdf_url:
        if language == "en":
            return f"open access available, link: {paper.pdf_url}"
        return f"开放获取可用，链接：{paper.pdf_url}"
    if language == "en":
        return "metadata only"
    return "仅元数据"


def _value(value: object) -> object:
    if value is None or value == "":
        return "N/A"
    return value