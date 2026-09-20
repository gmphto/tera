import { describe, expect, it } from "vitest";

import {
  API_SCHEMA,
  CLIENT_ERROR_CODES,
  COUNT_KEYS,
  EVIDENCE_KEYS,
  isRetryable,
  parseBatch,
} from "./batch";
import {
  ALL_EXCLUDED,
  ALTERNATIVE_OUTSIDE_RESULTS,
  DSP_ONLY,
  HYBRID_FIVE,
  MALFORMED,
  NON_CONTIGUOUS_RANKS,
  NO_CANDIDATES,
  NULL_SIMILARITY,
  REPEATED_CANDIDATE,
  REVISION_CONFLICT,
  UNSCORED_TWO,
  UNSUPPORTED_BATCH_SCHEMA,
  UNSUPPORTED_SCHEMA,
  candidateId,
  response,
} from "./fixtures";

function parsed(fixture: unknown) {
  const result = parseBatch(fixture);
  if (!result.ok) {
    throw new Error(`expected a parsed batch, got ${JSON.stringify(result)}`);
  }
  return result;
}

function refused(fixture: unknown) {
  const result = parseBatch(fixture);
  if (result.ok) {
    throw new Error("expected a refusal");
  }
  return result;
}

describe("the fixtures parse with their values preserved", () => {
  it("hybrid with five results and one alternative", () => {
    const result = parsed(HYBRID_FIVE);
    expect(result?.batch.mode).toBe("hybrid");
    expect(result?.batch.results.map((item) => item.rank)).toEqual([1, 2, 3, 4, 5]);
    expect(result?.batch.alternatives).toEqual([candidateId(2), candidateId(3)]);
    expect(result?.run.ranking.jev_status).toBe("jev_present");
  });

  it("dsp-only with empty Jev judgments", () => {
    const result = parsed(DSP_ONLY);
    expect(result?.batch.mode).toBe("dsp-only");
    expect(result?.batch.results.every((item) => item.jev_judgments.length === 0)).toBe(true);
  });

  it("a null similarity keeps its reason", () => {
    const result = parsed(NULL_SIMILARITY);
    expect(result?.batch.results[0].similarity).toBeNull();
    expect(result?.batch.results[0].similarity_unavailable_reason).toBe(
      "insufficient_common_dimensions",
    );
  });

  it("two unscored candidates are counted, not listed", () => {
    const result = parsed(UNSCORED_TWO);
    expect(result?.run.counts.unscored).toBe(2);
    expect(result?.batch.results).toHaveLength(5);
  });

  it("all-excluded keeps its counts and its codes", () => {
    const result = parsed(ALL_EXCLUDED);
    expect(result?.batch.results).toEqual([]);
    expect(result?.run.counts.eligible).toBe(0);
    expect(result?.run.counts.excluded).toBe(3);
    expect(result?.run.exclusions).toEqual([
      { code: "file_missing", count: 1 },
      { code: "inconsistent_analysis", count: 1 },
      { code: "silent_audio", count: 1 },
    ]);
    expect(result?.run.retrieval.limit_reason).toBe("no_candidates");
  });

  it("no candidates is distinguishable from all-excluded by counts alone", () => {
    const result = parsed(NO_CANDIDATES);
    expect(result?.run.counts.eligible).toBe(0);
    expect(result?.run.counts.excluded).toBe(0);
    expect(result?.run.retrieval.limit_reason).toBe("no_candidates");
  });
});

describe("the closed client codes", () => {
  it("refuses a payload from another api schema", () => {
    expect(refused(UNSUPPORTED_SCHEMA).code).toBe("unsupported_schema");
  });

  it("refuses a batch whose schema version is newer", () => {
    expect(refused(UNSUPPORTED_BATCH_SCHEMA).code).toBe("unsupported_schema");
  });

  it("is exactly the two codes this client reports", () => {
    expect([...CLIENT_ERROR_CODES]).toEqual(["unsupported_schema", "malformed_response"]);
  });
});

describe("malformed payloads", () => {
  it("refuses a missing run block", () => {
    expect(refused(MALFORMED).code).toBe("malformed_response");
  });

  it("refuses ranks that are not contiguous from one", () => {
    expect(refused(NON_CONTIGUOUS_RANKS).code).toBe("malformed_response");
  });

  it("refuses a repeated candidate id", () => {
    expect(refused(REPEATED_CANDIDATE).code).toBe("malformed_response");
  });

  it("refuses an alternative that is not a returned result", () => {
    expect(refused(ALTERNATIVE_OUTSIDE_RESULTS).code).toBe("malformed_response");
  });

  it("refuses a count key that is missing", () => {
    const broken = response();
    for (const key of COUNT_KEYS) {
      const trimmed: Record<string, unknown> = { ...broken.run.counts };
      delete trimmed[key];
      expect(refused({ ...broken, run: { ...broken.run, counts: trimmed } }).code, key).toBe(
        "malformed_response",
      );
    }
  });

  it("refuses an evidence key that is missing", () => {
    for (const key of EVIDENCE_KEYS) {
      const broken = response();
      const trimmed: Record<string, unknown> = { ...broken.run.evidence };
      delete trimmed[key];
      expect(refused({ ...broken, run: { ...broken.run, evidence: trimmed } }).code, key).toBe(
        "malformed_response",
      );
    }
  });

  it("refuses a compatibility or a confidence outside the unit interval", () => {
    for (const value of [-0.1, 1.1, Number.NaN, Number.POSITIVE_INFINITY, "0.5", null]) {
      const broken = response({ batch: { results: [candidateWith({ compatibility: value })] } });
      expect(refused(broken).code, String(value)).toBe("malformed_response");
      const other = response({ batch: { results: [candidateWith({ confidence: value })] } });
      expect(refused(other).code, String(value)).toBe("malformed_response");
    }
  });

  it("refuses a retrieval block without its limit reason", () => {
    const broken = response();
    expect(parsed(broken)).not.toBeNull();
    expect(
      refused({ ...broken, run: { ...broken.run, retrieval: { policy_version: "v1" } } }).code,
    ).toBe("malformed_response");
  });
});

describe("#27's envelope", () => {
  it("passes a conflict through with the current revision", () => {
    const result = refused(REVISION_CONFLICT);
    expect(result.code).toBe("stale_revision");
    expect(result.ok ? null : (result as { currentRevision?: number }).currentRevision).toBe(6);
  });

  it("reports a conflict without a revision as a null revision rather than a guess", () => {
    const result = refused({ api_schema: API_SCHEMA, error: { code: "revision_conflict", details: {} } });
    expect(result.code).toBe("stale_revision");
    expect((result as { currentRevision?: number | null }).currentRevision).toBeNull();
  });
});

describe("retryability", () => {
  it("retries only the documented set", () => {
    for (const code of ["database_locked", "internal_error", "stale_revision", "service_unavailable"]) {
      expect(isRetryable(code), code).toBe(true);
    }
    for (const code of [
      "unknown_palette",
      "palette_incomplete",
      "kick_unavailable",
      "malformed_response",
      "unsupported_schema",
    ]) {
      expect(isRetryable(code), code).toBe(false);
    }
  });
});

function candidateWith(overrides: Record<string, unknown>) {
  const base = response().recommendation.results[0];
  return { ...base, ...overrides } as unknown as typeof base;
}
