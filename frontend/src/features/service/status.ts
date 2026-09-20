/**
 * The shell's closed service vocabulary (issue #30).
 *
 * Every phase and reason the shell can show is declared here and nowhere else.
 * The host's snapshot is the only source of a phase or a reason; this module
 * derives what the panel renders from that snapshot plus the latest health
 * observation, and owns the one recovery sentence per reason.
 */

export type ServiceMode = "owned" | "attached";

export type ServicePhase =
  | "starting"
  | "running"
  | "degraded"
  | "unavailable"
  | "failed"
  | "stopping";

/** The reasons that resolve to `unavailable`. */
export const UNAVAILABLE_REASONS = [
  "python_not_found",
  "dev_data_dir_unavailable",
  "invalid_configuration",
  "connection_refused",
  "stopped_by_user",
] as const;

/** The reasons that resolve to `failed`. */
export const FAILED_REASONS = [
  "spawn_failed",
  "start_timeout",
  "port_in_use",
  "service_exited",
  "service_lost",
  "health_timeout",
  "health_error",
] as const;

export type UnavailableReason = (typeof UNAVAILABLE_REASONS)[number];
export type FailedReason = (typeof FAILED_REASONS)[number];
export type ServiceReason = UnavailableReason | FailedReason;

export const MODES: readonly ServiceMode[] = ["owned", "attached"] as const;

export const PHASE_BY_REASON = {
  ...Object.fromEntries(UNAVAILABLE_REASONS.map((reason) => [reason, "unavailable"])),
  ...Object.fromEntries(FAILED_REASONS.map((reason) => [reason, "failed"])),
} as Record<ServiceReason, "unavailable" | "failed">;

/** The longest recovery sentence the panel may render. */
export const RECOVERY_MAX_LENGTH = 200;

/** One actionable sentence per reason: what the developer does next. */
export const REASON_RECOVERY: Record<ServiceReason, string> = {
  python_not_found:
    "Run uv sync in the repository root, then Retry. Set TERA_PYTHON when the interpreter lives somewhere else.",
  dev_data_dir_unavailable:
    "Set TERA_DEV_DATA_DIR to a writable absolute directory; %LOCALAPPDATA% is missing and the shell will not guess one.",
  invalid_configuration:
    "Correct or unset the TERA_* variable named above; a relative, malformed or out-of-range value is refused before the service starts.",
  connection_refused:
    "Nothing is listening at the recorded origin. Start the service and Retry, or unset TERA_SERVICE_URL so this window owns it.",
  stopped_by_user:
    "This window stopped the service. Start service launches it again; the database and the library are untouched.",
  spawn_failed:
    "The interpreter could not be started. Check TERA_PYTHON points at a real python.exe, then Retry.",
  start_timeout:
    "The service never reported listening. Run the printed command by hand to read its output, then Retry.",
  port_in_use:
    "Free the port named above, or unset TERA_SERVICE_PORT so the shell asks for an ephemeral port, then Retry.",
  service_exited:
    "The service exited on its own; its exit code and last diagnostic line are shown. Retry starts exactly one new service.",
  service_lost:
    "The attached service stopped answering. Start it again and Retry; this window never owns an attached service.",
  health_timeout:
    "GET /health did not answer in time. The service may be busy; Retry re-probes it.",
  health_error:
    "GET /health answered with an error status or an unreadable body; the status is shown above. Retry re-probes it.",
};

/** The snapshot the Tauri host emits on `service:status`. */
export interface ServiceSnapshot {
  mode: ServiceMode;
  phase: ServicePhase;
  reason: ServiceReason | null;
  origin: string | null;
  port: number | null;
  pid: number | null;
  exit_code: number | null;
  python_path: string | null;
  database_path: string | null;
  data_dir: string | null;
  port_file: string | null;
  health_seen: boolean;
  started_at: string | null;
  diagnostic: string | null;
}

/** The fields of #27's `GET /health` body the panel shows, and no others. */
export interface HealthBody {
  api_schema: string;
  service: string;
  service_version: string;
  state: "ok" | "degraded";
  pid: number | null;
  database: {
    state: string | null;
    code: string | null;
    schema_version: number | null;
  } | null;
  library: { samples: number | null } | null;
  import: { state: string | null } | null;
}

/** What the shell currently knows about `GET /health`. */
export type HealthObservation =
  | { kind: "unknown" }
  | { kind: "ok"; body: HealthBody }
  | { kind: "failed"; reason: ServiceReason; status?: number; diagnostic?: string };

export interface DerivedStatus {
  phase: ServicePhase;
  reason: ServiceReason | null;
}

/**
 * The one precedence rule the panel follows.
 *
 * A stop in flight is `stopping`. A recorded failed or unavailable fact, or a
 * failed health result, wins over stale health data. `running` and `degraded`
 * need a successful `GET /health` plus a live child or attached mode. Anything
 * else — a spawn in flight, or a live child with no health success yet — is
 * `starting`.
 */
export function deriveServiceStatus(
  snapshot: ServiceSnapshot | null,
  health: HealthObservation,
): DerivedStatus {
  if (snapshot === null) {
    return { phase: "starting", reason: null };
  }
  if (snapshot.phase === "stopping") {
    return { phase: "stopping", reason: snapshot.reason };
  }
  if (snapshot.phase === "failed" || snapshot.phase === "unavailable") {
    return snapshot.reason === null
      ? { phase: snapshot.phase, reason: null }
      : { phase: PHASE_BY_REASON[snapshot.reason], reason: snapshot.reason };
  }
  if (health.kind === "failed") {
    return { phase: PHASE_BY_REASON[health.reason], reason: health.reason };
  }
  const live = snapshot.mode === "attached" || snapshot.pid !== null;
  if (health.kind === "ok" && live) {
    return {
      phase: health.body.state === "degraded" ? "degraded" : "running",
      reason: null,
    };
  }
  return { phase: "starting", reason: null };
}

/** The subset of an RTK Query result this module reads. */
export interface HealthResult {
  isUninitialized: boolean;
  isLoading: boolean;
  isError: boolean;
  error?: unknown;
  data?: HealthBody;
}

/**
 * The reason for one failed `GET /health`, from RTK Query's error shape.
 *
 * A refusal after at least one healthy poll is a lost service; a refusal that
 * never succeeded is a refused connection. Both keep the same recovery story.
 */
export function healthFailureReason(
  error: unknown,
  everHealthy: boolean,
): { reason: ServiceReason; status?: number } {
  const status = (error as { status?: unknown } | null | undefined)?.status;
  if (status === "TIMEOUT_ERROR") {
    return { reason: "health_timeout" };
  }
  if (status === "FETCH_ERROR") {
    return { reason: everHealthy ? "service_lost" : "connection_refused" };
  }
  if (typeof status === "number") {
    return { reason: "health_error", status };
  }
  if (status === "PARSING_ERROR") {
    const original = (error as { originalStatus?: unknown }).originalStatus;
    return typeof original === "number"
      ? { reason: "health_error", status: original }
      : { reason: "health_error" };
  }
  return { reason: "health_error" };
}

/** One RTK Query result plus the client's poll history as an observation. */
export function observeHealth(result: HealthResult, everHealthy: boolean): HealthObservation {
  if (result.isError && result.error !== undefined && result.error !== null) {
    const failure = healthFailureReason(result.error, everHealthy);
    return { kind: "failed", reason: failure.reason, status: failure.status };
  }
  if (result.data !== undefined) {
    return { kind: "ok", body: result.data };
  }
  return { kind: "unknown" };
}
