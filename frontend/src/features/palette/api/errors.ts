/**
 * The closed error vocabulary of the six routes this feature calls (issue #31).
 *
 * Every code here is one #27 can answer with on those routes, and every sentence
 * is this client's own: #27's `message` is never shown as the only explanation.
 */

export const LIBRARY_ERROR_CODES = [
  "invalid_root",
  "invalid_role",
  "invalid_query",
  "invalid_page_size",
  "unknown_query_parameter",
  "invalid_cursor",
  "invalid_sample_id",
  "unknown_sample",
  "import_already_running",
  "import_not_live",
  "unknown_import",
  "database_unavailable",
  "server_busy",
  "unsupported_media_type",
  "request_too_large",
  "internal_error",
] as const;
export type LibraryErrorCode = (typeof LIBRARY_ERROR_CODES)[number];

export const TRANSPORT_REASONS = [
  "connection_refused",
  "timeout",
  "unparseable",
  "unavailable",
] as const;
export type TransportReason = (typeof TRANSPORT_REASONS)[number];

export type LibraryError =
  | { kind: "api"; code: LibraryErrorCode; status: number; details: Record<string, unknown> }
  | { kind: "transport"; reason: TransportReason };

/** The longest recovery sentence the panel may render. */
export const RECOVERY_MAX_LENGTH = 200;

/** One actionable sentence per code and per transport reason. */
export const LIBRARY_ERROR_RECOVERY: Record<LibraryErrorCode | TransportReason, string> = {
  invalid_root:
    "Choose a folder on a local drive. A UNC path or a mapped network drive cannot be imported.",
  invalid_role: "Pick one of the listed roles; the service accepts no other.",
  invalid_query: "Shorten the search text: the service accepts 1 to 200 characters.",
  invalid_page_size: "Pick one of the listed page sizes; the service accepts 1 to 200.",
  unknown_query_parameter: "Reload the window: this client sent a parameter the service does not accept.",
  invalid_cursor: "The page position was refused. The list returns to the first page.",
  invalid_sample_id: "Reload the window: the sample identity it holds is not one the service issues.",
  unknown_sample: "This sample is no longer in the library. The selection was cleared.",
  import_already_running: "An import is already running in this library; its progress is shown here.",
  import_not_live: "The run already finished, so cancelling it would write nothing. Its result is shown.",
  unknown_import: "The service has no run with this id. Start a new import.",
  database_unavailable:
    "The library database is unavailable. See the service panel above; the library stays readable.",
  server_busy: "The service is at its request limit. Retry in a moment.",
  unsupported_media_type: "Reload the window: this client sent a body the service does not accept.",
  request_too_large: "The request was larger than the service accepts. Reload the window.",
  internal_error: "The service failed to complete the request. Retry; nothing partial was written.",
  connection_refused:
    "Nothing is listening on the service port. Start the service, then Retry.",
  timeout: "The service did not answer in time. It may be busy; Retry.",
  unparseable: "The service answered with something this client cannot read. Retry.",
  unavailable: "The service origin is not known yet. See the service panel above.",
};

/** One generic sentence for a code this client does not know. */
export const GENERIC_RECOVERY =
  "The service refused the request for a reason this window does not know. Retry.";

/** The sentence for one error, generic when the code is not in the closed table. */
export function recoveryFor(error: LibraryError): string {
  if (error.kind === "transport") {
    return LIBRARY_ERROR_RECOVERY[error.reason];
  }
  return LIBRARY_ERROR_RECOVERY[error.code] ?? GENERIC_RECOVERY;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

/**
 * Map one RTK Query failure to the closed vocabulary.
 *
 * A body that carries #27's envelope contributes its code and details; anything
 * else -- a refused connection, a timeout, a body that is not the documented
 * JSON -- becomes a transport reason, so a non-JSON body is never a success.
 */
export function toLibraryError(error: unknown): LibraryError {
  const status = isRecord(error) ? error.status : undefined;
  if (typeof status === "number" && isRecord(error)) {
    const data = error.data;
    const envelope = isRecord(data) ? data.error : undefined;
    const code = isRecord(envelope) ? envelope.code : undefined;
    const details = isRecord(envelope) && isRecord(envelope.details) ? envelope.details : {};
    const known = (LIBRARY_ERROR_CODES as readonly string[]).includes(String(code));
    return {
      kind: "api",
      code: known ? (code as LibraryErrorCode) : "internal_error",
      status,
      details,
    };
  }
  if (status === "TIMEOUT_ERROR") {
    return { kind: "transport", reason: "timeout" };
  }
  if (status === "PARSING_ERROR") {
    return { kind: "transport", reason: "unparseable" };
  }
  if (status === "FETCH_ERROR") {
    return { kind: "transport", reason: "connection_refused" };
  }
  return { kind: "transport", reason: "unavailable" };
}
