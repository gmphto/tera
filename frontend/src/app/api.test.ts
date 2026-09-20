import { configureStore } from "@reduxjs/toolkit";
import { afterEach, describe, expect, it } from "vitest";

import serviceReducer, { statusReceived } from "../features/service/serviceSlice";
import { deriveServiceStatus, observeHealth, type ServiceSnapshot } from "../features/service/status";
import { HANG, startHealthServer, type StubServer } from "../test/healthServer";
import { createTeraApi, HEALTH_TIMEOUT_MS, SERVICE_POLL_INTERVAL_MS } from "./api";

const OK_BODY = {
  api_schema: "1.0",
  service: "tera-local-service",
  service_version: "0.1.0",
  state: "ok",
  pid: 1234,
  database: { state: "ok", code: null, schema_version: 5, journal_mode: "wal" },
  import: { state: "idle", phase: null, run_id: null, started_at: null },
  library: { samples: 0, roots: 0, pending_analysis: 0, by_role: { kick: 0, bass: 0 } },
  limits: { max_page_size: 200 },
  roles: ["kick", "bass"],
  started_at: "2026-09-20T05:21:08Z",
};

const SNAPSHOT: ServiceSnapshot = {
  mode: "owned",
  phase: "starting",
  reason: null,
  origin: null,
  port: null,
  pid: 1234,
  exit_code: null,
  python_path: "C:/synthetic/.venv/Scripts/python.exe",
  database_path: "C:/synthetic/data/tera.sqlite3",
  data_dir: "C:/synthetic/data",
  port_file: "C:/synthetic/data/service-port.json",
  health_seen: false,
  started_at: "2026-09-20T05:21:08Z",
  diagnostic: null,
};

const SHORT_TIMEOUT_MS = 300;
const servers: StubServer[] = [];

afterEach(async () => {
  while (servers.length > 0) {
    const server = servers.pop();
    if (server !== undefined) {
      await server.close();
    }
  }
});

async function stub(respond: Parameters<typeof startHealthServer>[0]): Promise<StubServer> {
  const server = await startHealthServer(respond);
  servers.push(server);
  return server;
}

function harness(origin: string | null, timeoutMs = SHORT_TIMEOUT_MS) {
  const api = createTeraApi({ timeoutMs });
  const store = configureStore({
    reducer: { service: serviceReducer, [api.reducerPath]: api.reducer },
    middleware: (getDefaultMiddleware) => getDefaultMiddleware().concat(api.middleware),
  });
  store.dispatch(statusReceived({ ...SNAPSHOT, origin, port: origin === null ? null : 7391 }));
  const read = () => {
    const result = api.endpoints.getHealth.select()(store.getState());
    return deriveServiceStatus(store.getState().service.snapshot, observeHealth(result, false));
  };
  return { api, store, read };
}

describe("the health endpoint", () => {
  it("keeps the documented client timings", () => {
    expect(HEALTH_TIMEOUT_MS).toBe(5000);
    expect(SERVICE_POLL_INTERVAL_MS).toBe(5000);
  });

  it("issues no request while the origin is null", async () => {
    const server = await stub({ body: JSON.stringify(OK_BODY) });
    const { api, store } = harness(null, SHORT_TIMEOUT_MS);
    await store.dispatch(api.endpoints.getHealth.initiate());
    expect(server.requests).toEqual([]);
  });

  it("asks for exactly <origin>/health", async () => {
    const server = await stub({ body: JSON.stringify(OK_BODY) });
    const { api, store } = harness(server.origin);
    await store.dispatch(api.endpoints.getHealth.initiate());
    expect(server.requests).toEqual(["/health"]);
  });

  it("reaches running from a #27-shaped ok body", async () => {
    const server = await stub({ body: JSON.stringify(OK_BODY) });
    const { api, store, read } = harness(server.origin);
    await store.dispatch(api.endpoints.getHealth.initiate());
    expect(read()).toEqual({ phase: "running", reason: null });
  });

  it("reaches degraded from a degraded body", async () => {
    const body = { ...OK_BODY, state: "degraded", database: { ...OK_BODY.database, state: "degraded", code: "database_corrupt", schema_version: null }, library: null, import: null };
    const server = await stub({ body: JSON.stringify(body) });
    const { api, store, read } = harness(server.origin);
    await store.dispatch(api.endpoints.getHealth.initiate());
    expect(read()).toEqual({ phase: "degraded", reason: null });
  });

  it("reports a refused connection that never succeeded", async () => {
    const server = await stub({ body: "{}" });
    const origin = server.origin;
    await server.close();
    servers.pop();
    const { api, store, read } = harness(origin);
    await store.dispatch(api.endpoints.getHealth.initiate());
    expect(read()).toEqual({ phase: "unavailable", reason: "connection_refused" });
  });

  it("reports an error status as health_error with the status recorded", async () => {
    const server = await stub({ status: 500, body: JSON.stringify({}) });
    const { api, store } = harness(server.origin);
    const result = await store.dispatch(api.endpoints.getHealth.initiate());
    expect(observeHealth({ isUninitialized: false, isLoading: false, isError: true, error: result.error }, false))
      .toEqual({ kind: "failed", reason: "health_error", status: 500 });
  });

  it("reports an unreadable body as health_error with the status recorded", async () => {
    const server = await stub({ status: 200, body: "this is not JSON" });
    const { api, store } = harness(server.origin);
    const result = await store.dispatch(api.endpoints.getHealth.initiate());
    const observation = observeHealth(
      { isUninitialized: false, isLoading: false, isError: true, error: result.error },
      false,
    );
    expect(observation.kind).toBe("failed");
    expect(observation.kind === "failed" ? observation.reason : null).toBe("health_error");
    expect(observation.kind === "failed" ? observation.status : null).toBe(200);
  });

  it("reports a server that never answers as health_timeout", async () => {
    const server = await stub(() => HANG);
    const { api, store, read } = harness(server.origin);
    await store.dispatch(api.endpoints.getHealth.initiate());
    expect(read()).toEqual({ phase: "failed", reason: "health_timeout" });
  });

  it("reports a refusal after a healthy poll as a lost service", async () => {
    const closed = await stub({ body: JSON.stringify(OK_BODY) });
    const origin = closed.origin;
    await closed.close();
    servers.pop();
    const { api, store } = harness(origin);
    const result = await store.dispatch(api.endpoints.getHealth.initiate());
    const observation = observeHealth(
      { isUninitialized: false, isLoading: false, isError: true, error: result.error },
      true,
    );
    expect(observation).toEqual({ kind: "failed", reason: "service_lost" });
  });
});
