import { configureStore } from "@reduxjs/toolkit";
import { afterEach, describe, expect, it } from "vitest";

import { createTeraApi } from "../../../app/api";
import serviceReducer, { statusReceived } from "../../service/serviceSlice";
import type { ServiceSnapshot } from "../../service/status";
import { paletteLoaded, paletteReducer } from "../state/paletteSlice";
import {
  errorBody,
  paletteBody,
  startPaletteServer,
  type PaletteStub,
} from "../../../test/paletteServer";
import {
  REVISION_CONFLICT,
  injectPaletteEndpoints,
  sendPaletteMutation,
} from "./paletteApi";
import { CLIENT_UNKNOWN_REASON, toContextRequest } from "../state/contextDraft";

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

const KICK = `sha256:${"a".repeat(64)}`;
const OTHER = `sha256:${"b".repeat(64)}`;
const servers: PaletteStub[] = [];

afterEach(async () => {
  while (servers.length > 0) {
    const server = servers.pop();
    if (server !== undefined) {
      await server.close();
    }
  }
});

async function stub(handler: Parameters<typeof startPaletteServer>[0]): Promise<PaletteStub> {
  const server = await startPaletteServer(handler);
  servers.push(server);
  return server;
}

function harness(origin: string | null) {
  const api = injectPaletteEndpoints(createTeraApi({ timeoutMs: 300 }));
  const store = configureStore({
    reducer: { service: serviceReducer, palette: paletteReducer, [api.reducerPath]: api.reducer },
    middleware: (getDefaultMiddleware) => getDefaultMiddleware().concat(api.middleware),
  });
  store.dispatch(statusReceived({ ...SNAPSHOT, origin }));
  // The endpoints dispatch their answers through the revision guard, which
  // refuses anything for a palette this client has not loaded.
  store.dispatch(paletteLoaded(paletteBody(3, KICK).palette));
  return { api, store };
}

describe("GET /palette", () => {
  it("returns the projection and caches it under the Palette tag", async () => {
    const server = await stub(() => ({ body: paletteBody(2, KICK) }));
    const { api, store } = harness(server.origin);
    await store.dispatch(api.endpoints.getPalette.initiate(undefined, { forceRefetch: true }));
    expect(server.path()).toEqual(["GET /palette"]);
    const cached = api.endpoints.getPalette.select()(store.getState());
    expect(cached.data?.palette.revision).toBe(2);

    // The same subscription is served from the cache: no second request.
    await store.dispatch(api.endpoints.getPalette.initiate());
    expect(server.path()).toEqual(["GET /palette"]);
  });

  it("names a project when one is asked for", async () => {
    const server = await stub(() => ({ body: paletteBody(0, null) }));
    const { api, store } = harness(server.origin);
    await store.dispatch(api.endpoints.getPalette.initiate("project-001"));
    expect(server.path()).toEqual(["GET /palette?project_id=project-001"]);
  });
});

describe("PUT /palette/items/{slot}", () => {
  it("sends the documented body and applies the returned palette", async () => {
    const server = await stub(() => ({ body: { ...paletteBody(4, KICK), changed: true } }));
    const { api, store } = harness(server.origin);
    const answer = await store
      .dispatch(api.endpoints.setPaletteItem.initiate({
        slot: "kick", paletteId: "palette-001", sampleId: KICK, revision: 3,
      }))
      .unwrap();
    expect(server.requests[0].method).toBe("PUT");
    expect(server.requests[0].url).toBe("/palette/items/kick");
    expect(JSON.parse(server.requests[0].body)).toEqual({
      palette_id: "palette-001", sample_id: KICK, expected_revision: 3,
    });
    expect(answer.palette.revision).toBe(4);
  });
});

