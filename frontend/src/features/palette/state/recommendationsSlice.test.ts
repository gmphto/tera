/**
 * The slice's own rules, with no network (issue #33).
 *
 * Every case here is a plain reducer call: the request identity and the serial
 * that make a late answer refusable, the stale rule, cancel, and the card action
 * lifecycle. The request log — that a select writes the palette first, that a
 * retry reuses one `client_event_id`, that a second click sends nothing — is
 * `recommendationsApi.test.ts`, which drives the same slice through the api.
 */

import type { UnknownAction } from "@reduxjs/toolkit";
import { describe, expect, it } from "vitest";

import { candidate, candidateId, response } from "../api/fixtures";
import {
  actionFailed,
  actionPaletteWritten,
  actionStarted,
  actionSucceeded,
  recommendationRequest,
  recommendationsCancelled,
  recommendationsIdle,
  recommendationsReceived,
  recommendationsReducer,
  recommendationsRefused,
  recommendationsStarted,
  recommendationsStaled,
  recommendationsUnavailable,
  selectCardState as sliceCardState,
  selectCardStates as sliceCardStates,
  selectRecommendationBatch as sliceBatch,
  selectRecommendationStale as sliceStale,
  selectRecommendationStatus as sliceStatus,
  type RecommendationsState,
} from "./recommendationsSlice";

/** The slice's selectors read a root state; these cases hold the slice's own. */
const root = (state: RecommendationsState) => ({ recommendations: state });
const selectRecommendationStatus = (state: RecommendationsState) => sliceStatus(root(state));
const selectRecommendationBatch = (state: RecommendationsState) => sliceBatch(root(state));
const selectRecommendationStale = (state: RecommendationsState) => sliceStale(root(state));
const selectCardState = (state: RecommendationsState, candidateKey: string) =>
  sliceCardState(root(state), candidateKey);
const selectCardStates = (state: RecommendationsState) => sliceCardStates(root(state));

const PALETTE = "palette-001";
const KICK = candidateId(99);

const REQUEST = recommendationRequest({ paletteId: PALETTE, revision: 4, kickId: KICK });
const NEXT = recommendationRequest({ paletteId: PALETTE, revision: 5, kickId: KICK });

function apply(...actions: UnknownAction[]) {
  let state = recommendationsReducer(undefined, { type: "recommendations/init" });
  for (const action of actions) {
    state = recommendationsReducer(state, action);
  }
  return state;
}

/** One batch/run pair as the api would deliver it, at a chosen revision. */
function payload(serial: number, revision = 4) {
  const answered = response({
    batch: { palette: { ...response().recommendation.palette, revision } },
  });
  return { serial, batch: answered.recommendation, run: answered.run };
}

describe("the request identity", () => {
  it("starts loading with the previous run's cards already gone", () => {
    const ready = apply(recommendationsStarted(REQUEST), recommendationsReceived(payload(1)));
    expect(selectRecommendationBatch(ready)).not.toBeNull();
    const restarted = recommendationsReducer(ready, recommendationsStarted(NEXT));
    expect(selectRecommendationStatus(restarted)).toBe("loading");
    expect(selectRecommendationBatch(restarted)).toBeNull();
    expect(restarted.run).toBeNull();
  });

  it("drops a late answer for a superseded request instead of applying it", () => {
    const ready = apply(recommendationsStarted(REQUEST), recommendationsReceived(payload(1)));
    const restarted = recommendationsReducer(ready, recommendationsStarted(NEXT));
    const late = recommendationsReducer(restarted, recommendationsReceived(payload(1)));
    expect(selectRecommendationStatus(late)).toBe("loading");
    expect(selectRecommendationBatch(late)).toBeNull();
    expect(late.droppedResponses).toBe(1);
  });

  it("drops a refusal and an unavailability that belong to a superseded request", () => {
    const started = apply(recommendationsStarted(REQUEST));
    const restarted = recommendationsReducer(started, recommendationsStarted(NEXT));
    const refused = recommendationsReducer(
      restarted,
      recommendationsRefused({
        serial: 1,
        error: { source: "service", code: "internal_error", retryable: true },
      }),
    );
    expect(selectRecommendationStatus(refused)).toBe("loading");
    const unavailable = recommendationsReducer(
      refused,
      recommendationsUnavailable({ serial: 1 }),
    );
    expect(selectRecommendationStatus(unavailable)).toBe("loading");
    expect(unavailable.droppedResponses).toBe(2);
  });

  it("refuses a response that answers a different revision, with no cards", () => {
    const state = apply(recommendationsStarted(REQUEST), recommendationsReceived(payload(1, 9)));
    expect(selectRecommendationStatus(state)).toBe("stale");
    expect(selectRecommendationStale(state)).toEqual({
      requestedRevision: 4,
      currentRevision: 9,
    });
    expect(selectRecommendationBatch(state)).toBeNull();
  });

  it("applies a response that answers the active request in full", () => {
    const state = apply(recommendationsStarted(REQUEST), recommendationsReceived(payload(1)));
    expect(selectRecommendationStatus(state)).toBe("ready");
    expect(selectRecommendationBatch(state)?.results.map((result) => result.rank)).toEqual([
      1, 2, 3, 4, 5,
    ]);
    expect(state.droppedResponses).toBe(0);
  });

  it("cancels without an error, and drops whatever settles afterwards", () => {
    const started = apply(recommendationsStarted(REQUEST));
    const cancelled = recommendationsReducer(started, recommendationsCancelled());
    expect(selectRecommendationStatus(cancelled)).toBe("cancelled");
    expect(cancelled.error).toBeNull();
    const settled = recommendationsReducer(cancelled, recommendationsUnavailable({ serial: 1 }));
    expect(selectRecommendationStatus(settled)).toBe("cancelled");
    expect(settled.droppedResponses).toBe(1);
  });

  it("returns to idle with no request at all", () => {
    const state = apply(recommendationsStarted(REQUEST), recommendationsIdle());
    expect(selectRecommendationStatus(state)).toBe("idle");
    expect(state.request).toBeNull();
    expect(selectRecommendationBatch(state)).toBeNull();
  });

  it("records a stale state from a conflict without cards", () => {
    const state = apply(
      recommendationsStarted(REQUEST),
      recommendationsReceived(payload(1)),
      recommendationsStaled({ requestedRevision: 4, currentRevision: 6, autoRetried: false }),
    );
    expect(selectRecommendationStale(state)).toEqual({
      requestedRevision: 4,
      currentRevision: 6,
    });
    expect(selectRecommendationBatch(state)).toBeNull();
    expect(state.autoRetried).toBe(false);
  });
});

