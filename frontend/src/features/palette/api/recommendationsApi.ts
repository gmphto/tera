/**
 * The two recommendation-flow routes (issue #33), injected into #30's api.
 *
 * `frontend/src/app/api.ts` stays byte-unchanged: this module extends the one api
 * slice with `injectEndpoints` and declares exactly two endpoints —
 * `getRecommendations` (`POST /recommendations`) and `recordOutcome`
 * (`POST /outcomes`). No cache tag is added, so a refresh is the query's own
 * `refetch`, and no other route is reachable from the panel.
 *
 * Both bodies are built here, from named fields only, so a test can assert the
 * exact keys without a component: the recommendation body is the four keys #28
 * documents, and the outcome body is the ten keys #29 requires, copied from the
 * batch, the run block, the result and the palette state. Nothing is defaulted
 * except this client's own `CLIENT_RESULT_LIMIT`, and no `kick_id`, `run_id`
 * request field, unknown key or out-of-bound `limit` can be sent.
 */

import { createTeraApi, teraApi } from "../../../app/api";
import { CLIENT_RESULT_LIMIT } from "../recommendations/cards";
import { parseBatch, type BatchParse, type BatchWire, type RunWire } from "./batch";

type TeraApi = ReturnType<typeof createTeraApi>;

export const RECOMMENDATIONS_PATH = "/recommendations";
export const OUTCOMES_PATH = "/outcomes";

/** #28's own bound on `limit`; the client sends a value inside it. */
export const LIMIT_MIN = 5;
export const LIMIT_MAX = 20;

/**
 * #32's two filter-policy booleans, and nothing else.
 *
 * The landed palette state exposes no filter control, so the client sends `{}`;
 * the keys exist so a later palette state can pass them without this module
 * widening what the body carries — the builder below copies only these two.
 */
export interface RecommendationFilters {
  tempo_lock?: boolean;
  exact_key_lock?: boolean;
}

export interface RecommendationArgs {
  paletteId: string;
  revision: number;
  limit?: number;
  filters?: RecommendationFilters;
}

/**
 * #29's whole event vocabulary for `POST /outcomes`.
 *
 * The service accepts four types; this client sends three of them — `selected`
 * and `rejected` from #33 and `auditioned` from #34 — and `removed` belongs to
 * #36. The union is the route's, not one task's, so a later task can send its
 * own event without widening a list another task owns.
 */
export const OUTCOME_EVENT_TYPES = ["selected", "rejected", "auditioned", "removed"] as const;
export type OutcomeEventType = (typeof OUTCOME_EVENT_TYPES)[number];

export interface OutcomeArgs {
  clientEventId: string;
  eventType: OutcomeEventType;
  projectId: string;
  paletteId: string;
  candidateId: string;
  runId: string;
  paletteRevision: number;
  rankingVersion: string;
  mode: string;
  candidateAnalysisVersion: string;
}

export interface RecommendationEnvelope {
  api_schema: string;
  recommendation: BatchWire;
  run: RunWire;
}

export interface OutcomeEnvelope {
  api_schema: string;
  created: boolean;
  outcome: { event_id: number; client_event_id: string; event_type: string };
}

/** The two booleans of `filters`, copied one by one or omitted entirely. */
export function filterBody(filters: RecommendationFilters | undefined): Record<string, boolean> {
  const body: Record<string, boolean> = {};
  if (typeof filters?.tempo_lock === "boolean") {
    body.tempo_lock = filters.tempo_lock;
  }
  if (typeof filters?.exact_key_lock === "boolean") {
    body.exact_key_lock = filters.exact_key_lock;
  }
  return body;
}

/** The exact four-key `POST /recommendations` body, or a refusal to build one. */
export function recommendationBody(args: RecommendationArgs): Record<string, unknown> {
  const limit = args.limit ?? CLIENT_RESULT_LIMIT;
  if (!Number.isInteger(limit) || limit < LIMIT_MIN || limit > LIMIT_MAX) {
    throw new RangeError(`limit ${limit} is outside ${LIMIT_MIN}..${LIMIT_MAX}`);
  }
  return {
    palette_id: args.paletteId,
    revision: args.revision,
    limit,
    filters: filterBody(args.filters),
  };
}

/** The exact ten-key `POST /outcomes` body #29 validates. */
export function outcomeBody(args: OutcomeArgs): Record<string, unknown> {
  return {
    client_event_id: args.clientEventId,
    event_type: args.eventType,
    project_id: args.projectId,
    palette_id: args.paletteId,
    candidate_id: args.candidateId,
    run_id: args.runId,
    palette_revision: args.paletteRevision,
    ranking_version: args.rankingVersion,
    mode: args.mode,
    candidate_analysis_version: args.candidateAnalysisVersion,
  };
}

/**
 * The outcome body of one select, copied from a batch and its run block.
 *
 * Every value is the response's own: the palette revision and project come from
 * the batch's palette, the run identity from the run block, and the candidate's
 * analysis version from that result. The client caches nothing from a previous
 * run and defaults nothing.
 */
export function selectionOutcome(args: {
  clientEventId: string;
  batch: BatchWire;
  run: RunWire;
  candidateId: string;
  projectId: string;
}): OutcomeArgs {
  const result =
    args.batch.results.find((candidate) => candidate.candidate_id === args.candidateId) ?? null;
  if (result === null) {
    throw new RangeError("the candidate is not one of this batch's results");
  }
  return {
    clientEventId: args.clientEventId,
    eventType: "selected",
    projectId: args.projectId,
    paletteId: args.batch.palette.palette_id,
    candidateId: result.candidate_id,
    runId: args.run.run_id,
    paletteRevision: args.batch.palette.revision,
    rankingVersion: args.batch.ranking_version,
    mode: args.batch.mode,
    candidateAnalysisVersion: result.analysis_version,
  };
}

/** The same identity fields as a select, with #29's rejection event type. */
export function rejectionOutcome(args: {
  clientEventId: string;
  batch: BatchWire;
  run: RunWire;
  candidateId: string;
  projectId: string;
}): OutcomeArgs {
  return { ...selectionOutcome(args), eventType: "rejected" };
}

/**
 * The recommendation query's data is the validated payload, not the raw body:
 * `parseBatch` runs in `transformResponse`, so a 200 this client cannot read
 * arrives at the caller as a closed client code rather than as cards.
 */
export function injectRecommendationEndpoints(api: TeraApi) {
  return api.injectEndpoints({
    endpoints: (build) => ({
      getRecommendations: build.query<BatchParse, RecommendationArgs>({
        query: (args) => ({
          url: RECOMMENDATIONS_PATH,
          method: "POST",
          body: recommendationBody(args),
        }),
        transformResponse: (payload: unknown): BatchParse => parseBatch(payload),
      }),
      recordOutcome: build.mutation<OutcomeEnvelope, OutcomeArgs>({
        query: (args) => ({
          url: OUTCOMES_PATH,
          method: "POST",
          body: outcomeBody(args),
        }),
      }),
    }),
  });
}

const recommendationsApi = injectRecommendationEndpoints(teraApi);

export const { useGetRecommendationsQuery, useRecordOutcomeMutation } = recommendationsApi;
