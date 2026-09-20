import { describe, expect, it } from "vitest";

import type { ImportCounts, ImportFailure, ImportRun, SamplePage } from "../api/types";
import {
  canCancel,
  canGoNext,
  canGoPrevious,
  canRetry,
  canStartImport,
  countsOf,
  discoveryErrors,
  emptyStateFor,
  importPollingInterval,
  isPartialFailure,
  libraryPollingInterval,
  pageSummary,
  progressModel,
  retryFoundNothing,
  toFailureView,
} from "./selectors";

const COUNTS: ImportCounts = {
  pending: 3,
  running: 1,
  complete: 6,
  failed: 0,
  cancelled: 0,
  orphaned: 0,
  superseded: 0,
  analyzed: 6,
  reused: 0,
  remaining: 4,
};

function run(overrides: Partial<ImportRun> = {}): ImportRun {
  return {
    analysis_version: "0".repeat(64),
    cancel_requested: false,
    counts: COUNTS,
    current: null,
    failures: [],
    finished_at: null,
    phase: "analyzing",
    role: "kick",
    root_label: "Kicks",
    run_id: "run-1",
    scan: null,
    started_at: null,
    state: "running",
    ...overrides,
  };
}

function page(overrides: Partial<SamplePage["page"]> = {}, items = 2): SamplePage {
  return {
    api_schema: "1.0",
    items: Array.from({ length: items }, (_unused, index) => ({
      analysis: { analysis_version: "0".repeat(64), state: "current" as const },
      audio: { channels: 1, duration_ms: 500, frame_count: 24000, sample_rate_hz: 48000 },
      file_name: `kick-${index}.wav`,
      file_status: "present" as const,
      role: "kick" as const,
      sample_id: `sha256:${String(index).padStart(64, "0")}`,
    })),
    page: { count: items, has_more: false, limit: 50, next_cursor: null, ...overrides },
    query: { roles: [], text: null },
  };
}

describe("progressModel", () => {
  it("is indeterminate while scanning", () => {
    expect(progressModel(COUNTS, "scanning")).toEqual({ mode: "indeterminate", percent: null });
  });

  it("is indeterminate when the denominator is zero", () => {
    const counts = { ...COUNTS, analyzed: 0, remaining: 0 };
    expect(progressModel(counts, "analyzing")).toEqual({ mode: "indeterminate", percent: null });
  });

  it("reports the rounded share of the drain", () => {
    const counts = { ...COUNTS, analyzed: 3, remaining: 7 };
    expect(progressModel(counts, "analyzing")).toEqual({ mode: "determinate", percent: 30 });
  });

  it("reaches one hundred for a finished run and never leaves the range", () => {
    expect(progressModel({ ...COUNTS, analyzed: 10, remaining: 0 }, "complete")).toEqual({
      mode: "determinate",
      percent: 100,
    });
    expect(progressModel({ ...COUNTS, analyzed: 20, remaining: 0 }, "complete").percent).toBe(100);
  });
});

describe("toFailureView", () => {
  it("keeps five fields and drops everything else, including a message and a path", () => {
    const response = {
      sample_id: "sha256:aaaa",
      file_name: "kick-55.wav",
      stage: "extract",
      code: "unsupported_channels",
      attempts: 2,
      message: "C:\\Samples\\Kicks\\kick-55.wav could not be read",
      path: "C:\\Samples\\Kicks\\kick-55.wav",
      root: "C:\\Samples\\Kicks",
    } as unknown as ImportFailure;
    const view = toFailureView(response);
    expect(Object.keys(view).sort()).toEqual([
      "attempts",
      "code",
      "file_name",
      "sample_id",
      "stage",
    ]);
    expect(JSON.stringify(view)).not.toContain("Samples");
    expect(JSON.stringify(view)).not.toContain("\\\\");
    expect(view.file_name).not.toMatch(/[\\/]|[A-Za-z]:/);
  });
});

