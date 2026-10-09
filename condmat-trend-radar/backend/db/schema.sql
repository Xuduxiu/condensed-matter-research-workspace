CREATE TABLE IF NOT EXISTS papers (
  id TEXT PRIMARY KEY,
  doi TEXT,
  title TEXT NOT NULL,
  abstract TEXT,
  journal TEXT,
  publication_date TEXT,
  year INTEGER,
  month TEXT,
  source TEXT,
  source_scope TEXT,
  data_mode TEXT NOT NULL DEFAULT 'real',
  condmat_confidence TEXT DEFAULT 'medium',
  url TEXT,
  pdf_url TEXT,
  oa_url TEXT,
  is_open_access INTEGER NOT NULL DEFAULT 0,
  oa_status TEXT,
  openalex_id TEXT,
  arxiv_id TEXT,
  linked_published_paper_id TEXT,
  cited_by_count INTEGER DEFAULT 0,
  raw_json TEXT,
  raw_crossref_json TEXT,
  raw_openalex_json TEXT,
  authorships_json TEXT,
  institutions_json TEXT,
  openalex_concepts_json TEXT,
  openalex_topics_json TEXT,
  openalex_keywords_json TEXT,
  referenced_works_json TEXT,
  related_works_json TEXT,
  openalex_enriched_at TEXT,
  download_status TEXT DEFAULT 'not_requested',
  download_priority INTEGER DEFAULT 0,
  local_pdf_path TEXT,
  zotero_export_status TEXT DEFAULT 'not_requested',
  last_download_attempt_at TEXT,
  last_download_error TEXT,
  condmat_view_eligible INTEGER NOT NULL DEFAULT 0,
  condmat_view_reason TEXT DEFAULT '',
  created_at TEXT,
  updated_at TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_papers_doi
ON papers(doi)
WHERE doi IS NOT NULL AND doi != '';

CREATE INDEX IF NOT EXISTS idx_papers_month ON papers(month);
CREATE INDEX IF NOT EXISTS idx_papers_journal ON papers(journal);
CREATE INDEX IF NOT EXISTS idx_papers_source ON papers(source);
CREATE INDEX IF NOT EXISTS idx_papers_data_mode ON papers(data_mode);
CREATE INDEX IF NOT EXISTS idx_papers_openalex ON papers(openalex_id);
CREATE INDEX IF NOT EXISTS idx_papers_arxiv ON papers(arxiv_id);
CREATE INDEX IF NOT EXISTS idx_papers_condmat_view ON papers(condmat_view_eligible);

CREATE TABLE IF NOT EXISTS paper_terms (
  paper_id TEXT NOT NULL,
  term TEXT NOT NULL,
  term_type TEXT NOT NULL,
  normalized_term TEXT NOT NULL,
  confidence REAL NOT NULL DEFAULT 1.0,
  display_eligible INTEGER NOT NULL DEFAULT 1,
  display_reason TEXT DEFAULT 'legacy',
  source TEXT DEFAULT 'extractor',
  PRIMARY KEY (paper_id, normalized_term, term_type),
  FOREIGN KEY (paper_id) REFERENCES papers(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_paper_terms_term ON paper_terms(normalized_term);
CREATE INDEX IF NOT EXISTS idx_paper_terms_type ON paper_terms(term_type);
CREATE INDEX IF NOT EXISTS idx_paper_terms_display ON paper_terms(display_eligible);

CREATE TABLE IF NOT EXISTS monthly_corpus_stats (
  month TEXT NOT NULL,
  corpus_scope TEXT NOT NULL,
  data_mode TEXT NOT NULL DEFAULT 'mixed',
  total_papers INTEGER NOT NULL DEFAULT 0,
  total_weighted_papers REAL NOT NULL DEFAULT 0,
  PRIMARY KEY (month, corpus_scope, data_mode)
);

CREATE INDEX IF NOT EXISTS idx_monthly_corpus_scope ON monthly_corpus_stats(corpus_scope);
CREATE INDEX IF NOT EXISTS idx_monthly_corpus_mode ON monthly_corpus_stats(data_mode);

CREATE TABLE IF NOT EXISTS term_month_stats (
  term TEXT NOT NULL,
  month TEXT NOT NULL,
  corpus_scope TEXT NOT NULL DEFAULT 'all',
  data_mode TEXT NOT NULL DEFAULT 'mixed',
  raw_freq INTEGER NOT NULL,
  weighted_freq REAL NOT NULL,
  normalized_share REAL NOT NULL DEFAULT 0,
  weighted_normalized_share REAL NOT NULL DEFAULT 0,
  momentum REAL NOT NULL,
  journal_breakdown TEXT NOT NULL,
  citation_signal REAL NOT NULL DEFAULT 0,
  display_eligible INTEGER NOT NULL DEFAULT 1,
  display_reason TEXT DEFAULT 'legacy',
  PRIMARY KEY (term, month, corpus_scope, data_mode)
);

CREATE INDEX IF NOT EXISTS idx_term_month_stats_month ON term_month_stats(month);
CREATE INDEX IF NOT EXISTS idx_term_month_stats_scope ON term_month_stats(corpus_scope);
CREATE INDEX IF NOT EXISTS idx_term_month_stats_display ON term_month_stats(display_eligible);
CREATE INDEX IF NOT EXISTS idx_term_month_stats_mode ON term_month_stats(data_mode);

CREATE TABLE IF NOT EXISTS concept_lifecycle (
  concept TEXT PRIMARY KEY,
  first_seen TEXT,
  historical_first_seen TEXT,
  trend_first_seen TEXT,
  peak_month TEXT,
  peak_value REAL,
  half_life_months REAL,
  active_duration_months INTEGER,
  burst_duration_months INTEGER,
  baseline_total_count INTEGER DEFAULT 0,
  historical_count_before_trend INTEGER DEFAULT 0,
  trend_total_count INTEGER DEFAULT 0,
  novelty_score REAL DEFAULT 0,
  concept_class TEXT DEFAULT 'physics_concept',
  is_historical INTEGER DEFAULT 0,
  is_platform_term INTEGER DEFAULT 0,
  status TEXT,
  updated_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_lifecycle_status ON concept_lifecycle(status);
CREATE INDEX IF NOT EXISTS idx_lifecycle_class ON concept_lifecycle(concept_class);

CREATE TABLE IF NOT EXISTS update_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  from_date TEXT,
  to_date TEXT,
  baseline_from TEXT,
  baseline_to TEXT,
  trend_from TEXT,
  trend_to TEXT,
  trend_months INTEGER,
  scope TEXT,
  requested_live INTEGER NOT NULL DEFAULT 1,
  inserted_papers INTEGER NOT NULL DEFAULT 0,
  updated_papers INTEGER NOT NULL DEFAULT 0,
  duplicate_papers INTEGER NOT NULL DEFAULT 0,
  failed_requests INTEGER NOT NULL DEFAULT 0,
  used_mock_data INTEGER NOT NULL DEFAULT 0,
  message TEXT
);

CREATE TABLE IF NOT EXISTS ingest_runs (
  id TEXT PRIMARY KEY,
  started_at TEXT,
  finished_at TEXT,
  source TEXT,
  scope TEXT,
  baseline_from TEXT,
  baseline_to TEXT,
  status TEXT,
  fetched_count INTEGER DEFAULT 0,
  kept_count INTEGER DEFAULT 0,
  deduped_count INTEGER DEFAULT 0,
  failed_count INTEGER DEFAULT 0,
  error_summary TEXT,
  config_json TEXT
);

CREATE TABLE IF NOT EXISTS ingest_checkpoints (
  source TEXT,
  scope TEXT,
  journal TEXT,
  date_from TEXT,
  date_to TEXT,
  cursor TEXT,
  page INTEGER DEFAULT 0,
  updated_at TEXT,
  PRIMARY KEY(source, scope, journal, date_from, date_to)
);
CREATE TABLE IF NOT EXISTS ingest_chunks (
  id TEXT PRIMARY KEY,
  run_id TEXT,
  source TEXT,
  scope TEXT,
  journal TEXT,
  date_from TEXT,
  date_to TEXT,
  status TEXT,
  fetched_count INTEGER DEFAULT 0,
  kept_count INTEGER DEFAULT 0,
  deduped_count INTEGER DEFAULT 0,
  failed_count INTEGER DEFAULT 0,
  started_at TEXT,
  finished_at TEXT,
  error_summary TEXT,
  log_path TEXT,
  checkpoint_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_ingest_chunks_run ON ingest_chunks(run_id);
CREATE INDEX IF NOT EXISTS idx_ingest_chunks_status ON ingest_chunks(status);
CREATE INDEX IF NOT EXISTS idx_ingest_chunks_journal ON ingest_chunks(journal);
CREATE INDEX IF NOT EXISTS idx_ingest_chunks_dates ON ingest_chunks(date_from, date_to);


CREATE TABLE IF NOT EXISTS published_preprint_stats (
  term TEXT NOT NULL,
  month TEXT NOT NULL,
  published_heat_score REAL NOT NULL DEFAULT 0,
  preprint_rise_score REAL NOT NULL DEFAULT 0,
  validation_gap_score REAL NOT NULL DEFAULT 0,
  published_raw_freq INTEGER NOT NULL DEFAULT 0,
  preprint_raw_freq INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT,
  PRIMARY KEY (term, month)
);

CREATE INDEX IF NOT EXISTS idx_pub_preprint_month ON published_preprint_stats(month);
CREATE INDEX IF NOT EXISTS idx_pub_preprint_gap ON published_preprint_stats(validation_gap_score);

CREATE TABLE IF NOT EXISTS llm_jobs (
  id TEXT PRIMARY KEY,
  job_type TEXT NOT NULL,
  provider TEXT NOT NULL DEFAULT 'deepseek',
  model TEXT,
  concept TEXT,
  corpus_preset TEXT,
  status TEXT NOT NULL DEFAULT 'dry_run',
  request_json TEXT,
  response_json TEXT,
  error TEXT,
  created_at TEXT,
  updated_at TEXT
);

CREATE TABLE IF NOT EXISTS llm_cache (
  cache_key TEXT PRIMARY KEY,
  provider TEXT NOT NULL DEFAULT 'deepseek',
  model TEXT,
  prompt_hash TEXT,
  response_json TEXT,
  created_at TEXT
);

CREATE TABLE IF NOT EXISTS paper_chunks (
  id TEXT PRIMARY KEY,
  paper_id TEXT NOT NULL,
  chunk_index INTEGER NOT NULL DEFAULT 0,
  text TEXT NOT NULL,
  token_count INTEGER NOT NULL DEFAULT 0,
  evidence_type TEXT DEFAULT 'metadata',
  created_at TEXT,
  FOREIGN KEY (paper_id) REFERENCES papers(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_paper_chunks_paper ON paper_chunks(paper_id);

CREATE TABLE IF NOT EXISTS evidence_items (
  id TEXT PRIMARY KEY,
  paper_id TEXT NOT NULL,
  concept TEXT,
  claim_type TEXT,
  evidence_text TEXT,
  source_field TEXT DEFAULT 'metadata',
  score REAL NOT NULL DEFAULT 0,
  created_at TEXT,
  FOREIGN KEY (paper_id) REFERENCES papers(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_evidence_concept ON evidence_items(concept);
CREATE VIEW IF NOT EXISTS strict_condmat_papers AS
SELECT * FROM papers
WHERE data_mode = 'real' AND COALESCE(condmat_view_eligible, 0) = 1;
