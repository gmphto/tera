/**
 * The five palette and project routes (issue #32), injected into #30's api.
 *
 * Every endpoint is defined against the documented request and nothing else:
 * the item and context writes carry `palette_id`, `expected_revision` and their
 * own fields, and `POST /projects` carries the two name fields. The endpoints
 * also capture the epoch when a request is issued and dispatch the applied
 * response through the revision guard, so a late answer for a superseded action
 * is refused rather than rendered.
 */

import type { RootState } from "../../../app/store";
import { createTeraApi, teraApi } from "../../../app/api";
import { toLibraryError } from "./errors";
import { paletteResponseApplied } from "../state/paletteSlice";

type TeraApi = ReturnType<typeof createTeraApi>;

export const PALETTE_TAG = "Palette";
export const PALETTE_PATH = "/palette";
export const PROJECTS_PATH = "/projects";
export const PALETTE_ITEM_PATH = "/palette/items/{slot}";
export const PALETTE_CONTEXT_PATH = "/palette/context";

/**
 * The three codes these routes add to what #31's closed client union carries.
 *
 * `backend/api/errors.py` is one table, so the palette routes answer with codes
 * the six library and import routes never produce. `#31`'s `errors.ts` holds the
 * union for those six routes and is not this task's to widen, so the bridge is
 * here: the code is read as the string the service sent, and compared against
 * the literals this task's routes document.
 */
export const UNKNOWN_PROJECT = "unknown_project";
export const REVISION_CONFLICT = "revision_conflict";
export const PROJECT_EXISTS = "project_exists";
export const INVALID_CONTEXT = "invalid_context";

/** The refusal code of an api-shaped error, or null for a transport failure. */
export function apiCode(error: { kind: string; code?: string }): string | null {
  return error.kind === "api" && typeof error.code === "string" ? error.code : null;
}

/**
 * The code and details of one refusal, read straight from #27's envelope.
 *
 * `#31`'s `toLibraryError` closes its union to the six library and import routes
 * and coerces anything else to `internal_error`, so a palette code would be lost
 * by the time it arrived. The envelope itself is the contract both tasks share,
 * so the palette reads it here.
 */
export function refusal(error: unknown): { code: string | null; details: Record<string, unknown> } {
  if (typeof error !== "object" || error === null) {
    return { code: null, details: {} };
  }
  const { status, data } = error as { status?: unknown; data?: unknown };
  if (typeof status !== "number" || typeof data !== "object" || data === null) {
    return { code: null, details: {} };
  }
  const envelope = (data as { error?: unknown }).error;
  if (typeof envelope !== "object" || envelope === null) {
    return { code: null, details: {} };
  }
  const { code, details } = envelope as { code?: unknown; details?: unknown };
  return {
    code: typeof code === "string" ? code : null,
    details: typeof details === "object" && details !== null ? (details as Record<string, unknown>) : {},
  };
}

export interface MutationOutcome {
  applied: boolean;
  conflict: boolean;
  failure: unknown | null;
}

/**
 * Send one palette mutation, re-reading and retrying once on a conflict.
 *
 * The whole rule is here rather than in a component so it can be driven against
 * a stub: a `revision_conflict` re-reads the palette and, when the caller still
 * owns the newest action, sends the same mutation once with the freshly read
 * revision. A second conflict is reported, never retried again.
 */
export async function sendPaletteMutation(options: {
  send: (revision: number) => Promise<unknown>;
  revision: number;
  reread: () => Promise<number | null>;
  mayRetry: () => boolean;
}): Promise<MutationOutcome> {
  try {
    await options.send(options.revision);
    return { applied: true, conflict: false, failure: null };
  } catch (error) {
    if (refusal(error).code !== REVISION_CONFLICT) {
      return { applied: false, conflict: false, failure: error };
    }
    const fresh = await options.reread();
    if (fresh === null || !options.mayRetry()) {
      return { applied: false, conflict: true, failure: null };
    }
    try {
      await options.send(fresh);
      return { applied: true, conflict: false, failure: null };
    } catch {
      return { applied: false, conflict: true, failure: null };
    }
  }
}

