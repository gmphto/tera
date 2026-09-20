/**
 * The #27 vocabulary this feature mirrors, and nothing of its own (issue #31).
 *
 * Every name here is a spelling #27 or #9 already owns: the role set, the file
 * and analysis states, the run state and phase, and the ten count keys. A
 * humanised label may sit beside a value as visible text; a second state name
 * may not exist anywhere in code, state, the DOM or `_docs/library-browser.md`.
 */

/** The Phase 1 role set, mirrored from `backend.analysis.batch.ROLES`. */
export const PALETTE_ROLES = ["kick", "bass", "sub-bass"] as const;
export type PaletteRole = (typeof PALETTE_ROLES)[number];

/** #21's stored file availability. */
export const FILE_STATUSES = ["present", "missing", "unknown", "unreadable"] as const;
export type FileStatus = (typeof FILE_STATUSES)[number];

/** #27's analysis availability for one listed sample. */
export const ANALYSIS_STATES = ["current", "stale", "pending", "failed", "absent"] as const;
export type AnalysisState = (typeof ANALYSIS_STATES)[number];

/** #23's run state. */
export const RUN_STATES = ["running", "complete", "cancelled", "interrupted", "failed"] as const;
export type RunState = (typeof RUN_STATES)[number];
export const TERMINAL_RUN_STATES = ["complete", "cancelled", "interrupted", "failed"] as const;

/** #27's phase: `scanning`, `analyzing`, then the run's own terminal state. */
export const IMPORT_PHASES = ["scanning", "analyzing", ...RUN_STATES] as const;
export type ImportPhase = (typeof IMPORT_PHASES)[number];

/** #23's summary counts, exactly these ten keys. */
export const COUNT_KEYS = [
  "pending",
  "running",
  "complete",
  "failed",
  "cancelled",
  "orphaned",
  "superseded",
  "analyzed",
  "reused",
  "remaining",
] as const;
export type CountKey = (typeof COUNT_KEYS)[number];
export type ImportCounts = Record<CountKey, number>;

/** The client's own constants, none of which is a service threshold. */
export const PAGE_SIZE_OPTIONS = [25, 50, 100] as const;
export const DEFAULT_PAGE_SIZE = 50;
export const MAX_QUERY_LENGTH = 200;
export const SEARCH_DEBOUNCE_MS = 250;
export const IMPORT_POLL_INTERVAL_MS = 1000;
export const LIBRARY_REFRESH_INTERVAL_MS = 5000;

/** The visible label for a state value, used beside the attribute, never instead of it. */
export const COUNT_LABELS: Record<CountKey, string> = {
  pending: "Queued",
  running: "Running",
  complete: "Complete",
  failed: "Failed",
  cancelled: "Cancelled",
  orphaned: "Orphaned",
  superseded: "Superseded",
  analyzed: "Analyzed",
  reused: "Reused",
  remaining: "Remaining",
};

export interface SampleAudio {
  channels: number;
  duration_ms: number;
  frame_count: number;
  sample_rate_hz: number;
}

export interface SampleAnalysis {
  analysis_version: string | null;
  state: AnalysisState;
}

export interface SampleListItem {
  analysis: SampleAnalysis;
  audio: SampleAudio;
  file_name: string;
  file_status: FileStatus;
  role: PaletteRole;
  sample_id: string;
}

export interface SamplePageEcho {
  roles: PaletteRole[];
  text: string | null;
}

export interface SamplePage {
  api_schema: string;
  items: SampleListItem[];
  page: {
    count: number;
    has_more: boolean;
    limit: number;
    next_cursor: string | null;
  };
  query: SamplePageEcho;
}

/** The detail response. Only `sample_id` and `file_status` are ever read here. */
export interface SampleDetailResponse {
  api_schema: string;
  sample: {
    analysis: SampleAnalysis & { attempts?: number | null; error_code?: string | null };
    audio: SampleAudio;
    file_name: string;
    file_status: FileStatus;
    role: PaletteRole;
    sample_id: string;
  };
}

export interface ImportFailure {
  sample_id: string;
  file_name: string;
  stage: string | null;
  code: string | null;
  attempts: number | null;
}

export interface ImportScanFile {
  sample_id: string;
  file_name: string;
  code: string | null;
  analysis: string | null;
  error_code: string | null;
  stage: string | null;
}

export interface ImportScan {
  counts: { discovery_errors: number } & Record<string, number>;
  files: ImportScanFile[];
}

export interface ImportCurrent {
  file_name: string;
  attempts: number;
}

export interface ImportRun {
  analysis_version: string | null;
  cancel_requested: boolean;
  counts: ImportCounts;
  current: ImportCurrent | null;
  failures: ImportFailure[];
  finished_at: string | null;
  phase: ImportPhase;
  role: PaletteRole | null;
  root_label: string;
  run_id: string;
  scan: ImportScan | null;
  started_at: string | null;
  state: RunState;
}

export interface ImportStatusResponse {
  api_schema: string;
  import: ImportRun;
}

export interface ImportStartResponse {
  api_schema: string;
  import: {
    analysis_version: string;
    phase: ImportPhase;
    role: PaletteRole;
    run_id: string;
    started_at: string;
    state: RunState;
    status_path: string;
  };
}

export interface ImportCancelResponse {
  api_schema: string;
  import: {
    cancel_requested: boolean;
    run_id: string;
    state: RunState;
    status_path: string;
  };
}

export interface ImportRetryResponse {
  api_schema: string;
  import: { retried: number; run_id: string; status_path: string };
}

/** The arguments one `GET /library/samples` request is built from. */
export interface SampleQueryArgs {
  roles: PaletteRole[];
  text: string | null;
  limit: number;
  cursor: string | null;
}
