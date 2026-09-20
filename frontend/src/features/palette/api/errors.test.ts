import { describe, expect, it } from "vitest";

import {
  GENERIC_RECOVERY,
  LIBRARY_ERROR_CODES,
  LIBRARY_ERROR_RECOVERY,
  RECOVERY_MAX_LENGTH,
  TRANSPORT_REASONS,
  recoveryFor,
  toLibraryError,
  type LibraryError,
} from "./errors";

describe("the closed error vocabulary", () => {
  it("covers every code the six routes can answer with", () => {
    expect([...LIBRARY_ERROR_CODES].sort()).toEqual(
      [
        "database_unavailable",
        "import_already_running",
        "import_not_live",
        "internal_error",
        "invalid_cursor",
        "invalid_page_size",
        "invalid_query",
        "invalid_role",
        "invalid_root",
        "invalid_sample_id",
        "request_too_large",
        "server_busy",
        "unknown_import",
        "unknown_query_parameter",
        "unknown_sample",
        "unsupported_media_type",
      ].sort(),
    );
    expect(LIBRARY_ERROR_CODES.length).toBe(16);
  });

  it("closes the transport reasons", () => {
    expect([...TRANSPORT_REASONS]).toEqual([
      "connection_refused",
      "timeout",
      "unparseable",
      "unavailable",
    ]);
  });

  it("gives every code and reason one sentence inside the bound", () => {
    const declared = [...LIBRARY_ERROR_CODES, ...TRANSPORT_REASONS].sort();
    expect(Object.keys(LIBRARY_ERROR_RECOVERY).sort()).toEqual(declared);
    for (const sentence of Object.values(LIBRARY_ERROR_RECOVERY)) {
      expect(sentence.length).toBeGreaterThan(0);
      expect(sentence.length).toBeLessThanOrEqual(RECOVERY_MAX_LENGTH);
    }
    expect(GENERIC_RECOVERY.length).toBeGreaterThan(0);
    expect(GENERIC_RECOVERY.length).toBeLessThanOrEqual(RECOVERY_MAX_LENGTH);
  });
});

describe("toLibraryError", () => {
  it("reads code, status and details out of a #27 envelope", () => {
    const error = toLibraryError({
      status: 409,
      data: {
        api_schema: "1.0",
        error: { code: "import_already_running", message: "An import is already running.", details: { run_id: "run-1" } },
      },
    });
    expect(error).toEqual({
      kind: "api",
      code: "import_already_running",
      status: 409,
      details: { run_id: "run-1" },
    });
    expect(recoveryFor(error)).toContain("already running");
  });

  it("falls back to one generic sentence for a code it does not know", () => {
    const error = toLibraryError({
      status: 418,
      data: { api_schema: "1.0", error: { code: "not_a_code", message: "?", details: {} } },
    });
    expect(error.kind).toBe("api");
    expect(error.kind === "api" ? error.code : null).toBe("internal_error");
    expect(error.kind === "api" ? error.details : null).toEqual({});
  });

  it("maps a refused connection, a timeout, an unreadable body and an unknown origin", () => {
    expect(toLibraryError({ status: "FETCH_ERROR", error: "refused" })).toEqual({
      kind: "transport",
      reason: "connection_refused",
    });
    expect(toLibraryError({ status: "TIMEOUT_ERROR" })).toEqual({
      kind: "transport",
      reason: "timeout",
    });
    expect(toLibraryError({ status: "PARSING_ERROR", originalStatus: 200 })).toEqual({
      kind: "transport",
      reason: "unparseable",
    });
    expect(toLibraryError({ status: "CUSTOM_ERROR", error: "no origin" })).toEqual({
      kind: "transport",
      reason: "unavailable",
    });
    expect(toLibraryError(undefined)).toEqual({ kind: "transport", reason: "unavailable" });
  });

  it("never treats a body without the envelope as an api error", () => {
    const error: LibraryError = toLibraryError({ status: 503, data: "not the envelope" });
    expect(error.kind).toBe("api");
    expect(error.kind === "api" ? error.code : null).toBe("internal_error");
    expect(error.kind === "api" ? error.status : null).toBe(503);
  });
});
