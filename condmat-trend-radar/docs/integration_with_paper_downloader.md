# Integration with local paper downloader

`condmat-trend-radar` can export a downloader task list for another local paper retrieval project. The export is intentionally simple CSV/JSON so it can be consumed by PowerShell, Python, or a queue-based downloader.

## Endpoints

```text
GET /api/export/download_tasks.csv?concept=ZrTe5&from=2024-08&to=2026-07
GET /api/export/download_tasks.json?concept=ZrTe5&from=2024-08&to=2026-07
```

Parameters:

- `concept`: optional canonical concept/material/method filter. Leave empty to export the selected date range.
- `from`: first month, `YYYY-MM`.
- `to`: last month, `YYYY-MM`.

The API writes the latest files to the configured export directory:

```text
data/exports/download_tasks.csv
data/exports/download_tasks.json
```

If `CONDMAT_RADAR_EXPORT` is set, the files are written there instead.

## Fields

| Field | Meaning |
| --- | --- |
| `doi` | DOI when available. |
| `arxiv_id` | arXiv identifier when available or derivable from the local ID. |
| `title` | Original English paper title. |
| `journal` | Journal/source display name. |
| `publication_date` | Publication date from metadata. |
| `url` | Landing page URL. |
| `concepts` | Semicolon-separated display-eligible concepts. |
| `materials` | Semicolon-separated materials/material systems. |
| `methods` | Semicolon-separated methods. |
| `momentum` | Current radar momentum score for prioritization. |
| `download_priority` | Integer priority assembled from relevance, source, recency, DOI/arXiv availability, and momentum. |
| `reason` | Semicolon-separated priority reasons. |

## Suggested downloader order

1. Sort by `download_priority` descending.
2. Tie-break by `momentum` descending.
3. Prefer DOI resolution when `doi` is present.
4. Fall back to arXiv retrieval when `arxiv_id` is present.
5. Keep title/journal/date as the manual review fallback.

## Example PowerShell call

```powershell
$base = 'http://127.0.0.1:8000'
Invoke-WebRequest "$base/api/export/download_tasks.csv?concept=ZrTe5&from=2024-08&to=2026-07" -OutFile .\download_tasks.csv
```

## Notes

The export uses the local SQLite corpus and the current term extraction results. If the active corpus is mock data, the Overview page will mark it as mock; downloader exports from mock data should only be used for integration testing.

## Automatic downloader bridge

The bridge detects the existing `lab_paper_intake` project and writes task files into `data/inbox/trend_radar_download_tasks` unless `--output-dir` is supplied. It generates CSV, JSON, and `manifest.json`; it does not execute the downstream app or download PDFs.

CLI:

```powershell
.venv\Scripts\python.exe -m backend.integration.detect_downloader_project --root ".."
.venv\Scripts\python.exe -m backend.integration.export_to_downloader --concept ZrTe5 --from 2015-01 --to 2026-07 --limit 50 --dry-run
```

API:

```text
POST /api/integration/export_to_downloader
```

Body supports `concepts`, `mode`, `from`, `to`, `scope`, `limit`, `min_momentum`, `journal`, `include_arxiv`, `output_dir`, and `dry_run`.

## Consuming tasks in Lab Paper Intake

The integration is now bidirectional at the file boundary: Trend Radar writes the queue, and Lab Paper Intake consumes it.

From the workspace root:

```powershell
.\paper.cmd flow --concept ZrTe5 --from 2020-01 --limit 50
```

Or import the newest queue without exporting a new one:

```powershell
.\paper.cmd import-tasks
```

The intake UI also discovers the newest JSON/CSV queue in its sidebar. Imports use stable task IDs and are idempotent. Importing a task never starts PDF downloads.
## Import receipts and queue status

A successful import writes an atomic SHA-256 receipt beside the queue under `.receipts/`. The original task CSV/JSON remains unchanged. If the source file changes, the receipt is considered stale and the batch becomes pending again.

```powershell
.\paper.cmd status
cd lab_paper_intake
.\.venv\Scripts\python.exe -m paper_intake.cli queue-status
```
## Lifecycle events

When a linked paper is exported, Lab Paper Intake appends immutable `paper-intake-task-event-v1` files under `.receipts/events/`. Current statuses are `oa_resolved`, `downloaded`, and `exported`. Repeated exports preserve history while queue summaries count unique task IDs per status. The export ZIP contains `lifecycle_events.json` with counts and write errors.

Trend Radar exposes the same read-only progress at `GET /api/download-center/status`. The Download Center shows pending/imported batches, OA resolution, PDF download and citation-export counts, plus per-batch import reasons. Radar never writes receipts or lifecycle events; Lab Paper Intake remains their sole owner.
