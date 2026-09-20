import { useRef } from "react";

import { SERVICE_POLL_INTERVAL_MS, useGetHealthQuery } from "../../app/api";
import { useAppSelector } from "../../app/hooks";
import {
  deriveServiceStatus,
  observeHealth,
  REASON_RECOVERY,
  type DerivedStatus,
  type HealthBody,
  type ServiceSnapshot,
} from "./status";

export interface ServiceView {
  snapshot: ServiceSnapshot | null;
  status: DerivedStatus;
  health: HealthBody | null;
}

/**
 * The one place the window combines the host's snapshot with `GET /health`.
 *
 * The query is skipped until the origin is known, so no request is issued
 * before the service has a port.
 */
export function useServiceStatus(): ServiceView {
  const snapshot = useAppSelector((state) => state.service.snapshot);
  const origin = useAppSelector((state) => state.service.origin);
  const everHealthy = useRef(false);
  const result = useGetHealthQuery(undefined, {
    skip: origin === null,
    pollingInterval: SERVICE_POLL_INTERVAL_MS,
  });
  if (result.data !== undefined) {
    everHealthy.current = true;
  }
  const observation = observeHealth(result, everHealthy.current);
  return {
    snapshot,
    status: deriveServiceStatus(snapshot, observation),
    health: observation.kind === "ok" ? observation.body : null,
  };
}

function Fact({ name, value }: { name: string; value: string | number | null | undefined }) {
  return (
    <>
      <dt>{name}</dt>
      <dd>{value === null || value === undefined ? "—" : String(value)}</dd>
    </>
  );
}

/**
 * The service panel. It renders its chrome and its phase unconditionally, so
 * the window is never blank while the service is starting, degraded, stopped or
 * failed.
 */
export function ServiceStatusPanel() {
  const { snapshot, status, health } = useServiceStatus();

  return (
    <section
      className="panel"
      data-testid="service-status"
      data-phase={status.phase}
      data-reason={status.reason ?? ""}
      role="status"
      aria-live="polite"
    >
      <h2 className="panel__heading">Service</h2>
      <p className={`status__phase phase-${status.phase}`}>{status.phase}</p>
      {status.reason === null ? null : (
        <>
          <p className="status__reason">{status.reason}</p>
          <p className="status__recovery">{REASON_RECOVERY[status.reason]}</p>
        </>
      )}
      <dl className="status__facts">
        <Fact name="service" value={health?.service} />
        <Fact name="service_version" value={health?.service_version} />
        <Fact name="api_schema" value={health?.api_schema} />
        <Fact name="state" value={health?.state} />
        <Fact name="pid" value={health?.pid ?? snapshot?.pid} />
        <Fact name="origin" value={snapshot?.origin} />
        <Fact name="mode" value={snapshot?.mode} />
        <Fact name="exit_code" value={snapshot?.exit_code} />
        <Fact name="database.state" value={health?.database?.state} />
        <Fact name="database.code" value={health?.database?.code} />
        <Fact name="database.schema_version" value={health?.database?.schema_version} />
        <Fact name="database_path" value={snapshot?.database_path} />
        <Fact name="data_dir" value={snapshot?.data_dir} />
        <Fact name="library.samples" value={health?.library?.samples} />
        <Fact name="import.state" value={health?.import?.state} />
      </dl>
      {snapshot?.diagnostic === null || snapshot?.diagnostic === undefined ? null : (
        <pre className="status__diagnostic">{snapshot.diagnostic}</pre>
      )}
    </section>
  );
}