describe("a revision conflict", () => {
  it("re-reads and retries exactly once, in that order", async () => {
    let puts = 0;
    const server = await stub((request) => {
      if (request.method === "GET") {
        return { body: paletteBody(puts === 0 ? 3 : 5, KICK) };
      }
      puts += 1;
      return puts === 1
        ? { status: 409, body: errorBody(REVISION_CONFLICT, { expected_revision: 3, current_revision: 5 }) }
        : { body: { ...paletteBody(6, OTHER), changed: true } };
    });
    const { api, store } = harness(server.origin);

    const outcome = await sendPaletteMutation({
      send: (revision) =>
        store.dispatch(api.endpoints.setPaletteItem.initiate({
          slot: "kick", paletteId: "palette-001", sampleId: OTHER, revision,
        })).unwrap(),
      revision: 3,
      reread: async () => {
        const fresh = await store.dispatch(api.endpoints.getPalette.initiate(undefined, { forceRefetch: true }));
        return fresh.data === undefined ? null : fresh.data.palette.revision;
      },
      mayRetry: () => true,
    });

    expect(outcome).toEqual({ applied: true, conflict: false, failure: null });
    expect(server.path()).toEqual([
      "PUT /palette/items/kick",
      "GET /palette",
      "PUT /palette/items/kick",
    ]);
    const bodies = server.requests
      .filter((request) => request.method === "PUT")
      .map((request) => JSON.parse(request.body).expected_revision);
    expect(bodies).toEqual([3, 5]);
  });

  it("stops after the retry and reports the conflict", async () => {
    const server = await stub((request) =>
      request.method === "GET"
        ? { body: paletteBody(5, KICK) }
        : { status: 409, body: errorBody(REVISION_CONFLICT, { expected_revision: 3, current_revision: 5 }) },
    );
    const { api, store } = harness(server.origin);
    const outcome = await sendPaletteMutation({
      send: (revision) =>
        store.dispatch(api.endpoints.setPaletteItem.initiate({
          slot: "kick", paletteId: "palette-001", sampleId: KICK, revision,
        })).unwrap(),
      revision: 3,
      reread: async () => 5,
      mayRetry: () => true,
    });
    expect(outcome).toEqual({ applied: false, conflict: true, failure: null });
    expect(server.requests.filter((request) => request.method === "PUT")).toHaveLength(2);
  });

  it("does not retry when the caller no longer owns the newest action", async () => {
    const server = await stub(() => ({
      status: 409, body: errorBody(REVISION_CONFLICT, { expected_revision: 3, current_revision: 5 }),
    }));
    const { api, store } = harness(server.origin);
    let rereads = 0;
    const outcome = await sendPaletteMutation({
      send: (revision) =>
        store.dispatch(api.endpoints.setPaletteItem.initiate({
          slot: "kick", paletteId: "palette-001", sampleId: KICK, revision,
        })).unwrap(),
      revision: 3,
      reread: async () => {
        rereads += 1;
        return 5;
      },
      mayRetry: () => false,
    });
    expect(outcome.conflict).toBe(true);
    expect(rereads).toBe(1);
    expect(server.requests.filter((request) => request.method === "PUT")).toHaveLength(1);
  });
});

describe("PUT /palette/context", () => {
  it("sends the three state objects exactly as the draft built them", async () => {
    const server = await stub(() => ({ body: { ...paletteBody(4, KICK), changed: true } }));
    const { api, store } = harness(server.origin);
    const request = toContextRequest({
      tempo: { state: "known", value: "140" },
      key: { state: "known", value: "C major" },
      genre: { state: "unknown", value: "" },
    });
    expect(request.ok).toBe(true);
    const value = request.ok ? request.value : null;
    await store.dispatch(api.endpoints.setPaletteContext.initiate({
      paletteId: "palette-001", revision: 3, ...value!,
    })).unwrap();
    expect(server.requests[0].url).toBe("/palette/context");
    expect(JSON.parse(server.requests[0].body)).toEqual({
      palette_id: "palette-001",
      expected_revision: 3,
      tempo: { state: "known", bpm: 140 },
      key: { state: "known", tonic: "C", mode: "major" },
      genre: { state: "unknown", reason: CLIENT_UNKNOWN_REASON },
    });
  });
});

describe("POST /projects", () => {
  it("creates the first palette and sends only the name it was given", async () => {
    const server = await stub(() => ({ status: 201, body: paletteBody(0, null) }));
    const { api, store } = harness(server.origin);
    const answer = await store.dispatch(api.endpoints.createProject.initiate({ name: "Track A" })).unwrap();
    expect(server.requests[0].url).toBe("/projects");
    expect(JSON.parse(server.requests[0].body)).toEqual({ name: "Track A" });
    expect(answer.palette.revision).toBe(0);
  });

  it("passes a palette name through when one is given", async () => {
    const server = await stub(() => ({ status: 201, body: paletteBody(0, null) }));
    const { api, store } = harness(server.origin);
    await store.dispatch(
      api.endpoints.createProject.initiate({ name: "Track A", paletteName: "Main" }),
    ).unwrap();
    expect(JSON.parse(server.requests[0].body)).toEqual({ name: "Track A", palette_name: "Main" });
  });

  it("answers project_exists as a refusal the caller can turn into a reload", async () => {
    const server = await stub(() => ({
      status: 409, body: errorBody("project_exists", { project_id: "project-001" }),
    }));
    const { api, store } = harness(server.origin);
    const answer = await store.dispatch(api.endpoints.createProject.initiate({ name: "Track A" }));
    expect(answer.error).toBeDefined();
    expect(server.requests).toHaveLength(1);
  });
});
