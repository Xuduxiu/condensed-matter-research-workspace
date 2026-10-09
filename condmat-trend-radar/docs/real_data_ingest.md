# Real Data Ingest

This project is local-first. Real metadata is stored in SQLite and can be resumed by journal/date/cursor checkpoints.

## Small test

```powershell
.venv\Scripts\python.exe -m backend.ingest_real --baseline-from 2024-01-01 --baseline-to 2024-03-31 --scope core --limit-per-journal 50 --force-refresh
```

If OpenAlex is reachable, this imports only condensed-matter-relevant records after filtering. If OpenAlex fails, the command returns clear error context and writes `openalex_error_*.log`; it does not create fake real papers.

## Full ingest

```powershell
.venv\Scripts\python.exe -m backend.ingest_real --baseline-from 2015-01-01 --scope core --resume
.venv\Scripts\python.exe -m backend.ingest_real --baseline-from 2015-01-01 --scope core_context --resume
```

Use `--journals` for a comma-separated subset. Use `--limit-per-journal 0` for no limit. Checkpoints are stored in `ingest_checkpoints`.

## Crossref enrichment

```powershell
.venv\Scripts\python.exe -m backend.enrich_crossref --missing-only --limit 1000
```

Crossref is a supplement for existing DOI records. It does not replace OpenAlex as the primary source.

## arXiv ingest

```powershell
.venv\Scripts\python.exe -m backend.ingest_arxiv --from 2024-01-01 --to 2026-07-10 --categories cond-mat.str-el,cond-mat.mtrl-sci,cond-mat.mes-hall,cond-mat.supr-con
```

arXiv records are stored as `source=arxiv`, `data_mode=real`, and include `pdf_url` when available. They are not mixed into the default Core ranking unless the selected scope includes all/preprints.
