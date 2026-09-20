/**
 * The two routes, the exact bodies and the order of a select (issue #33).
 *
 * Everything here drives the real RTK Query pipeline — #30's api slice, its
 * dynamic base query and both injected endpoints — against the recording
 * `fetch` double, so the assertions are about the bytes the client actually
 * sends. No server is started and no Node-only module is imported.
 */

import { configureStore } from "@reduxjs/toolkit";
import { afterEach, describe, expect, it } from "vitest";

import { createTeraApi } from "../../../app/api";
import serviceReducer, { statusReceived } from "../../service/serviceSlice";
import type { ServiceSnapshot } from "../../service/status";
import { paletteLoaded, paletteReducer } from "../state/paletteSlice";
import {
  createRecommendationActions,
  recommendationsIdle,
  recommendationsReducer,
  selectCardState,
  selectRecommendationBatch,
  selectRecommendationRun,
  selectRecommendationStale,
  selectRecommendationStatus,
} from "../state/recommendationsSlice";
import {
  MALFORMED,
  PALETTE_ID,
  envelope,
  outcome,
  palette,
  response,
  candidateId,
} from "./fixtures";
import { injectPaletteEndpoints } from "./paletteApi";
import {
  LIMIT_MAX,
  LIMIT_MIN,
  injectRecommendationEndpoints,
  recommendationBody,
  outcomeBody,
} from "./recommendationsApi";
import { installStubFetch, type RecordedCall, type StubHandler } from "./stubFetch";

const ORIGIN = "http://127.0.0.1:7391";
const REVISION = 4;
const KICK = candidateId(99);

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

const doubles: { restore: () => void }[] = [];

afterEach(() => {
  while (doubles.length > 0) {
    doubles.pop()?.restore();
  }
});

function harness(handler: StubHandler) {
  const double = installStubFetch(handler);
  doubles.push(double);
  const api = injectRecommendationEndpoints(
    injectPaletteEndpoints(createTeraApi({ timeoutMs: 300 })),
  );
  const store = configureStore({
    reducer: {
      service: serviceReducer,
      palette: paletteReducer,
      recommendations: recommendationsReducer,
      [api.reducerPath]: api.reducer,
    },
    middleware: (getDefaultMiddleware) => getDefaultMiddleware().concat(api.middleware),
  });
  store.dispatch(statusReceived(SNAPSHOT));
  store.dispatch(paletteLoaded(palette()));
  const actions = createRecommendationActions(api);
  const request = { paletteId: PALETTE_ID, revision: REVISION, kickId: KICK, limit: 10, filters: {} };
  return { api, store, actions, double, request };
}

function bodyOf(call: RecordedCall): Record<string, unknown> {
  return JSON.parse(call.body) as Record<string, unknown>;
}

