/**
 * The rendered states of the library browser (issue #31).
 *
 * Every component here is presentational, so this file renders it from a fixture
 * view model with `renderToStaticMarkup` and asserts the `data-testid` and the
 * `data-*` attributes the issue names as the checkable surface.
 */

import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import type { LibraryError } from "../api/errors";
import type { ImportCounts, ImportRun, SampleListItem, SamplePage, SampleQueryArgs } from "../api/types";
import { ImportPanel } from "./ImportPanel";
import { LibraryFilters } from "./LibraryFilters";
import { LibraryResults } from "./LibraryResults";
import { PageControls } from "./PageControls";
import { SampleRow } from "./SampleRow";

const NOOP = () => undefined;
const ARGS: SampleQueryArgs = { roles: [], text: null, limit: 50, cursor: null };

function has(markup: string, testid: string): boolean {
  return markup.includes(`data-testid="${testid}"`);
}

function attr(markup: string, testid: string, name: string): string | null {
  const element = markup.match(new RegExp(`<[a-z]+[^>]*data-testid="${testid}"[^>]*>`));
  if (element === null) {
    return null;
  }
  const found = element[0].match(new RegExp(`data-${name}="([^"]*)"`));
  return found === null ? null : found[1];
}

function tagOf(markup: string, testid: string): string | null {
  const element = markup.match(new RegExp(`<([a-z]+)[^>]*data-testid="${testid}"`));
  return element === null ? null : element[1];
}

function item(overrides: Partial<SampleListItem> = {}): SampleListItem {
  return {
    analysis: { analysis_version: "0".repeat(64), state: "current" },
    audio: { channels: 1, duration_ms: 500, frame_count: 24000, sample_rate_hz: 48000 },
    file_name: "kick-55.wav",
    file_status: "present",
    role: "kick",
    sample_id: `sha256:${"a".repeat(64)}`,
    ...overrides,
  };
}

function page(overrides: Partial<SamplePage["page"]> = {}, items: SampleListItem[] = [item()]): SamplePage {
  return {
    api_schema: "1.0",
    items,
    page: { count: items.length, has_more: false, limit: 50, next_cursor: null, ...overrides },
    query: { roles: [], text: null },
  };
}

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
    run_id: "run-1e2ce2e140063b8fd2ab0cc64001488b",
    scan: null,
    started_at: null,
    state: "running",
    ...overrides,
  };
}

function results(overrides: Partial<Parameters<typeof LibraryResults>[0]> = {}): string {
  return renderToStaticMarkup(
    <LibraryResults
      page={page()}
      args={ARGS}
      serviceReady
      isFetching={false}
      isFirstLoad={false}
      error={null}
      staleSelection={false}
      selectedSampleId={null}
      onRetry={NOOP}
      onClearFilters={NOOP}
      onSelect={NOOP}
      onImport={NOOP}
      {...overrides}
    />,
  );
}

function panel(overrides: Partial<Parameters<typeof ImportPanel>[0]> = {}): string {
  return renderToStaticMarkup(
    <ImportPanel
      role="kick"
      run={null}
      pickerReason={null}
      startError={null}
      chosenLabel={null}
      retryNote={false}
      onRoleChange={NOOP}
      onPick={NOOP}
      onCancel={NOOP}
      onRetry={NOOP}
      {...overrides}
    />,
  );
}

