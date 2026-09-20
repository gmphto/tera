/**
 * One named fixture per awkward recommendation response (issue #33).
 *
 * Every id is synthetic: `palette-001`, `project-001`, a bare 64-character hex
 * `run_id`, and `sha256:` plus 64 hex characters for samples and candidates. No
 * path, file name, pack name, credential or audio byte appears anywhere, and the
 * shapes are transcriptions of #28's documented 200s and #27's envelope.
 */

import {
  API_SCHEMA,
  BATCH_SCHEMA,
  type BatchWire,
  type RankedCandidateWire,
  type RunWire,
} from "./batch";

export const RUN_ID = "a".repeat(64);
export const PALETTE_ID = "palette-001";
export const PROJECT_ID = "project-001";

export function candidateId(index: number): string {
  return `sha256:${String(index).padStart(64, "0")}`;
}

export function candidate(rank: number, overrides: Partial<RankedCandidateWire> = {}): RankedCandidateWire {
  return {
    candidate_id: candidateId(rank),
    analysis_version: "b".repeat(64),
    rank,
    compatibility: 0.9 - rank * 0.1,
    confidence: 0.8,
    similarity: 0.5,
    similarity_unavailable_reason: null,
    dsp_dimensions: [],
    jev_judgments: [],
    reasons: [],
    warnings: [],
    ...overrides,
  };
}

export function counts(overrides: Partial<RunWire["counts"]> = {}): RunWire["counts"] {
  return {
    eligible: 5,
    excluded: 0,
    shortlisted: 5,
    scored: 5,
    unscored: 0,
    requested: 10,
    returned: 5,
    ...overrides,
  };
}

export function run(overrides: Partial<RunWire> = {}): RunWire {
  return {
    run_id: RUN_ID,
    palette_hash: "c".repeat(64),
    analysis_version: "b".repeat(64),
    filters: { policy_version: "filter-policy-v1", tempo_lock: false, exact_key_lock: false },
    retrieval: {
      policy_version: "retrieval-policy-v1",
      representation_version: "retrieval-representation-v1",
      normalization_id: "retrieval-normalization-v1",
      shortlist_size_requested: 50,
      shortlist_size_returned: 5,
      limit_reason: "limited_by_request",
      duplicate_candidates: 0,
      skipped_stale_analysis: 0,
      skipped_absent_analysis: 0,
    },
    ranking: {
      ranking_version: "hybrid-ranking-v1",
      weight_table_id: "hybrid-weights-v1",
      jev_status: "jev_absent",
    },
    jev: { prompt_version: "jev-questions-v1", adapter_version: "jev-adapter-v1", model_versions: [] },
    counts: counts(),
    exclusions: [],
    evidence: { interface: 0, double: 0, unavailable: 0, cache: 0, cache_errors: 0 },
    ...overrides,
  };
}

export function batch(overrides: Partial<BatchWire> = {}): BatchWire {
  return {
    run_id: RUN_ID,
    palette: {
      palette_id: PALETTE_ID,
      revision: 4,
      kick_id: candidateId(99),
      selected_bass_id: null,
    },
    samples: [],
    results: [candidate(1), candidate(2), candidate(3), candidate(4), candidate(5)],
    ranking_version: "hybrid-ranking-v1",
    mode: "dsp-only",
    alternatives: [candidateId(2)],
    schema_version: BATCH_SCHEMA,
    ...overrides,
  };
}

export function response(
  overrides: { batch?: Partial<BatchWire>; run?: Partial<RunWire> } = {},
): { api_schema: string; recommendation: BatchWire; run: RunWire } {
  return {
    api_schema: API_SCHEMA,
    recommendation: batch(overrides.batch),
    run: run(overrides.run),
  };
}

export function envelope(code: string, details: Record<string, unknown> = {}) {
  return {
    api_schema: API_SCHEMA,
    error: { code, message: "refused", details },
  };
}

/** Hybrid with five results and one alternative. */
export const HYBRID_FIVE = response({
  batch: {
    mode: "hybrid",
    results: [
      candidate(1, { jev_judgments: [{ dimension: "frequency" }], confidence: 0.9 }),
      candidate(2, { jev_judgments: [{ dimension: "transient" }] }),
      candidate(3),
      candidate(4),
      candidate(5),
    ],
    alternatives: [candidateId(2), candidateId(3)],
  },
  run: { ranking: { ranking_version: "hybrid-ranking-v1", weight_table_id: "hybrid-weights-v1", jev_status: "jev_present" } },
});

/** `dsp-only` with empty Jev judgments. */
export const DSP_ONLY = response();

/** A result whose similarity is null with its reason. */
export const NULL_SIMILARITY = response({
  batch: {
    results: [
      candidate(1, { similarity: null, similarity_unavailable_reason: "insufficient_common_dimensions" }),
      candidate(2, { similarity: null, similarity_unavailable_reason: "insufficient_common_dimensions" }),
    ],
  },
  run: { counts: counts({ eligible: 2, shortlisted: 2, scored: 2, returned: 2 }) },
});

/** Two candidates that could not be scored. */
export const UNSCORED_TWO = response({
  run: { counts: counts({ eligible: 7, scored: 5, unscored: 2 }) },
});

/** Every candidate excluded, with three codes. */
export const ALL_EXCLUDED = response({
  batch: { results: [], alternatives: [] },
  run: {
    counts: counts({ eligible: 0, excluded: 3, shortlisted: 0, scored: 0, returned: 0 }),
    retrieval: { ...run().retrieval, limit_reason: "no_candidates" },
    exclusions: [
      { code: "file_missing", count: 1 },
      { code: "inconsistent_analysis", count: 1 },
      { code: "silent_audio", count: 1 },
    ],
  },
});

/** An empty library: no candidate was ever eligible. */
export const NO_CANDIDATES = response({
  batch: { results: [], alternatives: [] },
  run: {
    counts: counts({ eligible: 0, excluded: 0, shortlisted: 0, scored: 0, returned: 0 }),
    retrieval: { ...run().retrieval, limit_reason: "no_candidates" },
  },
});

/** #27's conflict envelope. */
export const REVISION_CONFLICT = envelope("revision_conflict", {
  expected_revision: 4,
  current_revision: 6,
});

/** A payload whose run block is missing. */
export const MALFORMED = { api_schema: API_SCHEMA, recommendation: batch(), run: {} };

/** A payload from a schema this client does not read. */
export const UNSUPPORTED_SCHEMA = { api_schema: "2.0", recommendation: batch(), run: run() };

/** A payload whose batch schema is newer than this client reads. */
export const UNSUPPORTED_BATCH_SCHEMA = {
  api_schema: API_SCHEMA,
  recommendation: batch({ schema_version: "2.0" }),
  run: run(),
};

/** A payload whose ranks skip a number. */
export const NON_CONTIGUOUS_RANKS = response({
  batch: { results: [candidate(1), candidate(3)] },
});

/** A payload that repeats a candidate. */
export const REPEATED_CANDIDATE = response({
  batch: { results: [candidate(1), candidate(2, { candidate_id: candidateId(1) })] },
});

/** A payload whose alternative is not a returned result. */
export const ALTERNATIVE_OUTSIDE_RESULTS = response({
  batch: { alternatives: [candidateId(8)] },
});
