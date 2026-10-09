# Troubleshooting

## OpenAlex HTTPError / 429

Run:

```powershell
.venv\Scripts\python.exe -m backend.ingest.test_openalex
```

The output includes URL, query params, HTTP status, response text first 500 characters, and retry count. Logs are written to `CONDMAT_RADAR_LOGS` or `data/logs`.

A 429 response with `Insufficient budget` or very large `Retry-After` means this OpenAlex environment cannot currently query the API. Wait for the reset, configure `OPENALEX_MAILTO`, or use an OpenAlex plan/key compatible with the current API policy.

## No real papers in Overview

Run the small ingest first:

```powershell
.venv\Scripts\python.exe -m backend.ingest_real --baseline-from 2024-01-01 --baseline-to 2024-03-31 --scope core --limit-per-journal 50 --force-refresh
```

If it fails, do not treat mock data as a real trend. The mock corpus is for UI validation only.

## Why not full PDF download

The radar exports a pending task queue. It does not automatically download every PDF because of storage, licensing, rate-limit, and review concerns. The downstream downloader should prioritize OA PDFs using DOI/arXiv/pdf_url and keep human review in the loop.
