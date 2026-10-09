from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import date, timedelta
from typing import Any

import httpx

from backend.config import deepseek_api_key, deepseek_base_url, deepseek_model_fast, deepseek_model_pro
from backend.library.repository import stable_id, utc_now


ANALYSIS_TYPE = "deepseek_paper_brief"
RADAR_BRIEF_ANALYSIS_TYPE = "deepseek_radar_brief"
RADAR_BRIEF_METHODOLOGY = "v2.5"


class DeepSeekNotConfigured(RuntimeError):
    """Raised before any network request when no local API key is available."""


class DeepSeekRequestError(RuntimeError):
    """A safe, user-facing DeepSeek request failure without secret material."""


def deepseek_status() -> dict[str, Any]:
    return {
        "configured": bool(deepseek_api_key()),
        "base_url": deepseek_base_url(),
        "fast_model": deepseek_model_fast(),
        "pro_model": deepseek_model_pro(),
    }


def _json(value: Any, default: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(str(value or ""))
    except (TypeError, ValueError):
        return default


def _paper_context(connection: sqlite3.Connection, canonical_paper_id: str) -> dict[str, Any]:
    paper = connection.execute("SELECT * FROM papers WHERE id=?", (canonical_paper_id,)).fetchone()
    if not paper:
        raise LookupError("paper not found")
    version = connection.execute(
        """
        SELECT * FROM paper_versions WHERE canonical_paper_id=?
        ORDER BY COALESCE(publication_date, submitted_date, '') DESC, id DESC LIMIT 1
        """,
        (canonical_paper_id,),
    ).fetchone()
    version_id = str(version["id"]) if version else ""
    authors = []
    if version_id:
        authors = [str(row[0]) for row in connection.execute(
            """
            SELECT COALESCE(pa.raw_name, a.display_name) FROM paper_authors pa
            JOIN authors a ON a.id=pa.author_id WHERE pa.paper_version_id=?
            ORDER BY pa.author_position LIMIT 12
            """,
            (version_id,),
        )]
    materials = [str(row[0]) for row in connection.execute(
        """SELECT DISTINCT m.canonical_name FROM paper_materials pm
           JOIN materials m ON m.id=pm.material_id
           WHERE pm.canonical_paper_id=? ORDER BY m.canonical_name LIMIT 24""",
        (canonical_paper_id,),
    )]
    topics = [str(row[0]) for row in connection.execute(
        """SELECT DISTINCT t.canonical_name FROM paper_topics pt
           JOIN topics t ON t.id=pt.topic_id
           WHERE pt.canonical_paper_id=? ORDER BY t.canonical_name LIMIT 24""",
        (canonical_paper_id,),
    )]
    data = {
        "canonical_paper_id": canonical_paper_id,
        "title": str((version or paper)["title"] or paper["title"] or ""),
        "abstract": str((version or paper)["abstract"] or paper["abstract"] or "")[:12000],
        "authors": authors,
        "journal": str((version or paper)["journal"] or paper["journal"] or ""),
        "publication_date": str((version or paper)["publication_date"] or paper["publication_date"] or ""),
        "doi": str((version or paper)["doi"] or paper["doi"] or ""),
        "arxiv_id": str((version or paper)["arxiv_id"] or paper["arxiv_id"] or ""),
        "materials_extracted_locally": materials,
        "topics_extracted_locally": topics,
    }
    return data


def _hash(model: str, context: dict[str, Any]) -> str:
    value = json.dumps({"model": model, "context": context}, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _as_text(value: Any, max_length: int = 1200) -> str:
    return " ".join(str(value or "").split())[:max_length]


def _as_list(value: Any, max_items: int = 12, max_length: int = 400) -> list[str]:
    values = value if isinstance(value, list) else [value] if value else []
    cleaned = [_as_text(item, max_length) for item in values]
    return [item for item in cleaned if item][:max_items]


def _normalize_result(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise DeepSeekRequestError("DeepSeek returned an invalid structured response")
    return {
        "summary_zh": _as_text(value.get("summary_zh"), 1800),
        "research_question": _as_text(value.get("research_question"), 800),
        "methods": _as_list(value.get("methods")),
        "key_findings": _as_list(value.get("key_findings")),
        "materials": _as_list(value.get("materials")),
        "keywords": _as_list(value.get("keywords")),
        "novelty": _as_text(value.get("novelty"), 900),
        "caveats": _as_list(value.get("caveats")),
    }


def _decode_response_json(content: Any) -> dict[str, Any]:
    if isinstance(content, dict):
        return content
    text = str(content or "").strip()
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        pass
    else:
        if not isinstance(value, dict):
            raise DeepSeekRequestError("DeepSeek returned an invalid structured response")
        return value

    # OpenAI-compatible providers occasionally wrap an otherwise valid JSON
    # object in a Markdown fence or a short explanatory sentence despite
    # response_format=json_object. Accept one unambiguous object only; the
    # downstream radar/paper normalizers still enforce their exact schemas and
    # evidence IDs.
    max_wrapper_length = 2_000
    fenced: list[dict[str, Any]] = []
    cursor = 0
    while True:
        start = text.find("```", cursor)
        if start < 0:
            break
        header_end = text.find("\n", start + 3)
        if header_end < 0:
            raise DeepSeekRequestError("DeepSeek did not return valid JSON")
        end = text.find("```", header_end + 1)
        if end < 0:
            raise DeepSeekRequestError("DeepSeek did not return valid JSON")
        language = text[start + 3 : header_end].strip().lower()
        if language in {"", "json"}:
            try:
                value = json.loads(text[header_end + 1 : end].strip())
            except (TypeError, ValueError):
                pass
            else:
                if not isinstance(value, dict):
                    raise DeepSeekRequestError(
                        "DeepSeek returned an invalid structured response"
                    )
                wrapper = (text[:start] + text[end + 3 :]).strip()
                if len(wrapper) <= max_wrapper_length:
                    fenced.append(value)
        cursor = end + 3
    if len(fenced) == 1:
        return fenced[0]
    if len(fenced) > 1:
        raise DeepSeekRequestError("DeepSeek did not return valid JSON")

    decoder = json.JSONDecoder()
    candidates: list[dict[str, Any]] = []
    cursor = 0
    while True:
        start = text.find("{", cursor)
        if start < 0:
            break
        try:
            value, end = decoder.raw_decode(text, start)
        except (TypeError, ValueError):
            cursor = start + 1
            continue
        cursor = max(end, start + 1)
        if not isinstance(value, dict):
            continue
        wrapper = (text[:start] + text[end:]).strip()
        if (
            len(wrapper) <= max_wrapper_length
            and not any(mark in wrapper for mark in "{}[]")
        ):
            candidates.append(value)
    if len(candidates) == 1:
        return candidates[0]
    raise DeepSeekRequestError("DeepSeek did not return valid JSON")


def _response_json(content: Any) -> dict[str, Any]:
    return _normalize_result(_decode_response_json(content))


def _strict_text(value: Any, field: str, max_length: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DeepSeekRequestError(f"DeepSeek radar brief field {field} must be a non-empty string")
    return _as_text(value, max_length)


def _strict_text_list(value: Any, field: str, *, max_items: int = 12, max_length: int = 400) -> list[str]:
    if not isinstance(value, list):
        raise DeepSeekRequestError(f"DeepSeek radar brief field {field} must be an array")
    result: list[str] = []
    for item in value[:max_items]:
        if not isinstance(item, str) or not item.strip():
            raise DeepSeekRequestError(f"DeepSeek radar brief field {field} must contain strings")
        clean = _as_text(item, max_length)
        if clean not in result:
            result.append(clean)
    return result


def _normalize_radar_brief(value: Any, evidence_papers: list[dict[str, Any]]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise DeepSeekRequestError("DeepSeek returned an invalid radar brief")
    required = {
        "headline",
        "summary_zh",
        "rising_topics",
        "notable_materials",
        "papers_to_read",
        "caveats",
    }
    if set(value) != required:
        raise DeepSeekRequestError("DeepSeek radar brief must contain exactly the required JSON fields")
    paper_lookup = {
        str(item.get("canonical_paper_id") or ""): str(item.get("title") or "")
        for item in evidence_papers
        if str(item.get("canonical_paper_id") or "")
    }
    raw_papers = value.get("papers_to_read")
    if not isinstance(raw_papers, list):
        raise DeepSeekRequestError("DeepSeek radar brief field papers_to_read must be an array")
    papers_to_read: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in raw_papers[:12]:
        if not isinstance(item, dict):
            raise DeepSeekRequestError("DeepSeek radar brief papers_to_read entries must be objects")
        paper_id = _strict_text(item.get("canonical_paper_id"), "papers_to_read.canonical_paper_id", 200)
        _strict_text(item.get("title"), "papers_to_read.title", 500)
        reason = _strict_text(item.get("reason"), "papers_to_read.reason", 800)
        if paper_id not in paper_lookup:
            raise DeepSeekRequestError("DeepSeek radar brief referenced a paper outside the supplied evidence")
        if paper_id in seen:
            continue
        seen.add(paper_id)
        papers_to_read.append(
            {
                "canonical_paper_id": paper_id,
                "title": paper_lookup[paper_id],
                "reason": reason,
            }
        )
    return {
        "headline": _strict_text(value.get("headline"), "headline", 300),
        "summary_zh": _strict_text(value.get("summary_zh"), "summary_zh", 2400),
        "rising_topics": _strict_text_list(value.get("rising_topics"), "rising_topics"),
        "notable_materials": _strict_text_list(value.get("notable_materials"), "notable_materials"),
        "papers_to_read": papers_to_read,
        "caveats": _strict_text_list(value.get("caveats"), "caveats", max_items=16),
    }


def _radar_paper_evidence(
    connection: sqlite3.Connection,
    *,
    days: int,
    limit: int,
) -> dict[str, Any]:
    if not 7 <= int(days) <= 90:
        raise ValueError("days must be between 7 and 90")
    if not 5 <= int(limit) <= 50:
        raise ValueError("limit must be between 5 and 50")
    window_to = date.today()
    window_from = window_to - timedelta(days=int(days) - 1)
    rows = connection.execute(
        """
        WITH ranked_versions AS (
          SELECT v.*,
                 ROW_NUMBER() OVER (
                   PARTITION BY v.canonical_paper_id
                   ORDER BY COALESCE(NULLIF(v.publication_date,''), NULLIF(v.submitted_date,''), '') DESC,
                            v.id DESC
                 ) AS version_rank
          FROM paper_versions v
        ), candidates AS (
          SELECT p.id AS canonical_paper_id,
                 COALESCE(NULLIF(v.title,''), p.title, '') AS title,
                 COALESCE(NULLIF(v.abstract,''), NULLIF(p.abstract,''), '') AS abstract,
                 COALESCE(NULLIF(v.publication_date,''), NULLIF(v.submitted_date,''),
                          NULLIF(p.publication_date,''), '') AS evidence_date,
                 COALESCE(p.cited_by_count, 0) AS cited_by_count
          FROM papers p
          LEFT JOIN ranked_versions v
            ON v.canonical_paper_id=p.id AND v.version_rank=1
          WHERE p.data_mode='real' AND p.condmat_view_eligible=1
        )
        SELECT * FROM candidates
        WHERE substr(evidence_date, 1, 10) BETWEEN ? AND ?
        ORDER BY substr(evidence_date, 1, 10) DESC, cited_by_count DESC,
                 canonical_paper_id
        LIMIT ?
        """,
        (window_from.isoformat(), window_to.isoformat(), int(limit)),
    ).fetchall()
    paper_ids = [str(row["canonical_paper_id"]) for row in rows]
    materials: dict[str, list[str]] = {paper_id: [] for paper_id in paper_ids}
    topics: dict[str, list[str]] = {paper_id: [] for paper_id in paper_ids}
    if paper_ids:
        placeholders = ",".join("?" for _ in paper_ids)
        for row in connection.execute(
            f"""SELECT pm.canonical_paper_id, m.canonical_name
                FROM paper_materials pm JOIN materials m ON m.id=pm.material_id
                WHERE pm.canonical_paper_id IN ({placeholders})
                ORDER BY pm.canonical_paper_id, pm.confidence DESC, m.canonical_name""",
            paper_ids,
        ):
            values = materials[str(row["canonical_paper_id"])]
            name = _as_text(row["canonical_name"], 160)
            if name and name not in values and len(values) < 16:
                values.append(name)
        for row in connection.execute(
            f"""SELECT pt.canonical_paper_id, t.canonical_name
                FROM paper_topics pt JOIN topics t ON t.id=pt.topic_id
                WHERE pt.canonical_paper_id IN ({placeholders})
                ORDER BY pt.canonical_paper_id, pt.confidence DESC, t.canonical_name""",
            paper_ids,
        ):
            values = topics[str(row["canonical_paper_id"])]
            name = _as_text(row["canonical_name"], 160)
            if name and name not in values and len(values) < 16:
                values.append(name)
    papers = [
        {
            "canonical_paper_id": str(row["canonical_paper_id"]),
            "title": _as_text(row["title"], 500),
            "publication_date": str(row["evidence_date"] or "")[:10],
            "abstract": _as_text(row["abstract"], 1600),
            "cited_by_count": int(row["cited_by_count"] or 0),
            "materials_extracted_locally": materials[str(row["canonical_paper_id"])],
            "topics_extracted_locally": topics[str(row["canonical_paper_id"])],
        }
        for row in rows
    ]
    return {
        "window_from": window_from.isoformat(),
        "window_to": window_to.isoformat(),
        "paper_count": len(papers),
        "papers": papers,
    }


def _radar_evidence_summary(evidence: dict[str, Any]) -> dict[str, Any]:
    return {
        "window_from": str(evidence.get("window_from") or ""),
        "window_to": str(evidence.get("window_to") or ""),
        "paper_count": int(evidence.get("paper_count") or 0),
        "quality": evidence.get("quality") if isinstance(evidence.get("quality"), dict) else {},
        "papers": [
            {
                "canonical_paper_id": str(item.get("canonical_paper_id") or ""),
                "title": str(item.get("title") or ""),
                "publication_date": str(item.get("publication_date") or ""),
            }
            for item in evidence.get("papers") or []
            if isinstance(item, dict)
        ],
    }


def _radar_quality_context(connection: sqlite3.Connection, days: int) -> dict[str, Any]:
    """Build the same auditable reliability gate used by the scientific charts."""
    try:
        from backend.api.analytics_v25 import build_analytics_payload

        analytics = build_analytics_payload(connection, int(days))
    except (sqlite3.DatabaseError, KeyError, TypeError, ValueError):
        return {
            "methodology": RADAR_BRIEF_METHODOLOGY,
            "hotspot_reliable": False,
            "material_coverage_comparable": False,
            "warnings": ["统计质量门未能完成，AI 简报只能摘要论文，不能判断领域热点。"],
            "topic_candidates": [],
            "material_candidates": [],
        }
    quality = analytics.get("data_quality") if isinstance(analytics.get("data_quality"), dict) else {}
    def candidates(key: str) -> list[dict[str, Any]]:
        output: list[dict[str, Any]] = []
        for item in analytics.get(key) or []:
            if not isinstance(item, dict):
                continue
            output.append({
                "name": str(item.get("name") or ""),
                "current_count": int(item.get("current_count") or 0),
                "baseline_count": int(item.get("baseline_count") or 0),
                "share_change_pp": float(item.get("share_change_pp") or 0),
                "status": str(item.get("status") or "insufficient_evidence"),
                "reliability": float(item.get("reliability") or 0),
            })
            if len(output) >= 12:
                break
        return output
    return {
        "methodology": RADAR_BRIEF_METHODOLOGY,
        "data_anchor": analytics.get("data_anchor"),
        "current_research_outputs": int(quality.get("current_research_outputs") or 0),
        "baseline_research_outputs": int(quality.get("baseline_research_outputs") or 0),
        "window_volume_ratio": float(quality.get("window_volume_ratio") or 0),
        "abstract_coverage_pct": float(quality.get("abstract_coverage_pct") or 0),
        "source_mix_shift_pct": float(quality.get("source_mix_shift_pct") or 0),
        "multi_source_coverage_pct": float(quality.get("multi_source_coverage_pct") or 0),
        "topic_coverage_comparable": bool(quality.get("topic_coverage_comparable")),
        "material_coverage_comparable": bool(quality.get("material_coverage_comparable")),
        "method_coverage_comparable": bool(quality.get("method_coverage_comparable")),
        "hotspot_reliable": bool(quality.get("hotspot_reliable")),
        "warnings": [str(item) for item in analytics.get("data_warnings") or [] if str(item).strip()],
        "topic_candidates": candidates("topic_trends"),
        "material_candidates": candidates("material_trends"),
    }


def _apply_radar_quality_gate(analysis: dict[str, Any], quality: dict[str, Any]) -> dict[str, Any]:
    """Prevent a fluent model response from outrunning the statistical evidence."""
    result = dict(analysis)
    caveats = list(result.get("caveats") or [])
    for warning in quality.get("warnings") or []:
        warning = str(warning).strip()
        if warning and warning not in caveats:
            caveats.append(warning)
    if not bool(quality.get("hotspot_reliable")):
        result["headline"] = "当前数据不足以判断全领域近期热点"
        summary = str(result.get("summary_zh") or "").strip()
        prefix = "以下内容仅是当前观测论文的证据摘要，不可外推为整个凝聚态领域的热点结论。"
        result["summary_zh"] = (summary if summary.startswith(prefix) else f"{prefix}{summary}")[:2400]
        result["rising_topics"] = []
        gate_warning = "统计可靠性门未通过，因此系统已清空模型生成的上升主题列表。"
        if gate_warning not in caveats:
            caveats.append(gate_warning)
    if not bool(quality.get("material_coverage_comparable")):
        result["notable_materials"] = []
        material_warning = "材料抽取覆盖不可比，因此系统已清空模型生成的材料热点列表。"
        if material_warning not in caveats:
            caveats.append(material_warning)
    result["caveats"] = caveats[:16]
    return result

def _radar_public_response(
    stored: dict[str, Any],
    *,
    provider: str,
    model: str,
    updated_at: Any,
    cached: bool,
) -> dict[str, Any]:
    request = stored.get("request") if isinstance(stored.get("request"), dict) else {}
    evidence = stored.get("evidence") if isinstance(stored.get("evidence"), dict) else {}
    evidence_papers = evidence.get("papers") if isinstance(evidence.get("papers"), list) else []
    analysis = _normalize_radar_brief(stored.get("analysis"), evidence_papers)
    quality = evidence.get("quality") if isinstance(evidence.get("quality"), dict) else {}
    analysis = _apply_radar_quality_gate(analysis, quality)
    return {
        "available": True,
        "cached": cached,
        "provider": provider or "deepseek",
        "model": model,
        "updated_at": updated_at,
        "request": request,
        "evidence": evidence,
        "analysis": analysis,
    }


def get_latest_radar_brief(connection: sqlite3.Connection, *, days: int) -> dict[str, Any]:
    if not 7 <= int(days) <= 90:
        raise ValueError("days must be between 7 and 90")
    rows = connection.execute(
        """
        SELECT result_json, provider, model, updated_at FROM analysis_results
        WHERE canonical_paper_id IS NULL AND analysis_type=? AND status='completed'
        ORDER BY updated_at DESC, id DESC
        """,
        (RADAR_BRIEF_ANALYSIS_TYPE,),
    )
    for row in rows:
        stored = _json(row["result_json"], {})
        request = stored.get("request") if isinstance(stored, dict) else None
        try:
            cached_days = int(request.get("days")) if isinstance(request, dict) else 0
        except (TypeError, ValueError):
            cached_days = 0
        if cached_days != int(days) or not isinstance(request, dict) or request.get("methodology") != RADAR_BRIEF_METHODOLOGY:
            continue
        try:
            return _radar_public_response(
                stored,
                provider=str(row["provider"] or "deepseek"),
                model=str(row["model"] or ""),
                updated_at=row["updated_at"],
                cached=True,
            )
        except DeepSeekRequestError:
            continue
    return {
        "available": False,
        "cached": False,
        "request": {"days": int(days)},
        "analysis": None,
    }


def generate_radar_brief(
    connection: sqlite3.Connection,
    *,
    days: int = 7,
    tier: str = "fast",
    force: bool = False,
    limit: int = 20,
) -> dict[str, Any]:
    if tier not in {"fast", "pro"}:
        raise ValueError("tier must be fast or pro")
    evidence = _radar_paper_evidence(connection, days=days, limit=limit)
    evidence["quality"] = _radar_quality_context(connection, days)
    request_meta = {"days": int(days), "tier": tier, "limit": int(limit), "methodology": RADAR_BRIEF_METHODOLOGY}
    evidence_summary = _radar_evidence_summary(evidence)
    if not evidence["papers"]:
        return {
            "available": False,
            "cached": False,
            "request": request_meta,
            "evidence": evidence_summary,
            "analysis": None,
            "reason": "no eligible real papers were found in the requested date window",
        }

    model = deepseek_model_pro() if tier == "pro" else deepseek_model_fast()
    model_context = {
        "request": request_meta,
        "window_from": evidence["window_from"],
        "window_to": evidence["window_to"],
        "quality": evidence["quality"],
        "papers": evidence["papers"],
    }
    input_hash = _hash(model, model_context)
    if not force:
        cached = connection.execute(
            """
            SELECT result_json, provider, model, updated_at FROM analysis_results
            WHERE canonical_paper_id IS NULL AND analysis_type=? AND input_hash=?
              AND status='completed'
            ORDER BY updated_at DESC LIMIT 1
            """,
            (RADAR_BRIEF_ANALYSIS_TYPE, input_hash),
        ).fetchone()
        if cached:
            return _radar_public_response(
                _json(cached["result_json"], {}),
                provider=str(cached["provider"] or "deepseek"),
                model=str(cached["model"] or model),
                updated_at=cached["updated_at"],
                cached=True,
            )

    key = deepseek_api_key()
    if not key:
        raise DeepSeekNotConfigured("DeepSeek API key is not configured")
    system_prompt = (
        "你是凝聚态论文雷达助理。只能依据用户提供的本地论文标题、摘要、日期、材料和主题证据，"
        "不得引入外部事实、补写缺失结论或虚构趋势。返回单个严格 JSON 对象，不要 Markdown。"
        "字段必须且只能为 headline、summary_zh、rising_topics、notable_materials、papers_to_read、caveats；"
        "rising_topics、notable_materials、caveats 是字符串数组；papers_to_read 是对象数组，"
        "每项必须包含 canonical_paper_id、title、reason，且 canonical_paper_id 必须来自输入。"
        "只有输入 quality.hotspot_reliable=true 时 rising_topics 才能非空；否则必须为空，并在标题与 caveats 说明只能做样本摘要。"
        "只有 quality.material_coverage_comparable=true 时 notable_materials 才能非空。证据不足时在 caveats 中逐条说明。"
    )
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(model_context, ensure_ascii=False)},
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0.15,
        "max_tokens": 6000,
    }
    endpoint = deepseek_base_url().rstrip("/") + "/chat/completions"
    try:
        with httpx.Client(timeout=httpx.Timeout(50.0, connect=10.0)) as client:
            response = client.post(
                endpoint,
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json=payload,
            )
        response.raise_for_status()
        body = response.json()
        choices = body.get("choices") if isinstance(body, dict) else None
        content = choices[0].get("message", {}).get("content") if isinstance(choices, list) and choices else None
        finish_reason = choices[0].get("finish_reason") if isinstance(choices, list) and choices else None
        if finish_reason == "length":
            raise DeepSeekRequestError("DeepSeek output token budget exhausted before a complete JSON response")
        analysis = _normalize_radar_brief(_decode_response_json(content), evidence["papers"])
        analysis = _apply_radar_quality_gate(analysis, evidence["quality"])
    except httpx.HTTPStatusError as exc:
        raise DeepSeekRequestError(f"DeepSeek API request failed (HTTP {exc.response.status_code})") from exc
    except httpx.HTTPError as exc:
        raise DeepSeekRequestError("DeepSeek network request failed") from exc
    except (KeyError, TypeError, ValueError) as exc:
        raise DeepSeekRequestError("DeepSeek response could not be processed") from exc

    now = utc_now()
    stored = {
        "request": request_meta,
        "evidence": evidence_summary,
        "analysis": analysis,
    }
    result_json = json.dumps(stored, ensure_ascii=False)
    cache_id = stable_id("analysis", f"library:{RADAR_BRIEF_ANALYSIS_TYPE}:{input_hash}")
    connection.execute(
        """
        INSERT INTO analysis_results
        (id, canonical_paper_id, analysis_type, provider, model, input_hash,
         result_json, status, created_at, updated_at)
        VALUES (?, NULL, ?, 'deepseek', ?, ?, ?, 'completed', ?, ?)
        ON CONFLICT(id) DO UPDATE SET
          provider='deepseek', model=excluded.model, input_hash=excluded.input_hash,
          result_json=excluded.result_json, status='completed', updated_at=excluded.updated_at
        """,
        (cache_id, RADAR_BRIEF_ANALYSIS_TYPE, model, input_hash, result_json, now, now),
    )
    return _radar_public_response(
        stored,
        provider="deepseek",
        model=model,
        updated_at=now,
        cached=False,
    )


def analyze_paper(
    connection: sqlite3.Connection,
    canonical_paper_id: str,
    *,
    tier: str = "fast",
    force: bool = False,
) -> dict[str, Any]:
    if tier not in {"fast", "pro"}:
        raise ValueError("tier must be fast or pro")
    key = deepseek_api_key()
    if not key:
        raise DeepSeekNotConfigured("DeepSeek API key is not configured")
    model = deepseek_model_pro() if tier == "pro" else deepseek_model_fast()
    context = _paper_context(connection, canonical_paper_id)
    input_hash = _hash(model, context)
    if not force:
        cached = connection.execute(
            """
            SELECT result_json, provider, model, updated_at FROM analysis_results
            WHERE canonical_paper_id=? AND analysis_type=? AND input_hash=? AND status='completed'
            ORDER BY updated_at DESC LIMIT 1
            """,
            (canonical_paper_id, ANALYSIS_TYPE, input_hash),
        ).fetchone()
        if cached:
            return {
                "cached": True,
                "provider": str(cached["provider"] or "deepseek"),
                "model": str(cached["model"] or model),
                "updated_at": cached["updated_at"],
                "analysis": _normalize_result(_json(cached["result_json"], {})),
            }

    system_prompt = (
        "你是凝聚态论文助理。只能根据用户提供的本地论文元数据与摘要，不得引入外部事实、"
        "虚构实验结果或引用。用简体中文返回单个 JSON 对象，不要 Markdown。"
        "JSON 字段必须为 summary_zh, research_question, methods, key_findings, materials, keywords, novelty, caveats；"
        "methods/key_findings/materials/keywords/caveats 为字符串数组。摘要缺失时明确说明信息不足。"
    )
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(context, ensure_ascii=False)},
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0.2,
        "max_tokens": 3000,
    }
    endpoint = deepseek_base_url().rstrip("/") + "/chat/completions"
    try:
        with httpx.Client(timeout=httpx.Timeout(40.0, connect=10.0)) as client:
            response = client.post(endpoint, headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"}, json=payload)
        response.raise_for_status()
        body = response.json()
        choices = body.get("choices") if isinstance(body, dict) else None
        content = choices[0].get("message", {}).get("content") if isinstance(choices, list) and choices else None
        finish_reason = choices[0].get("finish_reason") if isinstance(choices, list) and choices else None
        if finish_reason == "length":
            raise DeepSeekRequestError("DeepSeek output token budget exhausted before a complete JSON response")
        analysis = _response_json(content)
    except httpx.HTTPStatusError as exc:
        raise DeepSeekRequestError(f"DeepSeek API request failed (HTTP {exc.response.status_code})") from exc
    except httpx.HTTPError as exc:
        raise DeepSeekRequestError("DeepSeek network request failed") from exc
    except (KeyError, TypeError, ValueError) as exc:
        raise DeepSeekRequestError("DeepSeek response could not be processed") from exc

    now = utc_now()
    result_json = json.dumps(analysis, ensure_ascii=False)
    connection.execute(
        """
        INSERT INTO analysis_results
        (id, canonical_paper_id, analysis_type, provider, model, input_hash, result_json, status, created_at, updated_at)
        VALUES (?, ?, ?, 'deepseek', ?, ?, ?, 'completed', ?, ?)
        ON CONFLICT(canonical_paper_id, analysis_type, input_hash) DO UPDATE SET
          provider='deepseek', model=excluded.model, result_json=excluded.result_json,
          status='completed', updated_at=excluded.updated_at
        """,
        (stable_id("analysis", f"{canonical_paper_id}:{ANALYSIS_TYPE}:{input_hash}"), canonical_paper_id, ANALYSIS_TYPE, model, input_hash, result_json, now, now),
    )
    return {"cached": False, "provider": "deepseek", "model": model, "updated_at": now, "analysis": analysis}