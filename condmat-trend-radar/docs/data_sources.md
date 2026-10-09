# Data Sources

## Source tiers

The project uses three source tiers so core journal rankings are not diluted by broader context data.

### Core journals

Core journals drive the default hot list and default heatmap scope:

- Physical Review Letters
- Physical Review X
- Nature
- Science
- Nature Physics
- Nature Materials
- Nature Nanotechnology
- Nature Communications
- Science Advances
- npj Quantum Materials

### Context journals

Context journals extend the baseline and can be included in the UI with `Core + context`:

- Physical Review B
- 2D Materials
- Nano Letters
- ACS Nano
- Advanced Materials
- Advanced Functional Materials
- Materials Today Physics
- npj 2D Materials and Applications

### Preprints

Preprints are used for early-warning and all-scope lifecycle context:

- arXiv cond-mat.str-el
- arXiv cond-mat.mtrl-sci
- arXiv cond-mat.mes-hall
- arXiv cond-mat.supr-con
- arXiv cond-mat.quant-gas
- arXiv cond-mat.stat-mech

Preprint-only records have lower weight and are not mixed into the default core ranking.

## OpenAlex

OpenAlex is the primary journal metadata source. The client:

1. Resolves a journal display name through `/sources`.
2. Queries `/works` by source ID and publication date range.
3. Normalizes fields into the local paper schema.
4. Reconstructs abstracts from `abstract_inverted_index` when available.

Stored fields include title, abstract, publication date, journal/source, DOI, authors, concepts, cited-by count, referenced works, landing page URL, type, and raw JSON.

## Crossref

Crossref support is implemented as a DOI metadata supplement client. It is not called by default in the MVP update path, but the client normalizes title, abstract if available, published date, journal, DOI, publisher, subject, and reference count.

## arXiv

The arXiv client is available through `--include-arxiv` or when live updates use `--scope all`. arXiv records use weight `1` unless later matched to published DOI metadata.

## Manual import

Manual CSV/JSON import is available through:

```powershell
.venv\Scripts\python.exe -m backend.update_all --skip-network --manual data/raw/papers.csv
```

This supports lab-curated paper lists and DOI collections.

## Mock fallback

If external metadata APIs fail or return no filtered records, `backend.update_all` inserts deterministic mock records. The mock corpus is marked with `source = mock` and DOI prefix `10.0000/condmat-radar`.

The mock baseline intentionally includes:

- hBN from 2015 onward as a long-term platform material.
- graphene from the baseline start as a long-term platform material.
- moiré superlattice after 2018.
- fractional Chern insulator after 2023.
- altermagnetism after 2023.
- ZrTe5 as an intermittent smaller-field material system.

## Corpus size estimation

A lightweight estimator is available for documenting the expected corpus denominator:

```powershell
.venv\Scripts\python.exe -m backend.ingest.estimate_counts --baseline-from 2015-01-01 --baseline-to 2026-07-10 --scope core
```

It writes:

```text
data/exports/corpus_count_estimate.csv
data/exports/corpus_count_estimate.json
```

When OpenAlex is unavailable, the command still writes the files with zero/error rows so the dashboard can show that no live estimate was obtained.

## Local storage configuration

By default, data is stored under `data/`. For larger local metadata libraries, set these variables in `.env`:

```powershell
CONDMAT_RADAR_DATA_DIR=G:\condmat-trend-radar
CONDMAT_RADAR_DB=G:\condmat-trend-radar\condmat_radar.sqlite
CONDMAT_RADAR_CACHE=G:\condmat-trend-radar\cache
CONDMAT_RADAR_EXPORT=G:\condmat-trend-radar\exports
```

The backend also accepts `COND_MAT_RADAR_DATA_DIR`, `COND_MAT_RADAR_DB`, `COND_MAT_RADAR_CACHE`, and `COND_MAT_RADAR_EXPORT` aliases.

## Real metadata paths and OpenAlex troubleshooting

Real metadata can be stored under `G:\condmat-trend-radar` using `CONDMAT_RADAR_DATA_DIR`, `CONDMAT_RADAR_DB`, `CONDMAT_RADAR_CACHE`, `CONDMAT_RADAR_EXPORT`, and `CONDMAT_RADAR_LOGS`. The backend falls back to project-local `data/` if G: is unavailable or not writable.

Use `OPENALEX_MAILTO` for polite OpenAlex requests. `python -m backend.ingest.test_openalex` prints full HTTP error context and writes logs when OpenAlex rejects the request.