/** One context field exactly as the route documents it. */
export type ContextFieldWire =
  | { state: "known"; bpm?: number; tonic?: string; mode?: string; genre?: string }
  | { state: "unknown"; reason: string }
  | { state: "unset" };

export interface PaletteItemWire {
  slot: string;
  sample_id: string;
  role: string;
  added_revision: number;
  sample_state: string;
  sample_error_code: string | null;
  slot_role_mismatch: boolean;
}

export interface PaletteWire {
  palette_id: string;
  project: { project_id: string; name: string };
  name: string;
  revision: number;
  context: { tempo: ContextFieldWire; key: ContextFieldWire; genre: ContextFieldWire };
  items: { kick: PaletteItemWire | null; bass: PaletteItemWire | null };
}

export interface PaletteEnvelope {
  api_schema: string;
  palette: PaletteWire;
}

export interface MutationEnvelope extends PaletteEnvelope {
  changed: boolean;
}

export interface SetItemArgs {
  slot: string;
  paletteId: string;
  sampleId: string;
  revision: number;
}

export interface SetContextArgs {
  paletteId: string;
  revision: number;
  tempo: ContextFieldWire;
  key: ContextFieldWire;
  genre: ContextFieldWire;
}

export interface CreateProjectArgs {
  name: string;
  paletteName?: string;
}

export function injectPaletteEndpoints(api: TeraApi) {
  return api.injectEndpoints({
    endpoints: (build) => ({
      getPalette: build.query<PaletteEnvelope, string | void>({
        query: (projectId) =>
          projectId === undefined || projectId === null
            ? PALETTE_PATH
            : `${PALETTE_PATH}?project_id=${projectId}`,
        providesTags: [PALETTE_TAG],
      }),
      createProject: build.mutation<PaletteEnvelope, CreateProjectArgs>({
        query: (args) => ({
          url: PROJECTS_PATH,
          method: "POST",
          body:
            args.paletteName === undefined
              ? { name: args.name }
              : { name: args.name, palette_name: args.paletteName },
        }),
        invalidatesTags: [PALETTE_TAG],
      }),
      setPaletteItem: build.mutation<MutationEnvelope, SetItemArgs>({
        query: (args) => ({
          url: PALETTE_ITEM_PATH.replace("{slot}", args.slot),
          method: "PUT",
          body: {
            palette_id: args.paletteId,
            sample_id: args.sampleId,
            expected_revision: args.revision,
          },
        }),
        invalidatesTags: [PALETTE_TAG],
        async onQueryStarted(_args, { dispatch, getState, queryFulfilled }) {
          const issuedEpoch = (getState() as RootState).palette.epoch;
          try {
            const { data } = await queryFulfilled;
            dispatch(paletteResponseApplied({
              paletteId: data.palette.palette_id,
              revision: data.palette.revision,
              issuedEpoch,
              palette: data.palette,
            }));
          } catch {
            // A refusal is the caller's to report; the guard only applies answers.
          }
        },
      }),
      setPaletteContext: build.mutation<MutationEnvelope, SetContextArgs>({
        query: (args) => ({
          url: PALETTE_CONTEXT_PATH,
          method: "PUT",
          body: {
            palette_id: args.paletteId,
            expected_revision: args.revision,
            tempo: args.tempo,
            key: args.key,
            genre: args.genre,
          },
        }),
        invalidatesTags: [PALETTE_TAG],
        async onQueryStarted(_args, { dispatch, getState, queryFulfilled }) {
          const issuedEpoch = (getState() as RootState).palette.epoch;
          try {
            const { data } = await queryFulfilled;
            dispatch(paletteResponseApplied({
              paletteId: data.palette.palette_id,
              revision: data.palette.revision,
              issuedEpoch,
              palette: data.palette,
            }));
          } catch {
            // As above: the panel reports a failed save from the caller's catch.
          }
        },
      }),
    }),
  });
}

const paletteApi = injectPaletteEndpoints(teraApi);

export const {
  useGetPaletteQuery,
  useCreateProjectMutation,
  useSetPaletteItemMutation,
  useSetPaletteContextMutation,
} = paletteApi;
