/**
 * The recommendation slice (issue #33).
 *
 * It holds the run's payloads and every transition between the panel's states,
 * so no component keeps ad-hoc state for anything here. A response is applied
 * only when it answers the active request *and* its `batch.palette.revision` is
 * the revision that request asked for; anything else is counted and dropped, so
 * two runs' cards can never be on screen together.
 *
 * The select and reject actions live here too, because the order is the rule:
 * a select writes #32's palette item first and records #29's outcome second,
 * while a reject records the outcome alone. One `client_event_id` is created
 * when an action starts and is reused verbatim by every retry of that action.
 */

import { createSlice, type PayloadAction, type ThunkAction, type UnknownAction } from "@reduxjs/toolkit";

import { teraApi } from "../../../app/api";
import type { BatchWire, RunWire } from "../api/batch";
import { isRetryable } from "../api/batch";
import {
  LIMIT_MIN,
  LIMIT_MAX,
  type RecommendationArgs,
  type RecommendationFilters,
  injectRecommendationEndpoints,
  rejectionOutcome,
  selectionOutcome,
} from "../api/recommendationsApi";
import { injectPaletteEndpoints } from "../api/paletteApi";
import { CLIENT_RESULT_LIMIT, type CardState } from "../recommendations/cards";
import type { PaletteState } from "./paletteSlice";

/** The panel's closed set of states; exactly one renders at a time. */
export const RECOMMENDATION_STATUSES = [
  "idle",
  "loading",
  "ready",
  "cancelled",
  "stale",
  "error",
  "unavailable",
] as const;
export type RecommendationStatus = (typeof RECOMMENDATION_STATUSES)[number];

/** What one request asked for, and the identity a response must answer. */
export interface RecommendationRequest {
  paletteId: string;
  revision: number;
  limit: number;
  filters: RecommendationFilters;
  kickId: string;
}

/** The kind of action a card can run, and the event each one records. */
export const CARD_ACTION_KINDS = ["select", "reject"] as const;
export type CardActionKind = (typeof CARD_ACTION_KINDS)[number];

export interface CardAction {
  candidateId: string;
  kind: CardActionKind;
  /** Created once, when the action starts; every retry reuses it verbatim. */
  clientEventId: string;
  state: CardState;
  /** True once the palette write of a select has succeeded. */
  paletteWritten: boolean;
  errorCode: string | null;
  retryable: boolean;
}

export interface RecommendationError {
  source: "service" | "client";
  code: string;
  retryable: boolean;
}

export interface RecommendationsState {
  status: RecommendationStatus;
  request: RecommendationRequest | null;
  /** Bumped on every start; a response carrying an older serial is dropped. */
  serial: number;
  batch: BatchWire | null;
  run: RunWire | null;
  stale: { requestedRevision: number; currentRevision: number | null } | null;
  error: RecommendationError | null;
  /** One automatic re-issue with a newer revision, and never a second. */
  autoRetried: boolean;
  actions: CardAction[];
  /** How many responses the identity rule refused; a test proves they were. */
  droppedResponses: number;
}

const initialState: RecommendationsState = {
  status: "idle",
  request: null,
  serial: 0,
  batch: null,
  run: null,
  stale: null,
  error: null,
  autoRetried: false,
  actions: [],
  droppedResponses: 0,
};

/** The request body of one run, from the request identity alone. */
export function recommendationArgs(request: RecommendationRequest): RecommendationArgs {
  return {
    paletteId: request.paletteId,
    revision: request.revision,
    limit: request.limit,
    filters: request.filters,
  };
}

/** The identity a request for one palette, revision and kick asks for. */
export function recommendationRequest(args: {
  paletteId: string;
  revision: number;
  kickId: string;
  limit?: number;
  filters?: RecommendationFilters;
}): RecommendationRequest {
  const limit = args.limit ?? CLIENT_RESULT_LIMIT;
  if (!Number.isInteger(limit) || limit < LIMIT_MIN || limit > LIMIT_MAX) {
    throw new RangeError(`limit ${limit} is outside ${LIMIT_MIN}..${LIMIT_MAX}`);
  }
  return {
    paletteId: args.paletteId,
    revision: args.revision,
    kickId: args.kickId,
    limit,
    filters: args.filters ?? {},
  };
}

