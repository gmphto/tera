import { invoke } from "@tauri-apps/api/core";

import { START_COMMAND, STOP_COMMAND } from "./bridge";
import { useServiceStatus } from "./ServiceStatusPanel";

/**
 * The two lifecycle controls.
 *
 * Retry is offered in every recoverable phase and asks the host to start or
 * re-probe; Stop is offered only while this window owns the service. Neither
 * control reloads the page or re-creates the client, so the window keeps its
 * state across a stop and a retry.
 */
export function ServiceControls() {
  const { snapshot, status } = useServiceStatus();
  const stopping = status.phase === "stopping";
  const recoverable = status.phase === "unavailable" || status.phase === "failed";
  const label = status.reason === "stopped_by_user" ? "Start service" : "Retry";

  return (
    <section className="panel">
      <h2 className="panel__heading">Controls</h2>
      <div className="controls">
        {recoverable ? (
          <button
            type="button"
            data-testid="service-retry"
            disabled={stopping}
            onClick={() => {
              void invoke(START_COMMAND);
            }}
          >
            {label}
          </button>
        ) : null}
        {snapshot?.mode === "owned" ? (
          <button
            type="button"
            data-testid="service-stop"
            disabled={stopping}
            onClick={() => {
              void invoke(STOP_COMMAND);
            }}
          >
            Stop service
          </button>
        ) : null}
      </div>
    </section>
  );
}
