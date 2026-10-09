QUERY_PLAN_SCHEMA = """
Expected JSON:
{
  "main_topics": ["topic"],
  "queries": ["academic search query"],
  "year_from": 2021,
  "year_to": 2026,
  "exclude_terms": ["term"]
}
"""

SUMMARY_SCHEMA = """
Expected JSON:
{
  "relevance_score": 0.0,
  "relevance_reason": "short explanation",
  "tags": ["tag"],
  "chinese_summary": {
    "problem": "...",
    "method": "...",
    "key_results": "...",
    "relation_to_lab": "...",
    "reading_priority": "High"
  }
}
"""
