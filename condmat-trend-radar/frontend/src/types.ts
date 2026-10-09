export type Tone = 'neutral' | 'accent' | 'success' | 'warning' | 'danger' | 'preprint' | 'publication';

export type PaperSummary = {
  paper_version_id: string;
  canonical_paper_id: string;
  version_type: 'preprint' | 'publication' | string;
  title: string;
  abstract?: string | null;
  doi?: string | null;
  arxiv_id?: string | null;
  arxiv_version?: number | null;
  journal?: string | null;
  publication_date?: string | null;
  submitted_date?: string | null;
  year?: number | null;
  source?: string | null;
  url?: string | null;
  pdf_url?: string | null;
  is_open_access?: boolean;
  cited_by_count?: number | null;
  downloaded?: boolean;
  download_status?: string | null;
  extraction_status?: string | null;
  favorite?: boolean;
  reading_status?: string | null;
  authors: string[];
  materials: string[];
  topics: string[];
};

export type PaperSearchResponse = {
  items: PaperSummary[];
  total: number;
  count: number;
  limit: number;
  offset: number;
  query: string;
  search_scope: string;
};

export type PaperDetail = {
  paper: Record<string, unknown> & { id: string; title: string; abstract?: string | null };
  versions: Array<Record<string, unknown> & { id: string; title: string; version_type: string; publication_date?: string | null; source?: string }>;
  authors: Array<{ paper_version_id: string; display_name: string; orcid?: string | null; affiliations?: unknown[] }>;
  materials: Array<{ id: string; canonical_name: string; phase?: string | null; thickness?: string | null }>;
  topics: Array<{ id: string; canonical_name: string; topic_type: string }>;
  files: Array<{ id: string; absolute_path: string; file_size: number; extraction_status: string; validation_status: string; page_count?: number | null; access_basis?: string | null }>;
  download_tasks: Array<{ id: string; status: string; updated_at: string; last_error?: string | null; attempt_count: number }>;
  version_links: Array<Record<string, unknown>>;
  user_state: PaperUserState;
  available_collections: PaperCollection[];
  ai_analysis?: PaperAiAnalysis | null;
};

export type MaterialSummary = {
  id: string;
  canonical_name: string;
  material_family: string;
  phase?: string | null;
  thickness?: string | null;
  composition?: string | null;
  count_7d: number;
  count_30d: number;
  count_12m: number;
  monthly_baseline: number;
  preprint_30d: number;
  publication_30d: number;
  preprint_change: number;
  publication_change: number;
  total_papers: number;
  latest_paper_date?: string | null;
  new_team_count?: number | null;
  trend_score: number;
  evidence_mentions?: number;
  extraction_sources?: string | null;
};

export type MaterialDetail = {
  material: { id: string; canonical_name: string; material_family: string; phase?: string | null; thickness?: string | null; composition?: string | null };
  aliases: string[];
  series: Array<{ month: string; preprints: number; publications: number; total: number }>;
  counts: { total: number; preprints: number; publications: number };
  papers: PaperSummary[];
  topics: Array<{ canonical_name: string; paper_count: number }>;
  evidence: Array<{ source: string; confidence: number; canonical_paper_id: string; title: string; context: { field?: string; snippet?: string; detector?: string } }>;
  teams: null;
};

export type DashboardData = {
  today: string;
  last_updated_at?: string | null;
  material_data_anchor?: string | null;
  latest_data_date?: string | null;
  metrics_range_from?: string | null;
  metrics: Record<string, number>;
  spotlight: PaperSummary[];
  hot_materials: MaterialSummary[];
  latest_run?: DailyRun | null;
  download_queue: Record<string, number>;
};

export type Monitor = {
  id: string;
  name: string;
  query_text: string;
  filters_json: string;
  auto_download_oa: number;
  enabled: number;
  created_at: string;
  updated_at: string;
  last_checked_at?: string | null;
  last_matched_at?: string | null;
  total_hits?: number;
  today_new_hits?: number;
};