function actionFor(state: RecommendationsState, candidateId: string): CardAction | null {
  return state.actions.find((action) => action.candidateId === candidateId) ?? null;
}

const recommendationsSlice = createSlice({
  name: "recommendations",
  initialState,
  reducers: {
    /** No kick selected: the panel's idle state, and zero requests. */
    recommendationsIdle(state) {
      state.status = "idle";
      state.request = null;
      state.batch = null;
      state.run = null;
      state.stale = null;
      state.error = null;
    },
    recommendationsStarted(state, action: PayloadAction<RecommendationRequest>) {
      // The previous run's cards go before the new response arrives.
      state.serial += 1;
      state.request = action.payload;
      state.status = "loading";
      state.batch = null;
      state.run = null;
      state.stale = null;
      state.error = null;
      state.autoRetried = false;
    },
    recommendationsReceived(
      state,
      action: PayloadAction<{ serial: number; batch: BatchWire; run: RunWire }>,
    ) {
      const { serial, batch, run } = action.payload;
      const request = state.request;
      if (serial !== state.serial || request === null) {
        state.droppedResponses += 1;
        return;
      }
      if (batch.palette.revision !== request.revision) {
        // A response for another revision is not this run's answer, whatever it
        // carries: its cards would describe a palette this client did not ask
        // about, so they are discarded rather than shown.
        state.batch = null;
        state.run = null;
        state.status = "stale";
        state.stale = {
          requestedRevision: request.revision,
          currentRevision: batch.palette.revision,
        };
        return;
      }
      state.status = "ready";
      state.batch = batch;
      state.run = run;
      state.error = null;
      state.stale = null;
      // An in-flight action for a candidate that is still present survives the
      // refresh; one for a candidate the new batch dropped is forgotten.
      const present = new Set(batch.results.map((result) => result.candidate_id));
      state.actions = state.actions.filter((entry) => present.has(entry.candidateId));
    },
    recommendationsDropped(state) {
      state.droppedResponses += 1;
    },
    recommendationsCancelled(state) {
      // Cancelling supersedes the request it aborts: the serial moves on, so a
      // settle that arrives afterwards is dropped by the same guard a late
      // success meets, whatever shape the abort takes.
      state.serial += 1;
      state.status = "cancelled";
      state.batch = null;
      state.run = null;
      state.stale = null;
      state.error = null;
    },
    recommendationsStaled(
      state,
      action: PayloadAction<{
        requestedRevision: number;
        currentRevision: number | null;
        autoRetried: boolean;
      }>,
    ) {
      state.status = "stale";
      state.batch = null;
      state.run = null;
      state.stale = {
        requestedRevision: action.payload.requestedRevision,
        currentRevision: action.payload.currentRevision,
      };
      state.autoRetried = action.payload.autoRetried;
    },
    recommendationsRefused(
      state,
      action: PayloadAction<{ serial: number; error: RecommendationError }>,
    ) {
      if (action.payload.serial !== state.serial) {
        state.droppedResponses += 1;
        return;
      }
      state.status = "error";
      state.batch = null;
      state.run = null;
      state.error = action.payload.error;
    },
    recommendationsUnavailable(state, action: PayloadAction<{ serial: number }>) {
      if (action.payload.serial !== state.serial) {
        state.droppedResponses += 1;
        return;
      }
      state.status = "unavailable";
      state.batch = null;
      state.run = null;
    },
    actionStarted(
      state,
      action: PayloadAction<{ candidateId: string; kind: CardActionKind; clientEventId: string }>,
    ) {
      const { candidateId, kind, clientEventId } = action.payload;
      const existing = actionFor(state, candidateId);
      if (existing !== null && existing.state === "pending") {
        // A click while the action is pending is ignored, so a double click can
        // produce neither a second palette write nor a second outcome.
        return;
      }
      if (existing === null) {
        state.actions.push({
          candidateId,
          kind,
          clientEventId,
          state: "pending",
          paletteWritten: false,
          errorCode: null,
          retryable: false,
        });
        return;
      }
      existing.kind = kind;
      existing.clientEventId = clientEventId;
      existing.state = "pending";
      existing.errorCode = null;
      existing.retryable = false;
    },
    actionPaletteWritten(state, action: PayloadAction<{ candidateId: string }>) {
      const existing = actionFor(state, action.payload.candidateId);
      if (existing !== null) {
        existing.paletteWritten = true;
      }
    },
    actionSucceeded(
      state,
      action: PayloadAction<{ candidateId: string; state: Extract<CardState, "saved" | "rejected"> }>,
    ) {
      const existing = actionFor(state, action.payload.candidateId);
      if (existing !== null) {
        existing.state = action.payload.state;
        existing.errorCode = null;
        existing.retryable = false;
      }
    },
    actionFailed(
      state,
      action: PayloadAction<{ candidateId: string; errorCode: string; retryable: boolean }>,
    ) {
      const existing = actionFor(state, action.payload.candidateId);
      if (existing === null) {
        return;
      }
      // A select whose palette write landed but whose outcome did not is
      // `unrecorded`, never `error`: the item is selected and only the event is
      // missing, and the two are retried differently by the producer.
      existing.state = existing.paletteWritten ? "unrecorded" : "error";
      existing.errorCode = action.payload.errorCode;
      existing.retryable = action.payload.retryable;
    },
  },
});

