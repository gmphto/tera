import { configureStore } from "@reduxjs/toolkit";
import { afterEach, describe, expect, it } from "vitest";

import { createTeraApi } from "../../../app/api";
import serviceReducer, { statusReceived } from "../../service/serviceSlice";
import type { ServiceSnapshot } from "../../service/status";
import {
  startLibraryServer,
  type LibraryStub,
  type StubReply,
  type StubHandler,
} from "../../../test/libraryServer";
import { toLibraryError } from "./errors";
import { injectLibraryEndpoints } from "./libraryApi";
import type { SampleListItem, SampleQueryArgs } from "./types";

const SNAPSHOT: ServiceSnapshot = {
  mode: "owned",
  phase: "running",
  reason: null,
  origin: null,
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

const RUN_ID = "run-1e2ce2e140063b8fd2ab0cc64001488b";
const SAMPLE_ID = `sha256:${"a".repeat(64)}`;
const paths: string[] = [];

const servers: LibraryStub[] = [];

afterEach(async () => {
  paths.length = 0;
  while (servers.length > 0) {
    const server = servers.pop();
    if (server !== undefined) {
      await server.close();
    }
  }
});

async function stub(handler: StubHandler): Promise<LibraryStub> {
  const server = await startLibraryServer((request) => {
    paths.push(request.url);
    return handler(request);
  });
  servers.push(server);
  return server;
}

function harness(origin: string | null) {
  const api = injectLibraryEndpoints(createTeraApi({ timeoutMs: 300 }));
  const store = configureStore({
    reducer: { service: serviceReducer, [api.reducerPath]: api.reducer },
    middleware: (getDefaultMiddleware) => getDefaultMiddleware().concat(api.middleware),
  });
  store.dispatch(statusReceived({ ...SNAPSHOT, origin }));
  return { api, store };
}

function item(index: number): SampleListItem {
  return {
    analysis: { analysis_version: "0".repeat(64), state: "current" },
    audio: { channels: 1, duration_ms: 500, frame_count: 24000, sample_rate_hz: 48000 },
    file_name: `kick-${index}.wav`,
    file_status: "present",
    role: "kick",
    sample_id: `sha256:${String(index).padStart(64, "0")}`,
  };
}

function pageBody(
  items: SampleListItem[],
  query: { roles: string[]; text: string | null },
  page: Partial<{ has_more: boolean; next_cursor: string | null; limit: number }> = {},
): StubReply {
  return {
    body: {
      api_schema: "1.0",
      items,
      page: { count: items.length, has_more: false, limit: 50, next_cursor: null, ...page },
      query,
    },
  };
}

async function settle(): Promise<void> {
  await new Promise((resolve) => setTimeout(resolve, 60));
}

describe("the samples list request", () => {
  it("sends exactly the documented query, once", async () => {
    const server = await stub(() => pageBody([item(1)], { roles: ["kick", "bass"], text: "kick" }));
    const { api, store } = harness(server.origin);
    await store.dispatch(
      api.endpoints.getSamples.initiate({
        roles: ["kick", "bass"],
        text: "kick",
        limit: 50,
        cursor: null,
      }),
    );
    expect(server.requests).toHaveLength(1);
    expect(server.requests[0].method).toBe("GET");
    expect(server.requests[0].url).toBe("/library/samples?role=kick&role=bass&q=kick&limit=50");
  });

  it("sends no q for an empty text and no role for an empty filter", async () => {
    const server = await stub(() => pageBody([], { roles: [], text: null }));
    const { api, store } = harness(server.origin);
    await store.dispatch(
      api.endpoints.getSamples.initiate({ roles: [], text: null, limit: 25, cursor: null }),
    );
    expect(server.requests[0].url).toBe("/library/samples?limit=25");
  });

  it("sends the cursor only when one is set", async () => {
    const server = await stub(() => pageBody([item(1)], { roles: [], text: null }));
    const { api, store } = harness(server.origin);
    await store.dispatch(
      api.endpoints.getSamples.initiate({ roles: [], text: null, limit: 50, cursor: "cursor-a" }),
    );
    expect(server.requests[0].url).toBe("/library/samples?limit=50&cursor=cursor-a");
  });
});

describe("the sample detail request", () => {
  it("builds the path verbatim, without percent-encoding the identity", async () => {
    const server = await stub(() => ({
      body: {
        api_schema: "1.0",
        sample: { ...item(1), sample_id: SAMPLE_ID, analysis: { analysis_version: null, state: "absent" } },
      },
    }));
    const { api, store } = harness(server.origin);
    await store.dispatch(api.endpoints.getSample.initiate(SAMPLE_ID));
    expect(server.requests[0].method).toBe("GET");
    expect(server.requests[0].url).toBe(`/library/samples/${SAMPLE_ID}`);
  });

  it("answers a well-formed unknown id with the documented 404 envelope", async () => {
    const server = await stub(() => ({
      status: 404,
      body: {
        api_schema: "1.0",
        error: { code: "unknown_sample", message: "No stored sample has this id.", details: { sample_id: SAMPLE_ID } },
      },
    }));
    const { api, store } = harness(server.origin);
    const answer = await store.dispatch(api.endpoints.getSample.initiate(SAMPLE_ID));
    const failure = toLibraryError(answer.error);
    expect(failure).toEqual({
      kind: "api",
      code: "unknown_sample",
      status: 404,
      details: { sample_id: SAMPLE_ID },
    });
  });
});

describe("the import endpoints", () => {
  it("starts a run with the two-field body and the json media type", async () => {
    const server = await stub(() => ({
      status: 202,
      body: {
        api_schema: "1.0",
        import: {
          analysis_version: "0".repeat(64),
          phase: "scanning",
          role: "kick",
          run_id: RUN_ID,
          started_at: "2026-09-20T05:21:08Z",
          state: "running",
          status_path: `/imports/${RUN_ID}`,
        },
      },
    }));
    const { api, store } = harness(server.origin);
    const answer = await store.dispatch(
      api.endpoints.startImport.initiate({ root: "C:\\Samples\\Kicks", role: "kick" }),
    );
    expect(server.requests[0].method).toBe("POST");
    expect(server.requests[0].url).toBe("/imports");
    expect(server.requests[0].headers["content-type"]).toBe("application/json");
    expect(JSON.parse(server.requests[0].body)).toEqual({ root: "C:\\Samples\\Kicks", role: "kick" });
    expect(answer.data?.import.run_id).toBe(RUN_ID);
  });

  it("reads one run by its opaque id", async () => {
    const server = await stub(() => ({
      body: { api_schema: "1.0", import: { run_id: RUN_ID, state: "running", phase: "analyzing" } },
    }));
    const { api, store } = harness(server.origin);
    await store.dispatch(api.endpoints.getImport.initiate(RUN_ID));
    expect(server.requests[0].method).toBe("GET");
    expect(server.requests[0].url).toBe(`/imports/${RUN_ID}`);
  });

  it("cancels and retries with an empty json object body", async () => {
    const server = await stub((request) =>
      request.url.endsWith("/cancel")
        ? {
            body: {
              api_schema: "1.0",
              import: { cancel_requested: true, run_id: RUN_ID, state: "running", status_path: `/imports/${RUN_ID}` },
            },
          }
        : {
            status: 202,
            body: { api_schema: "1.0", import: { retried: 0, run_id: RUN_ID, status_path: `/imports/${RUN_ID}` } },
          },
    );
    const { api, store } = harness(server.origin);
    await store.dispatch(api.endpoints.cancelImport.initiate(RUN_ID));
    await store.dispatch(api.endpoints.retryImport.initiate(RUN_ID));
    expect(server.requests[0].url).toBe(`/imports/${RUN_ID}/cancel`);
    expect(server.requests[1].url).toBe(`/imports/${RUN_ID}/retry`);
    for (const request of server.requests) {
      expect(request.method).toBe("POST");
      expect(request.headers["content-type"]).toBe("application/json");
      expect(JSON.parse(request.body)).toEqual({});
    }
  });

  it("adopts the run id a 409 offers instead of failing", async () => {
    const server = await stub(() => ({
      status: 409,
      body: {
        api_schema: "1.0",
        error: {
          code: "import_already_running",
          message: "An import is already running.",
          details: { run_id: RUN_ID },
        },
      },
    }));
    const { api, store } = harness(server.origin);
    const answer = await store.dispatch(
      api.endpoints.startImport.initiate({ root: "C:\\Samples\\Kicks", role: "kick" }),
    );
    const failure = toLibraryError(answer.error);
    expect(failure.kind === "api" ? failure.code : null).toBe("import_already_running");
    expect(failure.kind === "api" ? failure.details["run_id"] : null).toBe(RUN_ID);
  });

  it("surfaces the two 400 refusals and the degraded 503", async () => {
    for (const [status, code, details] of [
      [400, "invalid_root", {}],
      [400, "invalid_role", { field: "role" }],
      [503, "database_unavailable", {}],
    ] as const) {
      const server = await stub(() => ({
        status,
        body: { api_schema: "1.0", error: { code, message: "refused", details } },
      }));
      const { api, store } = harness(server.origin);
      const answer = await store.dispatch(
        api.endpoints.startImport.initiate({ root: "C:\\Samples\\Kicks", role: "kick" }),
      );
      const failure = toLibraryError(answer.error);
      expect(failure.kind === "api" ? failure.code : null).toBe(code);
      expect(failure.kind === "api" ? failure.status : null).toBe(status);
      await server.close();
      servers.pop();
    }
  });
});

describe("transport failures", () => {
  it("reports a refused connection when nothing is listening", async () => {
    const server = await stub(() => pageBody([], { roles: [], text: null }));
    const origin = server.origin;
    await server.close();
    servers.pop();
    const { api, store } = harness(origin);
    const answer = await store.dispatch(
      api.endpoints.getSamples.initiate({ roles: [], text: null, limit: 50, cursor: null }),
    );
    expect(toLibraryError(answer.error)).toEqual({ kind: "transport", reason: "connection_refused" });
  });

  it("never treats a non-JSON body as a success", async () => {
    const server = await stub(() => ({ status: 200, raw: "not json at all" }));
    const { api, store } = harness(server.origin);
    const answer = await store.dispatch(
      api.endpoints.getSamples.initiate({ roles: [], text: null, limit: 50, cursor: null }),
    );
    expect(toLibraryError(answer.error)).toEqual({ kind: "transport", reason: "unparseable" });
  });

  it("issues no request while the origin is unknown", async () => {
    const server = await stub(() => pageBody([], { roles: [], text: null }));
    const { api, store } = harness(null);
    await store.dispatch(
      api.endpoints.getSamples.initiate({ roles: [], text: null, limit: 50, cursor: null }),
    );
    expect(server.requests).toHaveLength(0);
  });
});

describe("the tags", () => {
  it("refetch a subscribed list exactly once when an import starts", async () => {
    const server = await stub((request) =>
      request.method === "POST"
        ? {
            status: 202,
            body: {
              api_schema: "1.0",
              import: {
                analysis_version: "0".repeat(64),
                phase: "scanning",
                role: "kick",
                run_id: RUN_ID,
                started_at: "2026-09-20T05:21:08Z",
                state: "running",
                status_path: `/imports/${RUN_ID}`,
              },
            },
          }
        : pageBody([item(1)], { roles: [], text: null }),
    );
    const { api, store } = harness(server.origin);
    const args: SampleQueryArgs = { roles: [], text: null, limit: 50, cursor: null };
    const subscription = store.dispatch(api.endpoints.getSamples.initiate(args));
    await subscription;
    expect(paths.filter((path) => path.startsWith("/library/samples"))).toHaveLength(1);

    await store.dispatch(api.endpoints.startImport.initiate({ root: "C:\\Samples\\Kicks", role: "kick" }));
    await settle();
    expect(paths.filter((path) => path.startsWith("/library/samples"))).toHaveLength(2);
    subscription.unsubscribe();
  });
});

describe("superseded requests", () => {
  it("never let a late response replace a newer one", async () => {
    const server = await stub((request) =>
      request.url.includes("q=first")
        ? { ...pageBody([item(1)], { roles: [], text: "first" }), delayMs: 150 }
        : pageBody([item(2)], { roles: [], text: "second" }),
    );
    const { api, store } = harness(server.origin);
    const firstArgs: SampleQueryArgs = { roles: [], text: "first", limit: 50, cursor: null };
    const secondArgs: SampleQueryArgs = { roles: [], text: "second", limit: 50, cursor: null };
    const first = store.dispatch(api.endpoints.getSamples.initiate(firstArgs));
    const second = store.dispatch(api.endpoints.getSamples.initiate(secondArgs));
    await second;
    await first;

    const current = api.endpoints.getSamples.select(secondArgs)(store.getState());
    const superseded = api.endpoints.getSamples.select(firstArgs)(store.getState());
    expect(current.data?.query.text).toBe("second");
    expect(current.data?.items[0]?.file_name).toBe("kick-2.wav");
    // The old page exists only under its own cache key.
    expect(superseded.data?.query.text).toBe("first");
    expect(superseded.data?.items[0]?.file_name).toBe("kick-1.wav");
  });
});