describe("the results area", () => {
  it("shows a loading line and no counts on the first load", () => {
    const markup = results({ page: undefined, isFirstLoad: true });
    expect(has(markup, "library-loading")).toBe(true);
    expect(markup).toContain('aria-busy="true"');
    expect(has(markup, "sample-row")).toBe(false);
    expect(has(markup, "library-results")).toBe(false);
  });

  it("keeps the rows on screen while refreshing", () => {
    const markup = results({ isFetching: true });
    expect(attr(markup, "library-results", "busy")).toBe("true");
    expect(has(markup, "sample-row")).toBe(true);
  });

  it("labels the list with the filters the service echoed", () => {
    const markup = results({
      page: { ...page(), query: { roles: ["kick", "bass"], text: "kick" } },
    });
    expect(attr(markup, "library-results", "query-text")).toBe("kick");
    expect(attr(markup, "library-results", "query-roles")).toBe("kick,bass");
  });

  it("offers the picker when the library is empty and nothing is filtered", () => {
    const markup = results({ page: page({ count: 0 }, []) });
    expect(has(markup, "library-empty")).toBe(true);
    expect(has(markup, "library-empty-import")).toBe(true);
    expect(has(markup, "library-no-matches")).toBe(false);
  });

  it("offers a filter reset when nothing matches", () => {
    const markup = results({
      page: { ...page({ count: 0 }, []), query: { roles: ["kick"], text: null } },
      args: { roles: ["kick"], text: null, limit: 50, cursor: null },
    });
    expect(has(markup, "library-no-matches")).toBe(true);
    expect(has(markup, "library-clear-filters")).toBe(true);
    expect(has(markup, "library-empty")).toBe(false);
  });

  it("reports a degraded database beside the service panel", () => {
    const error: LibraryError = {
      kind: "api",
      code: "database_unavailable",
      status: 503,
      details: {},
    };
    const markup = results({ error });
    expect(attr(markup, "library-error", "kind")).toBe("api");
    expect(attr(markup, "library-error", "code")).toBe("database_unavailable");
    expect(attr(markup, "library-degraded", "code")).toBe("database_unavailable");
    expect(has(markup, "library-retry")).toBe(true);
  });

  it("reports a refused connection as a transport failure", () => {
    const error: LibraryError = { kind: "transport", reason: "connection_refused" };
    const markup = results({ error });
    expect(attr(markup, "library-error", "kind")).toBe("transport");
    expect(attr(markup, "library-error", "reason")).toBe("connection_refused");
    expect(has(markup, "library-degraded")).toBe(false);
  });

  it("asks for no library at all while the service is not ready", () => {
    const markup = results({ serviceReady: false });
    expect(has(markup, "library-service-unavailable")).toBe(true);
    expect(has(markup, "sample-row")).toBe(false);
    expect(has(markup, "library-results")).toBe(false);
  });

  it("announces a selection the service no longer knows", () => {
    const markup = results({ staleSelection: true, selectedSampleId: null });
    expect(has(markup, "selection-stale")).toBe(true);
  });

  it("marks the selected row by identity", () => {
    const markup = results({ selectedSampleId: item().sample_id });
    expect(attr(markup, "sample-row", "selected")).toBe("true");
  });
});

describe("a sample row", () => {
  it("carries identity, both statuses and the audio facts", () => {
    const markup = renderToStaticMarkup(
      <SampleRow item={item()} selected={false} onSelect={NOOP} />,
    );
    expect(attr(markup, "sample-row", "sample-id")).toBe(item().sample_id);
    expect(attr(markup, "sample-row", "role")).toBe("kick");
    expect(attr(markup, "sample-row", "file-status")).toBe("present");
    expect(attr(markup, "sample-row", "analysis-state")).toBe("current");
    expect(attr(markup, "sample-row", "duration-ms")).toBe("500");
    expect(attr(markup, "sample-row", "sample-rate-hz")).toBe("48000");
    expect(attr(markup, "sample-row", "channels")).toBe("1");
  });

  it("shows a missing file and its re-import action while the analysis stays current", () => {
    const markup = renderToStaticMarkup(
      <SampleRow item={item({ file_status: "missing" })} selected={false} onSelect={NOOP} />,
    );
    expect(attr(markup, "sample-row", "file-status")).toBe("missing");
    expect(attr(markup, "sample-row", "analysis-state")).toBe("current");
    expect(has(markup, "sample-reimport")).toBe(true);
    expect(has(markup, "sample-analysis-unavailable")).toBe(false);
  });

  it("shows an unavailable analysis with its state and keeps the row listed", () => {
    const markup = renderToStaticMarkup(
      <SampleRow
        item={item({ analysis: { analysis_version: null, state: "stale" } })}
        selected={false}
        onSelect={NOOP}
      />,
    );
    expect(attr(markup, "sample-analysis-unavailable", "analysis-state")).toBe("stale");
    expect(attr(markup, "sample-row", "file-status")).toBe("present");
    expect(has(markup, "sample-row")).toBe(true);
  });

  it("is a native button so Enter and Space activate it", () => {
    const markup = renderToStaticMarkup(
      <SampleRow item={item()} selected={false} onSelect={NOOP} />,
    );
    expect(tagOf(markup, "sample-row")).toBe("button");
    expect(markup).toContain('aria-label="kick-55.wav, kick, file present, analysis current"');
  });
});

