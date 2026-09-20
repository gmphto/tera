/**
 * The kick slot (issue #32).
 *
 * Every state follows the projection: `none` for an empty slot, `role-mismatch`
 * when the sample was re-roled out of the slot, otherwise the item's own sample
 * state. An item in any state stays displayed -- nothing is auto-removed,
 * auto-replaced or hidden -- and a state that is not `present` offers the
 * reselect control, because repairing the palette is a selection and never a
 * rewrite of the library.
 */

import type { PaletteItemWire } from "./api/paletteApi";

const SENTENCE: Record<string, string> = {
  none: "No kick is selected. A bass recommendation needs one.",
  present: "This kick is available.",
  missing: "This kick's file is no longer at its stored path. Recommendations cannot use it until it is available or replaced.",
  unknown: "This kick's file has not been checked since it was imported. Recommendations cannot use it until it is available or replaced.",
  removed: "This kick's library row was pruned. Recommendations cannot use it until it is available or replaced.",
  "role-mismatch": "This sample's role changed, so it is no longer a kick. Recommendations cannot use it until it is available or replaced.",
};

export interface KickSlotControlProps {
  kick: PaletteItemWire | null;
  state: string;
  fileLabel: string | null;
  onChoose(): void;
}

export function KickSlotControl({ kick, state, fileLabel, onChoose }: KickSlotControlProps) {
  const chooseLabel = state === "none" ? "Choose kick" : "Replace kick";
  const offersReselect = state !== "present" && state !== "none";
  return (
    <div className="kick" data-testid="palette-kick" data-kick-state={state} data-sample-id={kick?.sample_id ?? ""}>
      <p className="kick__state">{SENTENCE[state] ?? "This kick's state is unknown."}</p>
      {kick === null ? null : (
        <dl className="kick__facts">
          <dt>sample</dt>
          <dd>{fileLabel ?? kick.sample_id}</dd>
          <dt>role</dt>
          <dd>{kick.role}</dd>
          <dt>added at revision</dt>
          <dd>{kick.added_revision}</dd>
        </dl>
      )}
      <button type="button" data-testid="palette-kick-choose" onClick={onChoose}>
        {chooseLabel}
      </button>
      {offersReselect ? (
        <button type="button" data-testid="palette-kick-reselect" onClick={onChoose}>
          Select another kick
        </button>
      ) : null}
    </div>
  );
}
