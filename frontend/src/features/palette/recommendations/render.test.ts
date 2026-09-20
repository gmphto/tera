/**
 * Every state's markup, rendered to a string (issue #33).
 *
 * `renderToStaticMarkup` needs no DOM, so these run under #30's node
 * environment with the approved pins, and the files stay free of JSX. What is
 * asserted is exactly what the acceptance criteria name: the `data-testid` and
 * attributes of each state, that no non-list state renders a card, that the list
 * keeps the API's order, and that compatibility and confidence are two separate
 * labeled elements.
 */

import { configureStore } from "@reduxjs/toolkit";
import { createElement, type ReactElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { Provider } from "react-redux";
import { describe, expect, it } from "vitest";

import serviceReducer from "../../service/serviceSlice";
import type { ServiceSnapshot } from "../../service/status";
import { paletteLoaded, paletteReducer } from "../state/paletteSlice";
import {
  recommendationsReducer,
  recommendationsRefused,
  recommendationsReceived,
  recommendationsStaled,
  recommendationsStarted,
  recommendationRequest,
  type RecommendationsState,
} from "../state/recommendationsSlice";
import {
  ALL_EXCLUDED,
  HYBRID_FIVE,
  NO_CANDIDATES,
  UNSCORED_TWO,
  candidate,
  candidateId,
  palette,
  response,
} from "../api/fixtures";
import { alternativeItems, cardModel, type CardModel } from "./cards";
import { RecommendationCard } from "./RecommendationCard";
import { RecommendationPanel } from "./RecommendationPanel";
import {
  AlternativesBlock,
  CancelledState,
  DegradedBadge,
  EmptyState,
  ErrorState,
  IdleState,
  LoadingState,
  StaleState,
  UnavailableState,
  UnscoredNote,
  ExclusionTally,
} from "./RecommendationStates";

const ORIGIN = "http://127.0.0.1:7391";
const SNAPSHOT: ServiceSnapshot = {
  mode: "owned",
  phase: "running",
  reason: null,
  origin: ORIGIN,
  port: 7391,
  pid: 1234,
  exit_code: null,
  python_path: null,
  database_path: null,
  data_dir: null,
  port_file: null,
  health_seen: true,
  started_at: null,
  diagnostic: null,
};

const REQUEST = recommendationRequest({
  paletteId: "palette-001",
  revision: 4,
  kickId: candidateId(99),
});

function render(element: ReactElement): string {
  return renderToStaticMarkup(element);
}

/** The first opening tag carrying a `data-testid`, as its attribute map. */
function attrsOf(html: string, testid: string): Record<string, string | true> {
  const match = new RegExp(`<[a-z]+[^>]*data-testid="${testid}"[^>]*>`).exec(html);
  if (match === null) {
    throw new Error(`no element carries data-testid=${testid}`);
  }
  const attrs: Record<string, string | true> = {};
  for (const found of match[0].replace(/^<[a-z]+/, "").matchAll(/([a-z-]+)(?:="([^"]*)")?/g)) {
    attrs[found[1]] = found[2] === undefined ? true : found[2];
  }
  return attrs;
}

function count(html: string, testid: string): number {
  return html.split(`data-testid="${testid}"`).length - 1;
}

/** The raw opening tag of the first element carrying a `data-testid`. */
function tagOf(html: string, testid: string): string {
  const match = new RegExp(`<[a-z]+[^>]*data-testid="${testid}"[^>]*>`).exec(html);
  if (match === null) {
    throw new Error(`no element carries data-testid=${testid}`);
  }
  return match[0];
}

const initial = recommendationsReducer(undefined, { type: "recommendations/init" });

/** The slice state one panel render is defined by. */
function seeded(overrides: Partial<RecommendationsState> = {}): RecommendationsState {
  return { ...initial, ...overrides };
}

function ready(batch = response().recommendation, run = response().run): RecommendationsState {
  const started = recommendationsReducer(initial, recommendationsStarted(REQUEST));
  return recommendationsReducer(
    started,
    recommendationsReceived({ serial: started.serial, batch, run }),
  );
}

function panel(state: RecommendationsState, loadedPalette = true): string {
  const store = configureStore({
    reducer: {
      service: serviceReducer,
      palette: paletteReducer,
      recommendations: recommendationsReducer,
    },
    preloadedState: {
      service: { snapshot: SNAPSHOT, origin: ORIGIN },
      palette: paletteReducer(
        undefined,
        paletteLoaded(loadedPalette ? palette() : null),
      ),
      recommendations: state,
    },
  });
  return render(
    createElement(Provider, { store, children: createElement(RecommendationPanel) }),
  );
}

describe("a card's markup", () => {
  const results = response().recommendation.results;
  const model = (index: number, selectedBassId: string | null = null): CardModel =>
    cardModel(results[index], selectedBassId);

  it("carries the fixed field set and no similarity at all", () => {
    const html = render(
      createElement(RecommendationCard, {
        card: model(0),
        state: "idle",
        errorCode: null,
        retryable: false,
        onSelect: () => {},
        onReject: () => {},
        onRetryAction: () => {},
      }),
    );
    const attrs = attrsOf(html, "recommendation-card");
    expect(Object.keys(attrs).sort()).toEqual([
      "class",
      "data-candidate-id",
      "data-compatibility",
      "data-confidence",
      "data-jev",
      "data-rank",
      "data-selected",
      "data-state",
      "data-testid",
      "data-uncertain",
    ]);
    expect(attrs["data-rank"]).toBe("1");
    expect(attrs["data-candidate-id"]).toBe(results[0].candidate_id);
    expect(attrs["data-compatibility"]).toBe(String(results[0].compatibility));
    expect(attrs["data-confidence"]).toBe(String(results[0].confidence));
    expect(attrs["data-jev"]).toBe("none");
    expect(attrs["data-state"]).toBe("idle");
    expect(html).not.toContain("similarity");
    expect(html).not.toContain("0.5");
  });

  it("renders a known similarity and its null twin identically", () => {
    const known = cardModel(candidate(1, { similarity: 0.91 }), null);
    const unknown = cardModel(
      candidate(1, { similarity: null, similarity_unavailable_reason: "insufficient_common_dimensions" }),
      null,
    );
    const props = {
      state: "idle" as const,
      errorCode: null,
      retryable: false,
      onSelect: () => {},
      onReject: () => {},
      onRetryAction: () => {},
    };
    expect(render(createElement(RecommendationCard, { card: known, ...props }))).toBe(
      render(createElement(RecommendationCard, { card: unknown, ...props })),
    );
  });

  it("keeps compatibility and confidence in two separate labeled elements", () => {
    // Two different values, so the elements cannot be satisfied by one number.
    const distinct = candidate(1, { compatibility: 0.9, confidence: 0.4 });
    const html = render(
      createElement(RecommendationCard, {
        card: cardModel(distinct, null),
        state: "idle",
        errorCode: null,
        retryable: false,
        onSelect: () => {},
        onReject: () => {},
        onRetryAction: () => {},
      }),
    );
    const compatibility = attrsOf(html, "card-compatibility");
    const confidence = attrsOf(html, "card-confidence");
    expect(compatibility["data-value"]).toBe("0.9");
    expect(confidence["data-value"]).toBe("0.4");
    const compatibilityElement = html.slice(
      html.indexOf('data-testid="card-compatibility"'),
      html.indexOf('data-testid="card-confidence"'),
    );
    expect(compatibilityElement).toContain("Compatibility");
    expect(compatibilityElement).toContain("90%");
    expect(compatibilityElement).not.toContain("40%");
    expect(compatibilityElement).not.toContain("Confidence");
    const confidenceElement = html.slice(html.indexOf('data-testid="card-confidence"'));
    expect(confidenceElement).toContain("Confidence");
    expect(confidenceElement).toContain("40%");
    expect(confidenceElement).not.toContain("90%");
  });

  it("says a card without judgments has none, and names no Jev label", () => {
    const html = render(
      createElement(RecommendationCard, {
        card: model(0),
        state: "idle",
        errorCode: null,
        retryable: false,
        onSelect: () => {},
        onReject: () => {},
        onRetryAction: () => {},
      }),
    );
    expect(attrsOf(html, "card-jev")["data-jev"]).toBe("none");
    expect(html).toContain("No Jev judgment — measured rules only");
    expect(html).not.toContain("probability");
    expect(html).not.toContain("model version");
  });

  it("marks a judgment-carrying card present", () => {
    const withJudgment = HYBRID_FIVE.recommendation.results[0];
    const html = render(
      createElement(RecommendationCard, {
        card: cardModel(withJudgment, null),
        state: "idle",
        errorCode: null,
        retryable: false,
        onSelect: () => {},
        onReject: () => {},
        onRetryAction: () => {},
      }),
    );
    expect(attrsOf(html, "card-jev")["data-jev"]).toBe("present");
  });

  it("disables reject on the selected card and marks it selected", () => {
    const selectedId = candidateId(1);
    const html = render(
      createElement(RecommendationCard, {
        card: cardModel(candidate(1), selectedId),
        state: "idle",
        errorCode: null,
        retryable: false,
        onSelect: () => {},
        onReject: () => {},
        onRetryAction: () => {},
      }),
    );
    expect(attrsOf(html, "recommendation-card")["data-selected"]).toBe("true");
    expect(tagOf(html, "card-reject")).toContain("disabled");
    expect(tagOf(html, "card-select")).not.toContain("disabled");
  });

  it("shows the action failure, its code and a retry only when retryable", () => {
    const retryable = render(
      createElement(RecommendationCard, {
        card: cardModel(candidate(1), null),
        state: "error",
        errorCode: "database_locked",
        retryable: true,
        onSelect: () => {},
        onReject: () => {},
        onRetryAction: () => {},
      }),
    );
    expect(attrsOf(retryable, "card-action-error")).toMatchObject({
      "data-error-code": "database_locked",
      "data-retryable": "true",
    });
    expect(count(retryable, "card-action-retry")).toBe(1);
    const refused = render(
      createElement(RecommendationCard, {
        card: cardModel(candidate(1), null),
        state: "error",
        errorCode: "outcome_conflict",
        retryable: false,
        onSelect: () => {},
        onReject: () => {},
        onRetryAction: () => {},
      }),
    );
    expect(attrsOf(refused, "card-action-error")["data-retryable"]).toBe("false");
    expect(refused).toContain("outcome_conflict");
    expect(count(refused, "card-action-retry")).toBe(0);
  });

  it("labels an unrecorded selection as selected but not recorded", () => {
    const html = render(
      createElement(RecommendationCard, {
        card: cardModel(candidate(1), null),
        state: "unrecorded",
        errorCode: "database_locked",
        retryable: true,
        onSelect: () => {},
        onReject: () => {},
        onRetryAction: () => {},
      }),
    );
    expect(attrsOf(html, "recommendation-card")["data-state"]).toBe("unrecorded");
    expect(html).toContain("the event was not recorded");
    expect(html).not.toContain(">Selected<");
  });

  it("marks a saved card selected even before the next batch carries it", () => {
    const html = render(
      createElement(RecommendationCard, {
        card: cardModel(candidate(1), null),
        state: "saved",
        errorCode: null,
        retryable: false,
        onSelect: () => {},
        onReject: () => {},
        onRetryAction: () => {},
      }),
    );
    expect(attrsOf(html, "recommendation-card")["data-selected"]).toBe("true");
    expect(tagOf(html, "card-reject")).toContain("disabled");
  });
});

describe("each state's element", () => {
  const noop = () => {};
  const cases: [string, ReactElement, Record<string, string>][] = [
    ["idle", createElement(IdleState), { "data-reason": "no_kick" }],
    ["loading", createElement(LoadingState, { slow: false, onCancel: noop }), { "data-slow": "false" }],
    [
      "loading, slow",
      createElement(LoadingState, { slow: true, onCancel: noop }),
      { "data-slow": "true" },
    ],
    ["cancelled", createElement(CancelledState, { onRetry: noop }), {}],
    [
      "stale",
      createElement(StaleState, { requestedRevision: 4, currentRevision: 6, onRetry: noop }),
      { "data-run-revision": "4", "data-current-revision": "6" },
    ],
    [
      "unavailable",
      createElement(UnavailableState, { phase: "unavailable", onRetry: noop }),
      { "data-service-phase": "unavailable" },
    ],
    [
      "error",
      createElement(ErrorState, {
        source: "service",
        code: "palette_incomplete",
        retryable: false,
        onRetry: noop,
      }),
      {
        "data-error-source": "service",
        "data-error-code": "palette_incomplete",
        "data-retryable": "false",
      },
    ],
    [
      "empty, no candidates",
      createElement(EmptyState, {
        kind: "no_candidates",
        limitReason: "no_candidates",
        exclusions: [],
        unscored: 0,
      }),
      { "data-empty-kind": "no_candidates", "data-limit-reason": "no_candidates" },
    ],
    [
      "empty, all excluded",
      createElement(EmptyState, {
        kind: "all_excluded",
        limitReason: "no_candidates",
        exclusions: [{ code: "file_missing", count: 2, label: "File missing" }],
        unscored: 0,
      }),
      { "data-empty-kind": "all_excluded" },
    ],
    [
      "empty, all unscored",
      createElement(EmptyState, {
        kind: "all_unscored",
        limitReason: "no_candidates",
        exclusions: [],
        unscored: 2,
      }),
      { "data-empty-kind": "all_unscored" },
    ],
  ];

  for (const [name, element, expected] of cases) {
    it(`renders ${name} with no card and no score`, () => {
      const html = render(element);
      const testid = `recommendations-${name.split(",")[0].trim()}`;
      const attrs = attrsOf(html, testid);
      for (const [key, value] of Object.entries(expected)) {
        expect(attrs[key]).toBe(value);
      }
      expect(count(html, "recommendation-card")).toBe(0);
      expect(count(html, "card-compatibility")).toBe(0);
    });
  }

  it("keeps the all-excluded tally's codes, counts and order", () => {
    const html = render(
      createElement(ExclusionTally, {
        rows: [
          { code: "file_missing", count: 3, label: "File missing" },
          { code: "unmapped_code", count: 1, label: "unmapped_code" },
        ],
      }),
    );
    expect(count(html, "exclusion-row")).toBe(2);
    const rows = html.split('data-testid="exclusion-row"').slice(1);
    expect(rows[0]).toContain('data-exclusion-code="file_missing"');
    expect(rows[0]).toContain('data-exclusion-count="3"');
    expect(rows[0]).toContain("File missing");
    // An unknown code is shown verbatim rather than hidden or renamed.
    expect(rows[1]).toContain('data-exclusion-code="unmapped_code"');
    expect(rows[1]).toContain("unmapped_code");
  });

  it("counts unscorable candidates without inventing a card for them", () => {
    const html = render(createElement(UnscoredNote, { count: 2 }));
    expect(attrsOf(html, "unscored-note")["data-unscored-count"]).toBe("2");
    expect(html).toContain("could not be scored");
    expect(count(html, "recommendation-card")).toBe(0);
  });

  it("names the degraded condition and never claims a Jev judgment", () => {
    const html = render(createElement(DegradedBadge, { mode: "dsp-only", jevStatus: "jev_absent" }));
    const attrs = attrsOf(html, "recommendations-degraded");
    expect(attrs["data-mode"]).toBe("dsp-only");
    expect(attrs["data-jev-status"]).toBe("jev_absent");
    expect(html).toContain("no Jev judgment was available");
    expect(html).not.toContain("agreed");
  });

  it("lists the API's alternatives with their own values and a focus control", () => {
    const items = alternativeItems(HYBRID_FIVE.recommendation);
    const html = render(createElement(AlternativesBlock, { items, onFocus: noop }));
    expect(count(html, "alternative-item")).toBe(items.length);
    const first = attrsOf(html, "alternative-item");
    expect(first["data-candidate-id"]).toBe(candidateId(2));
    expect(first["data-rank"]).toBe("2");
    expect(first["data-compatibility"]).toBe(String(HYBRID_FIVE.recommendation.results[1].compatibility));
    expect(first["data-confidence"]).toBe(String(HYBRID_FIVE.recommendation.results[1].confidence));
    expect(count(html, "alternative-focus")).toBe(items.length);
  });

  it("renders no alternatives block when the API returned none", () => {
    expect(render(createElement(AlternativesBlock, { items: [], onFocus: noop }))).toBe("");
  });
});

describe("the panel", () => {
  it("renders the idle state with no kick, and no card", () => {
    const html = panel(seeded({ status: "idle" }), false);
    expect(attrsOf(html, "recommendations-idle")["data-reason"]).toBe("no_kick");
    expect(count(html, "recommendation-card")).toBe(0);
  });

  it("renders the list in API order, as an ordered list of cards", () => {
    const html = panel(ready());
    expect(html).toContain("<ol");
    expect(count(html, "recommendation-card")).toBe(5);
    // DOM order is the API's order, rank by rank.
    let cursor = -1;
    for (const result of response().recommendation.results) {
      const at = html.indexOf(`data-candidate-id="${result.candidate_id}"`);
      expect(at).toBeGreaterThan(cursor);
      cursor = at;
    }
    for (const [index, result] of response().recommendation.results.entries()) {
      expect(html).toContain(`data-rank="${index + 1}"`);
      expect(result.rank).toBe(index + 1);
    }
    expect(count(html, "recommendations-empty")).toBe(0);
    expect(count(html, "recommendations-error")).toBe(0);
  });

  it("renders one state only, and the degraded badge beside it", () => {
    const html = panel(ready());
    expect(count(html, "recommendations-panel")).toBe(1);
    expect(count(html, "recommendations-degraded")).toBe(1);
    expect(attrsOf(html, "recommendations-degraded")["data-mode"]).toBe("dsp-only");
    for (const other of [
      "recommendations-idle",
      "recommendations-loading",
      "recommendations-cancelled",
      "recommendations-stale",
      "recommendations-empty",
      "recommendations-error",
      "recommendations-unavailable",
    ]) {
      expect(count(html, other)).toBe(0);
    }
  });

  it("shows no degraded badge for a hybrid run with judgments", () => {
    const html = panel(ready(HYBRID_FIVE.recommendation, HYBRID_FIVE.run));
    expect(count(html, "recommendations-degraded")).toBe(0);
    expect(count(html, "recommendation-card")).toBe(5);
  });

  it("renders each empty kind as a state, not as an error", () => {
    const noCandidates = panel(ready(NO_CANDIDATES.recommendation, NO_CANDIDATES.run));
    expect(attrsOf(noCandidates, "recommendations-empty")["data-empty-kind"]).toBe("no_candidates");
    expect(attrsOf(noCandidates, "recommendations-empty")["data-limit-reason"]).toBe("no_candidates");
    expect(count(noCandidates, "recommendations-error")).toBe(0);
    const excluded = panel(ready(ALL_EXCLUDED.recommendation, ALL_EXCLUDED.run));
    expect(attrsOf(excluded, "recommendations-empty")["data-empty-kind"]).toBe("all_excluded");
    expect(count(excluded, "exclusion-row")).toBe(3);
    expect(count(excluded, "recommendation-card")).toBe(0);
    const unscored = panel(ready(UNSCORED_TWO.recommendation, UNSCORED_TWO.run));
    expect(count(unscored, "recommendation-card")).toBe(5);
    expect(attrsOf(unscored, "unscored-note")["data-unscored-count"]).toBe("2");
  });

  it("summarises exclusions beside a non-empty list", () => {
    const excluded = { ...response().run, counts: { ...response().run.counts, excluded: 4 } };
    const html = panel(ready(response().recommendation, excluded));
    expect(attrsOf(html, "exclusions-summary")["data-excluded-count"]).toBe("4");
    expect(count(html, "recommendation-card")).toBe(5);
  });

  it("renders the alternatives block when the batch carries them", () => {
    const html = panel(ready(HYBRID_FIVE.recommendation, HYBRID_FIVE.run));
    expect(count(html, "recommendations-alternatives")).toBe(1);
    expect(count(html, "alternative-item")).toBe(2);
  });

  it("shows the loading state with a cancel control and its live region", () => {
    const html = panel(seeded({ status: "loading", request: REQUEST }));
    const attrs = attrsOf(html, "recommendations-loading");
    expect(attrs.role).toBe("status");
    expect(attrs["aria-live"]).toBe("polite");
    expect(attrs["data-slow"]).toBe("false");
    expect(count(html, "recommendations-cancel")).toBe(1);
    expect(count(html, "recommendation-card")).toBe(0);
  });

  it("shows cancelled, stale, unavailable and error states with their own attributes", () => {
    const cancelled = panel(seeded({ status: "cancelled", request: REQUEST }));
    expect(count(cancelled, "recommendations-cancelled")).toBe(1);
    expect(count(cancelled, "recommendations-retry")).toBe(1);
    expect(count(cancelled, "recommendation-card")).toBe(0);

    const stale = recommendationsReducer(
      seeded({ status: "loading", request: REQUEST }),
      recommendationsStaled({ requestedRevision: 4, currentRevision: 6, autoRetried: false }),
    );
    const staleHtml = panel(stale);
    expect(attrsOf(staleHtml, "recommendations-stale")).toMatchObject({
      "data-run-revision": "4",
      "data-current-revision": "6",
      role: "alert",
    });
    expect(count(staleHtml, "recommendation-card")).toBe(0);

    const unavailable = panel(seeded({ status: "unavailable", request: REQUEST }));
    expect(attrsOf(unavailable, "recommendations-unavailable")["data-service-phase"]).toBe("running");
    expect(count(unavailable, "recommendation-card")).toBe(0);

    const refused = recommendationsReducer(
      seeded({ status: "loading", request: REQUEST }),
      recommendationsRefused({
        serial: seeded({ status: "loading", request: REQUEST }).serial,
        error: { source: "client", code: "malformed_response", retryable: false },
      }),
    );
    const errorHtml = panel(refused);
    expect(attrsOf(errorHtml, "recommendations-error")).toMatchObject({
      "data-error-source": "client",
      "data-error-code": "malformed_response",
      "data-retryable": "false",
      role: "alert",
    });
    expect(count(errorHtml, "recommendation-card")).toBe(0);
    expect(count(errorHtml, "recommendations-retry")).toBe(0);
  });

  it("shows no card from a failed run even when a batch is still in state", () => {
    const state = { ...ready(), status: "error" as const };
    const html = panel(
      {
        ...state,
        error: { source: "service", code: "unknown_palette", retryable: false },
      },
    );
    expect(count(html, "recommendation-card")).toBe(0);
    expect(attrsOf(html, "recommendations-error")["data-error-code"]).toBe("unknown_palette");
  });
});