describe("the pager", () => {
  it("disables both ends on a single page", () => {
    const markup = renderToStaticMarkup(
      <PageControls pageIndex={0} page={page()} onNext={NOOP} onPrevious={NOOP} />,
    );
    expect(attr(markup, "library-pager", "page-index")).toBe("0");
    expect(attr(markup, "library-pager", "has-more")).toBe("false");
    expect(attr(markup, "library-pager", "next-cursor")).toBe("absent");
    expect(markup).toMatch(/data-testid="library-next"[^>]*disabled/);
    expect(markup).toMatch(/data-testid="library-prev"[^>]*disabled/);
  });

  it("enables both in the middle", () => {
    const markup = renderToStaticMarkup(
      <PageControls
        pageIndex={1}
        page={page({ has_more: true, next_cursor: "cursor-a" })}
        onNext={NOOP}
        onPrevious={NOOP}
      />,
    );
    expect(attr(markup, "library-pager", "next-cursor")).toBe("present");
    expect(markup).not.toMatch(/data-testid="library-next"[^>]*disabled/);
    expect(markup).not.toMatch(/data-testid="library-prev"[^>]*disabled/);
  });

  it("treats a stale cursor beside has_more false as the last page", () => {
    const markup = renderToStaticMarkup(
      <PageControls
        pageIndex={2}
        page={page({ has_more: false, next_cursor: "cursor-stale" })}
        onNext={NOOP}
        onPrevious={NOOP}
      />,
    );
    expect(attr(markup, "library-pager", "next-cursor")).toBe("present");
    expect(markup).toMatch(/data-testid="library-next"[^>]*disabled/);
  });
});

describe("the import panel", () => {
  it("offers the role select and the picker, both native controls", () => {
    const markup = panel();
    expect(tagOf(markup, "import-role")).toBe("select");
    expect(tagOf(markup, "import-pick")).toBe("button");
    expect(has(markup, "import-controls")).toBe(true);
    expect(markup).not.toMatch(/data-testid="import-pick"[^>]*disabled/);
  });

  it("disables both while a run is live", () => {
    const markup = panel({ run: run() });
    expect(markup).toMatch(/data-testid="import-pick"[^>]*disabled/);
    expect(markup).toMatch(/data-testid="import-role"[^>]*disabled/);
  });

  it("shows a scanning run with an indeterminate bar and no percentage", () => {
    const markup = panel({ run: run({ phase: "scanning" }) });
    expect(attr(markup, "import-progress", "phase")).toBe("scanning");
    expect(attr(markup, "import-progress", "state")).toBe("running");
    expect(attr(markup, "import-progress", "run-id")).toBe(run().run_id);
    expect(attr(markup, "import-progress-bar", "mode")).toBe("indeterminate");
    expect(attr(markup, "import-progress-bar", "percent")).toBeNull();
    expect(markup).toContain('role="status"');
    expect(markup).toContain('aria-live="polite"');
  });

  it("shows the four counts, a determinate bar and the current file while analyzing", () => {
    const markup = panel({
      run: run({ current: { file_name: "kick-55.wav", attempts: 2 } }),
    });
    expect(attr(markup, "import-count-pending", "count")).toBe("3");
    expect(attr(markup, "import-count-running", "count")).toBe("1");
    expect(attr(markup, "import-count-complete", "count")).toBe("6");
    expect(attr(markup, "import-count-failed", "count")).toBe("0");
    expect(attr(markup, "import-progress-bar", "mode")).toBe("determinate");
    expect(attr(markup, "import-progress-bar", "percent")).toBe("60");
    expect(has(markup, "import-current")).toBe(true);
    expect(markup).toContain("kick-55.wav");
    expect(markup).toContain("Queued");
  });

  it("shows a partial failure without calling the run failed", () => {
    const markup = panel({
      run: run({
        state: "complete",
        phase: "complete",
        counts: { ...COUNTS, failed: 2, analyzed: 8 },
        failures: [
          { sample_id: "sha256:aaaa", file_name: "bad.wav", stage: "extract", code: "unsupported_channels", attempts: 1 },
        ],
        scan: {
          counts: { discovery_errors: 0 },
          files: [
            { sample_id: "sha256:bbbb", file_name: "broken.wav", code: null, analysis: null, error_code: "unreadable", stage: "read" },
          ],
        },
      }),
    });
    expect(attr(markup, "import-partial", "failed")).toBe("2");
    expect(attr(markup, "import-partial", "total")).toBe("10");
    expect(markup).toContain("2 of 10 files failed");
    expect(attr(markup, "import-progress", "state")).toBe("complete");
    expect(attr(markup, "import-terminal", "state")).toBe("complete");
    const rows = markup.match(/data-testid="import-failure"/g) ?? [];
    expect(rows).toHaveLength(2);
    expect(markup).toContain('data-origin="analysis"');
    expect(markup).toContain('data-origin="scan"');
    expect(markup).toContain('data-code="unsupported_channels"');
    expect(markup).toContain('data-code="unreadable"');
    expect(markup).toContain('data-stage="read"');
    expect(markup).toContain('data-file-name="bad.wav"');
    expect(markup).not.toContain("\\\\");
  });

  it("never shows a queued item as a failure", () => {
    // The route returns one record per non-complete item, so a file that is
    // merely queued arrives with a null stage and code and must render no row.
    const markup = panel({
      run: run({
        counts: { ...COUNTS, pending: 2, failed: 1, analyzed: 1 },
        failures: [
          { sample_id: "sha256:queued", file_name: "queued.wav", stage: null, code: null, attempts: 0 },
          { sample_id: "sha256:broken", file_name: "broken.wav", stage: "extract", code: "extractor_failure", attempts: 1 },
        ],
      }),
    });
    const rows = markup.match(/data-testid="import-failure"/g) ?? [];
    expect(rows).toHaveLength(1);
    expect(markup).toContain('data-file-name="broken.wav"');
    expect(markup).not.toContain("queued.wav");
    expect(markup).not.toContain("unknown stage");
    expect(markup).not.toContain("unknown code");
  });

  it("shows each terminal state with its own sentence", () => {
    for (const state of ["cancelled", "interrupted", "failed"] as const) {
      const markup = panel({ run: run({ state, phase: state }) });
      expect(attr(markup, "import-terminal", "state")).toBe(state);
      expect(has(markup, "import-progress")).toBe(true);
    }
  });

  it("notes a retry that found nothing left to do", () => {
    const markup = panel({ run: run({ state: "complete", phase: "complete" }), retryNote: true });
    expect(has(markup, "import-retry-none")).toBe(true);
  });

  it("is honest when this session did not scan, and reports discovery errors when it did", () => {
    const scanning = panel({ run: run() });
    expect(has(scanning, "import-scan-missing")).toBe(true);
    expect(has(scanning, "import-discovery-errors")).toBe(false);

    const scanned = panel({
      run: run({ scan: { counts: { discovery_errors: 3 }, files: [] } }),
    });
    expect(has(scanned, "import-scan-missing")).toBe(false);
    expect(attr(scanned, "import-discovery-errors", "count")).toBe("3");
  });

  it("shows a cancellation that is still pending", () => {
    const markup = panel({ run: run({ cancel_requested: true }) });
    expect(has(markup, "import-cancel-pending")).toBe(true);
    expect(markup).toMatch(/data-testid="import-cancel"[^>]*disabled/);
  });

  it("reports a picker refusal with its reason", () => {
    const markup = panel({ pickerReason: "permission_denied" });
    expect(attr(markup, "import-picker-error", "reason")).toBe("permission_denied");
    expect(markup).toContain("dialog:allow-open");
  });

  it("reports a refused start as an api error with its code", () => {
    const markup = panel({
      startError: { kind: "api", code: "invalid_root", status: 400, details: {} },
    });
    expect(attr(markup, "library-error", "kind")).toBe("api");
    expect(attr(markup, "library-error", "code")).toBe("invalid_root");
  });
});