describe("a card's action", () => {
  const base = apply(recommendationsStarted(REQUEST), recommendationsReceived(payload(1)));

  it("walks idle to pending to saved", () => {
    expect(selectCardState(base, candidateId(1))).toBe("idle");
    const pending = recommendationsReducer(
      base,
      actionStarted({ candidateId: candidateId(1), kind: "select", clientEventId: "client-001" }),
    );
    expect(selectCardState(pending, candidateId(1))).toBe("pending");
    const saved = recommendationsReducer(
      pending,
      actionSucceeded({ candidateId: candidateId(1), state: "saved" }),
    );
    expect(selectCardState(saved, candidateId(1))).toBe("saved");
    expect(selectCardStates(saved)).toEqual({ [candidateId(1)]: "saved" });
  });

  it("is unrecorded when the outcome fails after the palette write, and error before it", () => {
    const pending = recommendationsReducer(
      base,
      actionStarted({ candidateId: candidateId(1), kind: "select", clientEventId: "client-001" }),
    );
    const early = recommendationsReducer(
      pending,
      actionFailed({ candidateId: candidateId(1), errorCode: "database_locked", retryable: true }),
    );
    expect(selectCardState(early, candidateId(1))).toBe("error");
    expect(early.actions[0]).toMatchObject({ errorCode: "database_locked", retryable: true });
    const written = recommendationsReducer(
      pending,
      actionPaletteWritten({ candidateId: candidateId(1) }),
    );
    const late = recommendationsReducer(
      written,
      actionFailed({ candidateId: candidateId(1), errorCode: "database_locked", retryable: true }),
    );
    expect(selectCardState(late, candidateId(1))).toBe("unrecorded");
    expect(late.actions[0].paletteWritten).toBe(true);
  });

  it("ignores a click while the action is pending, keeping the first event id", () => {
    const pending = recommendationsReducer(
      base,
      actionStarted({ candidateId: candidateId(1), kind: "select", clientEventId: "client-001" }),
    );
    const second = recommendationsReducer(
      pending,
      actionStarted({ candidateId: candidateId(1), kind: "select", clientEventId: "client-002" }),
    );
    expect(second.actions).toHaveLength(1);
    expect(second.actions[0]).toMatchObject({
      state: "pending",
      clientEventId: "client-001",
    });
  });

  it("rewrites a settled action when a retry starts it again", () => {
    const settled = apply(
      recommendationsStarted(REQUEST),
      recommendationsReceived(payload(1)),
      actionStarted({ candidateId: candidateId(1), kind: "select", clientEventId: "client-001" }),
      actionPaletteWritten({ candidateId: candidateId(1) }),
      actionFailed({ candidateId: candidateId(1), errorCode: "database_locked", retryable: true }),
      actionStarted({ candidateId: candidateId(1), kind: "select", clientEventId: "client-001" }),
    );
    expect(settled.actions[0]).toMatchObject({
      state: "pending",
      clientEventId: "client-001",
      errorCode: null,
    });
  });

  it("reaches rejected for a rejection, which writes no palette item", () => {
    const state = apply(
      recommendationsStarted(REQUEST),
      recommendationsReceived(payload(1)),
      actionStarted({ candidateId: candidateId(2), kind: "reject", clientEventId: "client-003" }),
      actionSucceeded({ candidateId: candidateId(2), state: "rejected" }),
    );
    expect(selectCardState(state, candidateId(2))).toBe("rejected");
    expect(state.actions[0].paletteWritten).toBe(false);
  });

  it("survives a refresh for a candidate that is still present, and is forgotten otherwise", () => {
    const pending = recommendationsReducer(
      base,
      actionStarted({ candidateId: candidateId(1), kind: "select", clientEventId: "client-001" }),
    );
    const refreshed = apply(
      recommendationsStarted(REQUEST),
      recommendationsReceived(payload(1)),
      actionStarted({ candidateId: candidateId(1), kind: "select", clientEventId: "client-001" }),
    );
    expect(refreshed.actions[0].state).toBe("pending");
    const restarted = recommendationsReducer(pending, recommendationsStarted(REQUEST));
    const dropped = recommendationsReducer(
      restarted,
      recommendationsReceived({
        serial: restarted.serial,
        batch: response({ batch: { results: [candidate(1, { candidate_id: candidateId(7) })] } })
          .recommendation,
        run: response().run,
      }),
    );
    expect(dropped.actions).toEqual([]);
  });
});
