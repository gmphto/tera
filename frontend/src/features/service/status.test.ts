import { describe, expect, it } from "vitest";

import {
  deriveServiceStatus,
  FAILED_REASONS,
  PHASE_BY_REASON,
  REASON_RECOVERY,
  RECOVERY_MAX_LENGTH,
  UNAVAILABLE_REASONS,
  type HealthBody,
  type HealthObservation,
  type ServiceReason,
  type ServiceSnapshot,
} from "./status";

const OK_BODY: HealthBody = {
  api_schema: "1.0",
  service: "tera-local-service",
  service_version: "0.1.0",
  state: "ok",
  pid: 1234,
  database: { state: "ok", code: null, schema_version: 5 },
  library: { samples: 0 },
  import: { state: "idle" },
};

const DEGRADED_BODY: HealthBody = {
  ...OK_BODY,
  state: "degraded",
  database: { state: "degraded", code: "database_corrupt", schema_version: null },
  library: null,
  import: null,
};

const HEALTHY: HealthObservation = { kind: "ok", body: OK_BODY };
const DEGRADED: HealthObservation = { kind: "ok", body: DEGRADED_BODY };
const UNKNOWN: HealthObservation = { kind: "unknown" };

function snapshot(overrides: Partial<ServiceSnapshot> = {}): ServiceSnapshot {
  return {
    mode: "owned",
    phase: "running",
    reason: null,
    origin: "http://127.0.0.1:7391",
    port: 7391,
    pid: 1234,
    exit_code: null,
    python_path: "C:/synthetic/.venv/Scripts/python.exe",
    database_path: "C:/synthetic/data/tera.sqlite3",
    data_dir: "C:/synthetic/data",
    port_file: "C:/synthetic/data/service-port.json",
    health_seen: true,
    started_at: "2026-09-20T05:21:08Z",
    diagnostic: null,
    ...overrides,
  };
}

describe("deriveServiceStatus", () => {
  const cases: Array<{
    name: string;
    snapshot: ServiceSnapshot | null;
    health: HealthObservation;
    phase: string;
    reason: ServiceReason | null;
  }> = [
    {
      name: "no snapshot yet is starting and never blank",
      snapshot: null,
      health: UNKNOWN,
      phase: "starting",
      reason: null,
    },
    {
      name: "a live child without a health success is starting",
      snapshot: snapshot({ phase: "starting", health_seen: false }),
      health: UNKNOWN,
      phase: "starting",
      reason: null,
    },
    {
      name: "an attached origin with no health yet is starting",
      snapshot: snapshot({ mode: "attached", phase: "starting", pid: null }),
      health: UNKNOWN,
      phase: "starting",
      reason: null,
    },
    {
      name: "a healthy poll with a live child is running",
      snapshot: snapshot(),
      health: HEALTHY,
      phase: "running",
      reason: null,
    },
    {
      name: "a degraded health body is degraded",
      snapshot: snapshot(),
      health: DEGRADED,
      phase: "degraded",
      reason: null,
    },
    {
      name: "a stop in flight is stopping",
      snapshot: snapshot({ phase: "stopping" }),
      health: HEALTHY,
      phase: "stopping",
      reason: null,
    },
    {
      name: "a recorded failure wins over stale health",
      snapshot: snapshot({ phase: "failed", reason: "service_exited", exit_code: 2 }),
      health: HEALTHY,
      phase: "failed",
      reason: "service_exited",
    },
    {
      name: "a recorded unavailability wins over stale health",
      snapshot: snapshot({ phase: "unavailable", reason: "python_not_found", pid: null }),
      health: HEALTHY,
      phase: "unavailable",
      reason: "python_not_found",
    },
    {
      name: "a failed health result is a failure while the child lives",
      snapshot: snapshot(),
      health: { kind: "failed", reason: "health_error", status: 500 },
      phase: "failed",
      reason: "health_error",
    },
    {
      name: "a refused first connection is unavailable",
      snapshot: snapshot({ pid: null, health_seen: false }),
      health: { kind: "failed", reason: "connection_refused" },
      phase: "unavailable",
      reason: "connection_refused",
    },
    {
      name: "a lost attached service is a failure",
      snapshot: snapshot({ mode: "attached", pid: null }),
      health: { kind: "failed", reason: "service_lost" },
      phase: "failed",
      reason: "service_lost",
    },
  ];

  for (const item of cases) {
    it(item.name, () => {
      const derived = deriveServiceStatus(item.snapshot, item.health);
      expect(derived.phase).toBe(item.phase);
      expect(derived.reason).toBe(item.reason);
    });
  }

  it("resolves every reason to its documented phase", () => {
    for (const reason of UNAVAILABLE_REASONS) {
      expect(PHASE_BY_REASON[reason]).toBe("unavailable");
      expect(
        deriveServiceStatus(snapshot({ phase: "unavailable", reason }), HEALTHY),
      ).toEqual({ phase: "unavailable", reason });
    }
    for (const reason of FAILED_REASONS) {
      expect(PHASE_BY_REASON[reason]).toBe("failed");
      expect(deriveServiceStatus(snapshot({ phase: "failed", reason }), HEALTHY)).toEqual({
        phase: "failed",
        reason,
      });
    }
  });

  it("never shows running for a reason alone", () => {
    const reasons: ServiceReason[] = [...UNAVAILABLE_REASONS, ...FAILED_REASONS];
    for (const reason of reasons) {
      const derived = deriveServiceStatus(
        snapshot({ phase: PHASE_BY_REASON[reason], reason }),
        HEALTHY,
      );
      expect(derived.phase).not.toBe("running");
      expect(derived.reason).toBe(reason);
    }
  });
});

describe("REASON_RECOVERY", () => {
  it("is total over the closed reason union", () => {
    const declared = [...UNAVAILABLE_REASONS, ...FAILED_REASONS].sort();
    expect(Object.keys(REASON_RECOVERY).sort()).toEqual(declared);
  });

  it("gives every reason one non-empty sentence inside the bound", () => {
    for (const sentence of Object.values(REASON_RECOVERY)) {
      expect(sentence.length).toBeGreaterThan(0);
      expect(sentence.length).toBeLessThanOrEqual(RECOVERY_MAX_LENGTH);
    }
  });

  it("names the fix for the three reasons the issue calls out", () => {
    expect(REASON_RECOVERY.python_not_found).toContain("uv sync");
    expect(REASON_RECOVERY.port_in_use).toContain("TERA_SERVICE_PORT");
    expect(REASON_RECOVERY.invalid_configuration).toContain("TERA_");
  });
});
