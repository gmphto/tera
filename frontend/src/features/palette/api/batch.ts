/**
 * The one parser and validator for a recommendation response (issue #33).
 *
 * Pure, so the awkward payloads are covered without a DOM or a service. The
 * panel renders no card from a payload that failed here, and nothing in the
 * client re-derives a compatibility, a confidence, a mode, a ranking version, an
 * alternative or an uncertainty flag: the parsed values are the payload's.
 */

/** The payload's own schema version, from #28's projection. */
export const API_SCHEMA = "1.0";
export const BATCH_SCHEMA = "1.0";

/** What this client may report when the payload itself is wrong. */
export const CLIENT_ERROR_CODES = ["unsupported_schema", "malformed_response"] as const;
export type ClientErrorCode = (typeof CLIENT_ERROR_CODES)[number];

/** The seven count keys and the five evidence keys #28 documents. */
export const COUNT_KEYS = [
  "eligible",
  "excluded",
  "shortlisted",
  "scored",
  "unscored",
  "requested",
  "returned",
] as const;
export const EVIDENCE_KEYS = ["interface", "double", "unavailable", "cache", "cache_errors"] as const;

export interface RecommendationCounts {
  eligible: number;
  excluded: number;
  shortlisted: number;
  scored: number;
  unscored: number;
  requested: number;
  returned: number;
}

export interface RankedCandidateWire {
  candidate_id: string;
  analysis_version: string;
  rank: number;
  compatibility: number;
  confidence: number;
  similarity: number | null;
  similarity_unavailable_reason: string | null;
  dsp_dimensions: unknown[];
  jev_judgments: unknown[];
  reasons: string[];
  warnings: string[];
}

export interface RunWire {
  run_id: string;
  palette_hash: string;
  analysis_version: string;
  filters: { policy_version: string; tempo_lock: boolean; exact_key_lock: boolean };
  retrieval: {
    policy_version: string;
    representation_version: string;
    normalization_id: string;
    shortlist_size_requested: number;
    shortlist_size_returned: number;
    limit_reason: string;
    duplicate_candidates: number;
    skipped_stale_analysis: number;
    skipped_absent_analysis: number;
  };
  ranking: { ranking_version: string; weight_table_id: string; jev_status: string };
  jev: { prompt_version: string; adapter_version: string; model_versions: string[] };
  counts: RecommendationCounts;
  exclusions: { code: string; count: number }[];
  evidence: Record<string, number>;
}

export interface BatchWire {
  run_id: string;
  palette: {
    palette_id: string;
    revision: number;
    kick_id: string;
    selected_bass_id: string | null;
  };
  samples: unknown[];
  results: RankedCandidateWire[];
  ranking_version: string;
  mode: string;
  alternatives: string[];
  schema_version: string;
}

export interface RecommendationResponse {
  recommendation: BatchWire;
  run: RunWire;
}

export type BatchParse =
  | { ok: true; batch: BatchWire; run: RunWire }
  | { ok: false; code: ClientErrorCode }
  | { ok: false; code: "stale_revision"; currentRevision: number | null };

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function finiteUnit(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value) && value >= 0 && value <= 1;
}

function hasKeys(value: unknown, keys: readonly string[]): boolean {
  return isRecord(value) && keys.every((key) => key in value);
}

/**
 * One response as the batch and its run block, or the closed code that refused
 * it. #27's envelope codes pass through unchanged, so a caller maps them with the
 * same table it uses for the palette routes.
 */
export function parseBatch(payload: unknown): BatchParse {
  if (!isRecord(payload) || payload.api_schema !== API_SCHEMA) {
    return { ok: false, code: "unsupported_schema" };
  }
  if (isRecord(payload.error)) {
    const code = payload.error.code;
    const details = isRecord(payload.error.details) ? payload.error.details : {};
    const current = details.current_revision;
    if (code === "revision_conflict") {
      return {
        ok: false,
        code: "stale_revision",
        currentRevision: typeof current === "number" ? current : null,
      };
    }
    return { ok: false, code: "malformed_response" };
  }
  const batch = payload.recommendation;
  const run = payload.run;
  if (!isRecord(batch) || batch.schema_version !== BATCH_SCHEMA) {
    return { ok: false, code: "unsupported_schema" };
  }
  if (!isRecord(run)) {
    return { ok: false, code: "malformed_response" };
  }
  if (!hasKeys(run.counts, COUNT_KEYS) || !hasKeys(run.evidence, EVIDENCE_KEYS)) {
    return { ok: false, code: "malformed_response" };
  }
  if (!isRecord(run.retrieval) || !("limit_reason" in run.retrieval)) {
    return { ok: false, code: "malformed_response" };
  }
  if (!isRecord(run.ranking) || typeof run.ranking.jev_status !== "string") {
    return { ok: false, code: "malformed_response" };
  }
  if (!Array.isArray(batch.results) || !Array.isArray(batch.alternatives)) {
    return { ok: false, code: "malformed_response" };
  }

  const seen = new Set<string>();
  const results = batch.results as unknown[];
  for (const [index, candidate] of results.entries()) {
    if (!isRecord(candidate)) {
      return { ok: false, code: "malformed_response" };
    }
    if (candidate.rank !== index + 1) {
      return { ok: false, code: "malformed_response" };
    }
    const id = candidate.candidate_id;
    if (typeof id !== "string" || seen.has(id)) {
      return { ok: false, code: "malformed_response" };
    }
    seen.add(id);
    if (!finiteUnit(candidate.compatibility) || !finiteUnit(candidate.confidence)) {
      return { ok: false, code: "malformed_response" };
    }
    if (!Array.isArray(candidate.warnings) || !Array.isArray(candidate.jev_judgments)) {
      return { ok: false, code: "malformed_response" };
    }
  }
  for (const alternative of batch.alternatives) {
    if (typeof alternative !== "string" || !seen.has(alternative)) {
      return { ok: false, code: "malformed_response" };
    }
  }
  return { ok: true, batch: batch as unknown as BatchWire, run: run as unknown as RunWire };
}

/** The retryable codes, from #27's table and this client's own. */
export const RETRYABLE_CODES = [
  "database_locked",
  "internal_error",
  "stale_revision",
  "service_unavailable",
] as const;

export function isRetryable(code: string): boolean {
  return (RETRYABLE_CODES as readonly string[]).includes(code);
}