describe("POST /recommendations", () => {
  it("sends exactly the four documented keys to the one loopback origin", async () => {
    const { store, actions, double, request } = harness(() => ({ body: response() }));
    await store.dispatch(actions.run(request));
    expect(double.path()).toEqual(["POST /recommendations"]);
    expect(double.calls[0].url).toBe(`${ORIGIN}/recommendations`);
    expect(bodyOf(double.calls[0])).toEqual({
      palette_id: PALETTE_ID,
      revision: REVISION,
      limit: 10,
      filters: {},
    });
    expect(Object.keys(bodyOf(double.calls[0])).sort()).toEqual([
      "filters",
      "limit",
      "palette_id",
      "revision",
    ]);
    expect(selectRecommendationStatus(store.getState())).toBe("ready");
    expect(selectRecommendationBatch(store.getState())?.run_id).toBe(response().run.run_id);
    expect(selectRecommendationRun(store.getState())?.ranking.ranking_version).toBe(
      "hybrid-ranking-v1",
    );
  });

  it("sends no request while the panel is idle", () => {
    const { store, actions, double } = harness(() => ({ body: response() }));
    store.dispatch(recommendationsIdle());
    store.dispatch(recommendationsIdle());
    expect(double.calls).toEqual([]);
    expect(selectRecommendationStatus(store.getState())).toBe("idle");
    expect(actions).toBeDefined();
  });

  it("refuses to build a body outside #28's limit bound", () => {
    expect(() => recommendationBody({ paletteId: PALETTE_ID, revision: 1, limit: LIMIT_MIN - 1 }))
      .toThrow(RangeError);
    expect(() => recommendationBody({ paletteId: PALETTE_ID, revision: 1, limit: LIMIT_MAX + 1 }))
      .toThrow(RangeError);
    expect(recommendationBody({ paletteId: PALETTE_ID, revision: 1, limit: LIMIT_MIN })).toEqual({
      palette_id: PALETTE_ID,
      revision: 1,
      limit: LIMIT_MIN,
      filters: {},
    });
  });

  it("copies only the two documented filter booleans, and drops anything else", () => {
    const body = recommendationBody({
      paletteId: PALETTE_ID,
      revision: 3,
      filters: { tempo_lock: true, exact_key_lock: false, kick_id: KICK } as unknown as {
        tempo_lock: boolean;
        exact_key_lock: boolean;
      },
    });
    expect(body.filters).toEqual({ tempo_lock: true, exact_key_lock: false });
    expect(Object.keys(body.filters as object).sort()).toEqual(["exact_key_lock", "tempo_lock"]);
  });

  it("surfaces a 409's code and current revision as the stale state, once", async () => {
    let calls = 0;
    const { store, actions, double, request } = harness(() => {
      calls += 1;
      return {
        status: 409,
        body: envelope("revision_conflict", { expected_revision: REVISION, current_revision: 6 }),
      };
    });
    await store.dispatch(actions.run(request));
    // The first conflict re-issues once at the revision the service named; the
    // second renders the same state and issues nothing further.
    expect(calls).toBe(2);
    expect(double.path()).toEqual(["POST /recommendations", "POST /recommendations"]);
    expect(bodyOf(double.calls[1]).revision).toBe(6);
    expect(selectRecommendationStatus(store.getState())).toBe("stale");
    expect(selectRecommendationBatch(store.getState())).toBeNull();
    expect(selectRecommendationStale(store.getState())).toEqual({
      requestedRevision: 6,
      currentRevision: 6,
    });
  });

  it("re-issues once at the named revision when the palette has moved on", async () => {
    const { store, actions, double, request } = harness((call) =>
      bodyOf(call).revision === REVISION
        ? {
            status: 409,
            body: envelope("revision_conflict", { expected_revision: REVISION, current_revision: 6 }),
          }
        : { body: response({ batch: { palette: { ...response().recommendation.palette, revision: 6 } } }) },
    );
    await store.dispatch(actions.run(request));
    expect(double.path()).toEqual(["POST /recommendations", "POST /recommendations"]);
    expect(bodyOf(double.calls[1]).revision).toBe(6);
    expect(selectRecommendationStatus(store.getState())).toBe("ready");
    expect(selectRecommendationBatch(store.getState())?.palette.revision).toBe(6);
  });

  it("reports an unreachable service as unavailable, with no cards", async () => {
    const { store, actions, request } = harness(() => ({ reject: true }));
    await store.dispatch(actions.run(request));
    expect(selectRecommendationStatus(store.getState())).toBe("unavailable");
    expect(selectRecommendationBatch(store.getState())).toBeNull();
  });

  it("reports an envelope code verbatim and applies no cards", async () => {
    // A 409 that is not a revision conflict is not this run's to re-issue: the
    // code decides the state, so the panel names what to fix instead.
    const { store, actions, request } = harness(() => ({
      status: 409,
      body: envelope("palette_incomplete", {}),
    }));
    await store.dispatch(actions.run(request));
    expect(selectRecommendationStatus(store.getState())).toBe("error");
    expect(store.getState().recommendations.error).toEqual({
      source: "service",
      code: "palette_incomplete",
      retryable: false,
    });
  });

  it("renders a 200 it cannot read as a client error rather than as cards", async () => {
    const { store, actions, request } = harness(() => ({ body: MALFORMED }));
    await store.dispatch(actions.run(request));
    expect(selectRecommendationStatus(store.getState())).toBe("error");
    expect(store.getState().recommendations.error).toEqual({
      source: "client",
      code: "malformed_response",
      retryable: false,
    });
    expect(selectRecommendationBatch(store.getState())).toBeNull();
  });

  it("aborts on cancel: one request, no data, and no outcome afterwards", async () => {
    const { store, actions, double, request } = harness(() => ({ pending: true }));
    const running = store.dispatch(actions.run(request));
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(double.path()).toEqual(["POST /recommendations"]);
    store.dispatch(actions.cancel());
    await running;
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(double.path()).toEqual(["POST /recommendations"]);
    expect(selectRecommendationStatus(store.getState())).toBe("cancelled");
    expect(selectRecommendationBatch(store.getState())).toBeNull();
  });

  it("aborts the previous run when the kick changes, and never mixes the two", async () => {
    const seen: string[] = [];
    const { store, actions, double, request } = harness((call) => {
      const body = bodyOf(call);
      seen.push(String(body.revision));
      return body.revision === REVISION
        ? { pending: true }
        : {
            body: response({ batch: { palette: { ...response().recommendation.palette, revision: 5 } } }),
          };
    });
    const first = store.dispatch(actions.run(request));
    await store.dispatch(actions.run({ ...request, revision: 5 }));
    await first;
    expect(double.path()).toEqual(["POST /recommendations", "POST /recommendations"]);
    expect(seen).toEqual(["4", "5"]);
    expect(selectRecommendationStatus(store.getState())).toBe("ready");
    expect(selectRecommendationBatch(store.getState())?.palette.revision).toBe(5);
  });
});

