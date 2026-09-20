import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";

import { store } from "../../app/store";
import { statusReceived } from "./serviceSlice";
import type { ServiceSnapshot } from "./status";

/** The one command that returns a snapshot. */
export const STATUS_COMMAND = "service_status";

/** The one event that carries a snapshot. */
export const STATUS_EVENT = "service:status";

/** The two commands that change the service's lifecycle. */
export const START_COMMAND = "service_start";
export const STOP_COMMAND = "service_stop";

function apply(snapshot: ServiceSnapshot): void {
  store.dispatch(statusReceived(snapshot));
}

/**
 * Wire the window to the host: one snapshot path, no second status channel.
 *
 * The command is invoked on mount and the event is subscribed to afterwards, so
 * a snapshot the host emitted before the listener existed is still shown — the
 * command's answer is the current one. Every event then carries the full
 * snapshot, so applying it wholesale is always correct.
 *
 * Outside the Tauri host (a plain browser tab) there is no host to talk to; the
 * panel keeps rendering the `starting` phase and the window never goes blank.
 */
export function startBridge(): () => void {
  let unlisten: (() => void) | null = null;
  let stopped = false;

  void invoke<ServiceSnapshot>(STATUS_COMMAND)
    .then(apply)
    .catch(() => undefined)
    .then(() => listen<ServiceSnapshot>(STATUS_EVENT, (event) => apply(event.payload)))
    .then((dispose) => {
      if (stopped) {
        dispose();
      } else {
        unlisten = dispose;
      }
    })
    .catch(() => undefined);

  return () => {
    stopped = true;
    if (unlisten !== null) {
      unlisten();
    }
  };
}