export const {
  recommendationsIdle,
  recommendationsStarted,
  recommendationsReceived,
  recommendationsDropped,
  recommendationsCancelled,
  recommendationsStaled,
  recommendationsRefused,
  recommendationsUnavailable,
  actionStarted,
  actionPaletteWritten,
  actionSucceeded,
  actionFailed,
} = recommendationsSlice.actions;

export const recommendationsReducer = recommendationsSlice.reducer;
export default recommendationsSlice.reducer;

// ---------------------------------------------------------------------------
// selectors
// ---------------------------------------------------------------------------

interface WithRecommendations {
  recommendations: RecommendationsState;
}

export function selectRecommendations(state: WithRecommendations): RecommendationsState {
  return state.recommendations;
}

export function selectRecommendationStatus(state: WithRecommendations): RecommendationStatus {
  return state.recommendations.status;
}

export function selectRecommendationBatch(state: WithRecommendations): BatchWire | null {
  return state.recommendations.batch;
}

export function selectRecommendationRun(state: WithRecommendations): RunWire | null {
  return state.recommendations.run;
}

export function selectRecommendationRequest(state: WithRecommendations): RecommendationRequest | null {
  return state.recommendations.request;
}

export function selectRecommendationStale(
  state: WithRecommendations,
): RecommendationsState["stale"] {
  return state.recommendations.stale;
}

export function selectRecommendationError(state: WithRecommendations): RecommendationError | null {
  return state.recommendations.error;
}

/** The card's action state, or `idle` for a card with no action yet. */
export function selectCardState(state: WithRecommendations, candidateId: string): CardState {
  return actionFor(state.recommendations, candidateId)?.state ?? "idle";
}

/** One record per candidate that has an action, for `data-state` attributes. */
export function selectCardStates(state: WithRecommendations): Record<string, CardState> {
  const states: Record<string, CardState> = {};
  for (const action of state.recommendations.actions) {
    states[action.candidateId] = action.state;
  }
  return states;
}

export function selectCardAction(
  state: WithRecommendations,
  candidateId: string,
): CardAction | null {
  return actionFor(state.recommendations, candidateId);
}

// ---------------------------------------------------------------------------
// the actions the panel dispatches
// ---------------------------------------------------------------------------

type TeraApi = ReturnType<typeof injectRecommendationEndpoints>;
type PaletteApi = ReturnType<typeof injectPaletteEndpoints>;

/** The state these actions read: this slice, and #32's palette for the write. */
export interface RecommendationRoot {
  recommendations: RecommendationsState;
  palette: PaletteState;
}

/**
 * One action the panel dispatches, typed as RTK's own thunk so the store
 * accepts it and so `dispatch` keeps the api's return types.
 */