describe("POST /outcomes", () => {
  it("selects by writing the palette item first and recording second", async () => {
    const { store, actions, double, request } = harness((call) => {
      if (call.url.endsWith("/recommendations")) {
        return { body: response() };
      }
      if (call.url.endsWith("/outcomes")) {
        return { status: 201, body: outcome(true) };
      }
      return {
        status: 200,
        body: { api_schema: "1.0", changed: true, palette: { ...palette(), revision: 5 } },
      };
    });
    await store.dispatch(actions.run(request));
    await store.dispatch(actions.select(candidateId(1)));
    expect(double.path()).toEqual(["POST /recommendations", "PUT /palette/items/bass", "POST /outcomes"]);
    const written = bodyOf(double.calls[1]);
    expect(written).toEqual({
      palette_id: PALETTE_ID,
      sample_id: candidateId(1),
      expected_revision: REVISION,
    });
    const recorded = bodyOf(double.calls[2]);
    expect(Object.keys(recorded).sort()).toEqual([
      "candidate_analysis_version",
      "candidate_id",
      "client_event_id",
      "event_type",
      "mode",
      "palette_id",
      "palette_revision",
      "project_id",
      "ranking_version",
      "run_id",
    ]);
    expect(recorded).toMatchObject({
      event_type: "selected",
      project_id: "project-001",
      palette_id: PALETTE_ID,
      candidate_id: candidateId(1),
      run_id: response().run.run_id,
      palette_revision: REVISION,
      ranking_version: "hybrid-ranking-v1",
      mode: "dsp-only",
      candidate_analysis_version: response().recommendation.results[0].analysis_version,
    });
    expect(selectCardState(store.getState(), candidateId(1))).toBe("saved");
  });

  it("treats an idempotent 200 as success and does not resend", async () => {
    const { store, actions, double, request } = harness((call) =>
      call.url.endsWith("/recommendations")
        ? { body: response() }
        : call.url.endsWith("/outcomes")
          ? { status: 200, body: outcome(false) }
          : { status: 200, body: { api_schema: "1.0", changed: false, palette: palette() } },
    );
    await store.dispatch(actions.run(request));
    await store.dispatch(actions.select(candidateId(1)));
    expect(double.path().filter((line) => line === "POST /outcomes")).toHaveLength(1);
    expect(selectCardState(store.getState(), candidateId(1))).toBe("saved");
  });

  it("rejects with one outcome and no palette write", async () => {
    const { store, actions, double, request } = harness((call) =>
      call.url.endsWith("/recommendations")
        ? { body: response() }
        : { status: 201, body: outcome(true) },
    );
    await store.dispatch(actions.run(request));
    await store.dispatch(actions.reject(candidateId(2)));
    expect(double.path()).toEqual(["POST /recommendations", "POST /outcomes"]);
    expect(bodyOf(double.calls[1]).event_type).toBe("rejected");
    expect(selectCardState(store.getState(), candidateId(2))).toBe("rejected");
  });

  it("records an unrecorded selection when the outcome fails after the write", async () => {
    const { store, actions, double, request } = harness((call) => {
      if (call.url.endsWith("/recommendations")) {
        return { body: response() };
      }
      if (call.url.endsWith("/outcomes")) {
        return { status: 503, body: envelope("database_locked", {}) };
      }
      return { status: 200, body: { api_schema: "1.0", changed: true, palette: palette() } };
    });
    await store.dispatch(actions.run(request));
    await store.dispatch(actions.select(candidateId(1)));
    expect(selectCardState(store.getState(), candidateId(1))).toBe("unrecorded");
    expect(store.getState().recommendations.actions[0].retryable).toBe(true);
    await store.dispatch(actions.retryCard(candidateId(1)));
    // The retry replays both steps and reuses one client_event_id verbatim.
    expect(double.path()).toEqual([
      "POST /recommendations",
      "PUT /palette/items/bass",
      "POST /outcomes",
      "PUT /palette/items/bass",
      "POST /outcomes",
    ]);
    const first = bodyOf(double.calls[2]).client_event_id;
    const second = bodyOf(double.calls[4]).client_event_id;
    expect(second).toBe(first);
  });

  it("keeps a failed palette write visible and never reports it saved", async () => {
    const { store, actions, request } = harness((call) => {
      if (call.url.endsWith("/recommendations")) {
        return { body: response() };
      }
      return { status: 503, body: envelope("database_locked", {}) };
    });
    await store.dispatch(actions.run(request));
    await store.dispatch(actions.select(candidateId(1)));
    expect(selectCardState(store.getState(), candidateId(1))).toBe("error");
    expect(store.getState().recommendations.actions[0]).toMatchObject({
      errorCode: "database_locked",
      retryable: true,
      paletteWritten: false,
    });
  });

  it("ignores a second click while the action is pending", async () => {
    const { store, actions, double, request } = harness((call) =>
      call.url.endsWith("/recommendations")
        ? { body: response() }
        : { status: 201, body: outcome(true) },
    );
    await store.dispatch(actions.run(request));
    const first = store.dispatch(actions.select(candidateId(1)));
    const second = store.dispatch(actions.select(candidateId(1)));
    await Promise.all([first, second]);
    expect(double.path().filter((line) => line === "PUT /palette/items/bass")).toHaveLength(1);
    expect(double.path().filter((line) => line === "POST /outcomes")).toHaveLength(1);
  });

  it("contacts nothing but the three documented routes", async () => {
    const { store, actions, double, request } = harness((call) =>
      call.url.endsWith("/recommendations")
        ? { body: response() }
        : call.url.endsWith("/outcomes")
          ? { status: 201, body: outcome(true) }
          : { status: 200, body: { api_schema: "1.0", changed: true, palette: palette() } },
    );
    await store.dispatch(actions.run(request));
    await store.dispatch(actions.select(candidateId(1)));
    await store.dispatch(actions.reject(candidateId(2)));
    for (const call of double.calls) {
      const url = new URL(call.url);
      expect(url.origin).toBe(ORIGIN);
      expect(["POST /recommendations", "POST /outcomes", "PUT /palette/items/bass"]).toContain(
        `${call.method} ${url.pathname}`,
      );
    }
    expect(double.calls.every((call) => call.url.startsWith(ORIGIN))).toBe(true);
    const events = double.calls
      .filter((call) => call.url.endsWith("/outcomes"))
      .map((call) => bodyOf(call).event_type);
    expect(events).toEqual(["selected", "rejected"]);
    expect(events).not.toContain("auditioned");
  });

  it("builds the ten-key outcome body from named fields only", () => {
    const body = outcomeBody({
      clientEventId: "client-001",
      eventType: "selected",
      projectId: "project-001",
      paletteId: PALETTE_ID,
      candidateId: candidateId(1),
      runId: response().run.run_id,
      paletteRevision: 4,
      rankingVersion: "hybrid-ranking-v1",
      mode: "dsp-only",
      candidateAnalysisVersion: "b".repeat(64),
    });
    expect(Object.keys(body).sort()).toEqual([
      "candidate_analysis_version",
      "candidate_id",
      "client_event_id",
      "event_type",
      "mode",
      "palette_id",
      "palette_revision",
      "project_id",
      "ranking_version",
      "run_id",
    ]);
  });
});
