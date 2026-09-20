/**
 * The six #27 routes this feature calls, injected into #30's api (issue #31).
 *
 * The endpoints are added here rather than in `app/api.ts`, so the shell keeps
 * one api instance, one base query and one factory signature. Nothing in this
 * module calls a route outside the six above: no status route, no audio path and
 * no palette, project, recommendation or outcome route.
 */

import { createTeraApi, teraApi } from "../../../app/api";
import type {
  ImportCancelResponse,
  ImportRetryResponse,
  ImportStartResponse,
  ImportStatusResponse,
  PaletteRole,
  SampleDetailResponse,
  SamplePage,
  SampleQueryArgs,
} from "./types";

type TeraApi = ReturnType<typeof createTeraApi>;

/** The tag types this feature adds to the shell's api. */
export const TAG_TYPES = ["Samples", "Sample", "Import"] as const;
export type Samples = "Samples";
export type Sample = "Sample";
export type Import = "Import";

const SAMPLES_PATH = "/library/samples";
const IMPORTS_PATH = "/imports";
const LIST_ID = "LIST";

export interface StartImportArgs {
  root: string;
  role: PaletteRole;
}

/**
 * The exact query #27 documents: `role` repeated once per selected role, `q`
 * only for a non-empty text, `limit` always, `cursor` only when one is set.
 *
 * The query string is built here rather than handed to `params`, because the
 * base query's default serialiser joins an array into one comma-separated value
 * (`role=kick%2Cbass`), which #27 refuses with `invalid_role`; it also sends a
 * bare `role=` for an empty array.
 */
function listUrl(args: SampleQueryArgs): string {
  const search = new URLSearchParams();
  for (const role of args.roles) {
    search.append("role", role);
  }
  if (args.text !== null && args.text !== "") {
    search.append("q", args.text);
  }
  search.append("limit", String(args.limit));
  if (args.cursor !== null) {
    search.append("cursor", args.cursor);
  }
  return `${SAMPLES_PATH}?${search.toString()}`;
}

export function injectLibraryEndpoints(api: TeraApi) {
  return api
    .enhanceEndpoints({ addTagTypes: [...TAG_TYPES] })
    .injectEndpoints({
      endpoints: (build) => ({
        getSamples: build.query<SamplePage, SampleQueryArgs>({
          query: (args) => listUrl(args),
          providesTags: () => [{ type: "Samples" as const, id: LIST_ID }],
        }),
        getSample: build.query<SampleDetailResponse, string>({
          // A sample_id is `sha256:` plus 64 hex characters, so the path is built
          // verbatim: percent-encoding the colon could reach #27's refusal branch.
          query: (sampleId) => `${SAMPLES_PATH}/${sampleId}`,
          providesTags: (_result, _error, sampleId) => [{ type: "Sample" as const, id: sampleId }],
        }),
        startImport: build.mutation<ImportStartResponse, StartImportArgs>({
          query: (body) => ({ url: IMPORTS_PATH, method: "POST", body }),
          invalidatesTags: [{ type: "Samples" as const, id: LIST_ID }],
        }),
        getImport: build.query<ImportStatusResponse, string>({
          query: (runId) => `${IMPORTS_PATH}/${runId}`,
          providesTags: (_result, _error, runId) => [{ type: "Import" as const, id: runId }],
        }),
        // #27 refuses a non-GET request without `application/json`, so both
        // mutations send an empty JSON object body.
        cancelImport: build.mutation<ImportCancelResponse, string>({
          query: (runId) => ({ url: `${IMPORTS_PATH}/${runId}/cancel`, method: "POST", body: {} }),
          invalidatesTags: (_result, _error, runId) => [{ type: "Import" as const, id: runId }],
        }),
        retryImport: build.mutation<ImportRetryResponse, string>({
          query: (runId) => ({ url: `${IMPORTS_PATH}/${runId}/retry`, method: "POST", body: {} }),
          invalidatesTags: (_result, _error, runId) => [
            { type: "Import" as const, id: runId },
            { type: "Samples" as const, id: LIST_ID },
          ],
        }),
      }),
    });
}

const libraryApi = injectLibraryEndpoints(teraApi);

export const {
  useGetSamplesQuery,
  useGetSampleQuery,
  useStartImportMutation,
  useGetImportQuery,
  useCancelImportMutation,
  useRetryImportMutation,
} = libraryApi;