export type MonitorHit = {
  id: string;
  monitor_id: string;
  monitor_name: string;
  canonical_paper_id: string;
  paper_version_id: string;
  title?: string | null;
  version_type?: string | null;
  publication_date?: string | null;
  source?: string | null;
  first_matched_at: string;
  last_matched_at: string;
  match_count: number;
};

export type PaperAiAnalysis = {
  provider: string;
  model: string;
  updated_at: string;
  cached?: boolean;
  analysis: {
    summary_zh: string;
    research_question: string;
    methods: string[];
    key_findings: string[];
    materials: string[];
    keywords: string[];
    novelty: string;
    caveats: string[];
  };
};

export type DailyRun = {
  id: string;
  status: string;
  dry_run: number;
  started_at: string;
  finished_at?: string | null;
  trigger_type: string;
  report?: Record<string, unknown>;
  report_json?: string;
  error_message?: string | null;
};

export type SystemStatus = {
  database: { path: string; size_bytes: number; quick_check: unknown; foreign_key_violations: number | null; counts: Record<string, number> };
  pdf_library: { unique_pdfs: number; total_size: number; invalid_files: number; parsed_files: number; pending_parse: number; duplicates: number; unmatched: number | null };
  search: { fts_entries: number; last_content_extracted_at?: string | null; pdf_fulltext_entries: number };
  download_queue: Record<string, number>;
  daily: { runs: DailyRun[]; source_cursors: Array<Record<string, unknown>>; registered: boolean | null; schedule: string | null };
  migration: { installed: boolean; migrations: Array<Record<string, unknown>>; verification: Record<string, unknown> };
};

export type DetailSelection =
  | { kind: 'paper'; id: string }
  | { kind: 'material'; id: string }
  | null;

export type PaperCollection = {
  id: string;
  name: string;
  description: string;
  color: string;
  paper_count: number;
  created_at: string;
  updated_at: string;
  last_paper_added_at?: string | null;
};

export type PaperUserState = {
  canonical_paper_id: string;
  favorite: boolean;
  reading_status: 'unread' | 'later' | 'reading' | 'read' | 'archived';
  note: string;
  created_at?: string | null;
  updated_at?: string | null;
  collections: PaperCollection[];
};

export type RemotePaper = PaperSummary & {
  remote_id: string;
  remote: true;
  sources: string[];
  local_canonical_paper_id?: string | null;
  is_local: boolean;
  condmat_evidence: boolean;
  import_record: Record<string, unknown>;
};

export type RemoteSearchResponse = {
  items: RemotePaper[];
  count: number;
  query: string;
  sources: Record<string, { status: 'ok' | 'error'; count: number; error?: string }>;
  deduplicated: number;
  notice?: string;
};
export type ZoteroPackage = {
  package_id: string;
  output_dir: string;
  paper_count: number;
  attachment_count: number;
  zotero_mode: string;
  ris_download_url: string;
  zip_download_url: string;
};

export type ImmediateDownloadResponse = {
  status: 'completed' | 'failed';
  download: { task_id?: string | null; status: string; paper_file_id?: string; path?: string; error?: string };
  zotero?: ZoteroPackage | null;
};

export type LiveScanStatus = {
  enabled: boolean;
  interval_seconds: number;
  abstract_backfill_limit: number;
  scheduler_running: boolean;
  due: boolean;
  last_scan_at?: string | null;
  next_scan_at?: string | null;
  active_run?: DailyRun | null;
};
export type BatchDownloadRun = {
  id: string;
  status: 'queued' | 'running' | 'completed' | 'partial' | 'failed';
  total_count: number;
  processed_count: number;
  completed_count: number;
  failed_count: number;
  created_at: string;
  started_at?: string | null;
  finished_at?: string | null;
  error_message?: string | null;
  zotero_package_id?: string | null;
  result?: { items?: Array<{ paper_version_id: string; canonical_paper_id?: string; status: string; error?: string | null }>; zotero?: ZoteroPackage | null };
};