export type RecommendationThunk = ThunkAction<
  Promise<void>,
  RecommendationRoot,
  unknown,
  UnknownAction
>;

/** The dispatch and getState one of those thunks is called with. */
type ThunkArguments = Parameters<RecommendationThunk>;
type Dispatch = ThunkArguments[0];
type GetState = ThunkArguments[1];

/** The one in-flight query, so a new run or a cancel can abort it. */
let active: { abort: () => void } | null = null;

interface Failure {
  code: string;
  retryable: boolean;
  conflict: boolean;
  currentRevision: number | null;
  unreachable: boolean;
}

function envelopeOf(error: unknown): Failure {
  const blank: Failure = {
    code: "",
    retryable: false,
    conflict: false,
    currentRevision: null,
    unreachable: false,
  };
  if (typeof error !== "object" || error === null) {
    return { ...blank, code: "internal_error" };
  }
  const { status, data, error: transport } = error as {
    status?: unknown;
    data?: unknown;
    error?: unknown;
  };
  if (status === "FETCH_ERROR" || status === "TIMEOUT_ERROR") {
    return { ...blank, unreachable: true, code: "service_unavailable" };
  }
  const envelope =
    typeof data === "object" && data !== null
      ? ((data as { error?: unknown }).error as { code?: unknown; details?: unknown } | undefined)
      : undefined;
  const code = typeof envelope?.code === "string" ? envelope.code : null;
  if (code === null) {
    return { ...blank, code: "internal_error", retryable: true };
  }
  const details =
    typeof envelope?.details === "object" && envelope.details !== null
      ? (envelope.details as Record<string, unknown>)
      : {};
  const current = details.current_revision;
  return {
    code,
    retryable: isRetryable(code),
    // The code decides, not the status: #29 answers `409 outcome_conflict` for
    // a rejection the service refuses, and that is a card's error, not a palette
    // revision this run could re-issue against.
    conflict: code === "revision_conflict",
    currentRevision: typeof current === "number" ? current : null,
    unreachable: false,
  };
}

function isAbort(error: unknown): boolean {
  return (
    typeof error === "object" &&
    error !== null &&
    (error as { name?: unknown }).name === "AbortError"
  );
}

/**
 * The select and reject actions, bound to one injected api instance.
 *
 * A factory rather than module-level functions so a test can drive the real
 * pipeline through its own api instance and the same code the app runs.
 */