describe("keyboard and semantics", () => {
  it("makes every named control a native element", () => {
    const rows = renderToStaticMarkup(
      <LibraryResults
        page={page()}
        args={ARGS}
        serviceReady
        isFetching={false}
        isFirstLoad={false}
        error={null}
        staleSelection={false}
        selectedSampleId={null}
        onRetry={NOOP}
        onClearFilters={NOOP}
        onSelect={NOOP}
        onImport={NOOP}
      />,
    );
    expect(tagOf(rows, "sample-row")).toBe("button");

    const pager = renderToStaticMarkup(
      <PageControls pageIndex={1} page={page({ has_more: true, next_cursor: "c" })} onNext={NOOP} onPrevious={NOOP} />,
    );
    expect(tagOf(pager, "library-next")).toBe("button");
    expect(tagOf(pager, "library-prev")).toBe("button");

    const controls = panel({ run: run({ state: "complete", phase: "complete", counts: { ...COUNTS, failed: 1 } }) });
    expect(tagOf(controls, "import-cancel")).toBe("button");
    expect(tagOf(controls, "import-retry")).toBe("button");
    // A finished run cannot be cancelled, and its failures can be retried.
    expect(controls).toMatch(/data-testid="import-cancel"[^>]*disabled/);
    expect(controls).not.toMatch(/data-testid="import-retry"[^>]*disabled/);

    const filters = renderToStaticMarkup(
      <LibraryFilters
        text=""
        roles={[]}
        limit={50}
        onText={NOOP}
        onRoles={NOOP}
        onLimit={NOOP}
      />,
    );
    expect(tagOf(filters, "library-search")).toBe("input");
    expect(tagOf(filters, "library-page-size")).toBe("select");
    const boxes = filters.match(/<input[^>]*type="checkbox"[^>]*>/g) ?? [];
    expect(boxes).toHaveLength(3);
    expect(filters).toContain('data-role="kick"');
    expect(filters).toContain('data-role="bass"');
    expect(filters).toContain('data-role="sub-bass"');
    // Attribute names are case-insensitive in HTML; static markup keeps the JSX spelling.
    expect(filters.toLowerCase()).toContain('maxlength="200"');
  });
});