describe("the polling cadence", () => {
  it("polls the run every second while it is live and never after", () => {
    expect(importPollingInterval("running")).toBe(1000);
    for (const state of ["complete", "cancelled", "interrupted", "failed"] as const) {
      expect(importPollingInterval(state)).toBe(0);
    }
  });

  it("refreshes the list every five seconds while a run is live and never after", () => {
    expect(libraryPollingInterval("running")).toBe(5000);
    for (const state of ["complete", "cancelled", "interrupted", "failed"] as const) {
      expect(libraryPollingInterval(state)).toBe(0);
    }
    expect(libraryPollingInterval(null)).toBe(0);
  });
});

describe("the enablement rules", () => {
  it("offer cancel only for a live run that has not been asked to stop", () => {
    expect(canCancel(null)).toBe(false);
    expect(canCancel(run())).toBe(true);
    expect(canCancel(run({ cancel_requested: true }))).toBe(false);
    expect(canCancel(run({ state: "complete" }))).toBe(false);
  });

  it("offer retry only once a run stopped with failures", () => {
    expect(canRetry(null)).toBe(false);
    expect(canRetry(run())).toBe(false);
    expect(canRetry(run({ state: "complete", counts: { ...COUNTS, failed: 2 } }))).toBe(true);
    expect(canRetry(run({ state: "complete" }))).toBe(false);
  });

  it("refuse a second start while a run is live", () => {
    expect(canStartImport(run())).toBe(false);
    expect(canStartImport(run({ state: "cancelled" }))).toBe(true);
  });

  it("call a completed run with failures a partial failure", () => {
    expect(isPartialFailure(run({ state: "complete", counts: { ...COUNTS, failed: 1 } }))).toBe(true);
    expect(isPartialFailure(run({ state: "failed", counts: { ...COUNTS, failed: 1 } }))).toBe(false);
    expect(isPartialFailure(run({ state: "complete" }))).toBe(false);
  });

  it("note a retry that found nothing", () => {
    expect(retryFoundNothing(run({ state: "complete" }))).toBe(true);
    expect(retryFoundNothing(run({ state: "complete", counts: { ...COUNTS, failed: 1 } }))).toBe(false);
    expect(retryFoundNothing(run())).toBe(false);
  });
});

describe("the run's projections", () => {
  it("always report all ten counts", () => {
    expect(countsOf(null)).toBeNull();
    expect(Object.keys(countsOf(run()) ?? {}).length).toBe(10);
    const partial = run({ counts: { analyzed: 1 } as unknown as ImportCounts });
    expect(countsOf(partial)?.pending).toBe(0);
    expect(countsOf(partial)?.analyzed).toBe(1);
  });

  it("report no discovery count when this process did not scan", () => {
    expect(discoveryErrors(run())).toBeNull();
    expect(
      discoveryErrors(run({ scan: { counts: { discovery_errors: 2 }, files: [] } })),
    ).toBe(2);
  });
});

describe("the empty states", () => {
  it("separates an empty library from filters that match nothing", () => {
    expect(emptyStateFor(page({}, 0), [], "")).toBe("empty");
    expect(emptyStateFor(page({}, 0), ["kick"], "")).toBe("no-matches");
    expect(emptyStateFor(page({}, 0), [], "kick")).toBe("no-matches");
    expect(emptyStateFor(page(), [], "")).toBeNull();
    expect(emptyStateFor(undefined, [], "")).toBeNull();
  });
});

describe("the pager", () => {
  it("lets only has_more gate Next, whatever the cursor says", () => {
    expect(canGoNext(page({ has_more: true, next_cursor: "c1" }))).toBe(true);
    expect(canGoNext(page({ has_more: false, next_cursor: null }))).toBe(false);
    expect(canGoNext(page({ has_more: false, next_cursor: "c1" }))).toBe(false);
    expect(canGoNext(undefined)).toBe(false);
  });

  it("disables Previous on the first page", () => {
    expect(canGoPrevious(0)).toBe(false);
    expect(canGoPrevious(1)).toBe(true);
  });

  it("reports how many are shown, never a library total", () => {
    expect(pageSummary(page())).toBe("Showing 2, no more");
    expect(pageSummary(page({ has_more: true }))).toBe("Showing 2");
  });
});