export function createRecommendationActions(api: TeraApi) {
  // #32 injects its routes into the same api object this module extends:
  // `injectEndpoints` writes into the shared endpoint definitions and returns
  // that same object, so the palette action creators are on it at runtime. The
  // cast names what `paletteApi.ts` has already injected; it is not a second api.
  const palette = api as unknown as PaletteApi;

  async function issue(
    dispatch: Dispatch,
    getState: GetState,
    request: RecommendationRequest,
    auto: boolean,
  ): Promise<void> {
    active?.abort();
    dispatch(recommendationsStarted(request));
    const serial = getState().recommendations.serial;
    const result = dispatch(
      api.endpoints.getRecommendations.initiate(recommendationArgs(request), {
        forceRefetch: true,
      }),
    );
    active = result;
    try {
      const parsed = await result.unwrap();
      if (parsed.ok) {
        dispatch(recommendationsReceived({ serial, batch: parsed.batch, run: parsed.run }));
        return;
      }
      if (parsed.code === "stale_revision") {
        dispatch(
          recommendationsStaled({
            requestedRevision: request.revision,
            currentRevision: parsed.currentRevision,
            autoRetried: auto,
          }),
        );
        return;
      }
      dispatch(
        recommendationsRefused({
          serial,
          error: { source: "client", code: parsed.code, retryable: isRetryable(parsed.code) },
        }),
      );
    } catch (error) {
      if (isAbort(error)) {
        // A cancel or a superseded run owns the state; an abort is not an error.
        return;
      }
      const failure = envelopeOf(error);
      if (failure.unreachable) {
        dispatch(recommendationsUnavailable({ serial }));
        return;
      }
      if (failure.conflict) {
        const current = failure.currentRevision;
        dispatch(
          recommendationsStaled({
            requestedRevision: request.revision,
            currentRevision: current,
            autoRetried: auto,
          }),
        );
        // At most one automatic re-issue, and only for a revision the service
        // named: a second conflict renders the same state without another run.
        if (!auto && current !== null && current !== request.revision) {
          await issue(dispatch, getState, { ...request, revision: current }, true);
        }
        return;
      }
      dispatch(
        recommendationsRefused({
          serial,
          error: {
            source: "service",
            code: failure.code,
            retryable: failure.retryable,
          },
        }),
      );
    } finally {
      if (active === result) {
        active = null;
      }
    }
  }

  async function runCardAction(
    dispatch: Dispatch,
    getState: GetState,
    kind: CardActionKind,
    candidateId: string,
  ): Promise<void> {
    const state = getState();
    const { recommendations } = state;
    const { batch, run, request } = recommendations;
    const projectId = state.palette.project?.project_id ?? null;
    if (batch === null || run === null || request === null || projectId === null) {
      return;
    }
    const existing = actionFor(recommendations, candidateId);
    if (existing !== null && existing.state === "pending") {
      return;
    }
    const clientEventId = existing?.clientEventId ?? crypto.randomUUID();
    dispatch(actionStarted({ candidateId, kind, clientEventId }));
    const identity = { clientEventId, batch, run, candidateId, projectId };
    if (kind === "select") {
      try {
        await dispatch(
          palette.endpoints.setPaletteItem.initiate({
            slot: "bass",
            paletteId: request.paletteId,
            sampleId: candidateId,
            revision: batch.palette.revision,
          }),
        ).unwrap();
      } catch (error) {
        const failure = envelopeOf(error);
        dispatch(
          actionFailed({
            candidateId,
            errorCode: failure.code,
            retryable: failure.retryable,
          }),
        );
        return;
      }
      dispatch(actionPaletteWritten({ candidateId }));
    }
    try {
      await dispatch(
        api.endpoints.recordOutcome.initiate(
          kind === "select" ? selectionOutcome(identity) : rejectionOutcome(identity),
        ),
      ).unwrap();
      dispatch(
        actionSucceeded({ candidateId, state: kind === "select" ? "saved" : "rejected" }),
      );
    } catch (error) {
      const failure = envelopeOf(error);
      dispatch(
        actionFailed({ candidateId, errorCode: failure.code, retryable: failure.retryable }),
      );
    }
  }

  return {
    /** Start a run for one request identity, aborting whatever was in flight. */
    run(request: RecommendationRequest): RecommendationThunk {
      return (dispatch, getState) => issue(dispatch, getState, request, false);
    },
    /** Re-issue the active request as a user action. */
    retry(): RecommendationThunk {
      return (dispatch, getState) => {
        const request = getState().recommendations.request;
        if (request === null) {
          return Promise.resolve();
        }
        return issue(dispatch, getState, request, false);
      };
    },
    cancel(): RecommendationThunk {
      return (dispatch) => {
        active?.abort();
        active = null;
        dispatch(recommendationsCancelled());
        return Promise.resolve();
      };
    },
    select(candidateId: string): RecommendationThunk {
      return (dispatch, getState) => runCardAction(dispatch, getState, "select", candidateId);
    },
    reject(candidateId: string): RecommendationThunk {
      return (dispatch, getState) => runCardAction(dispatch, getState, "reject", candidateId);
    },
    /** Replay one card's whole action, with the same `client_event_id`. */
    retryCard(candidateId: string): RecommendationThunk {
      return (dispatch, getState) => {
        const existing = actionFor(getState().recommendations, candidateId);
        if (existing === null) {
          return Promise.resolve();
        }
        return runCardAction(dispatch, getState, existing.kind, candidateId);
      };
    },
  };
}

/**
 * The app's own instance of those actions.
 *
 * `injectRecommendationEndpoints` returns the same api object it extends, so
 * this is #30's one api with #33's two routes on it — not a second api slice.
 */
export const recommendationActions = createRecommendationActions(
  injectRecommendationEndpoints(teraApi),
);
