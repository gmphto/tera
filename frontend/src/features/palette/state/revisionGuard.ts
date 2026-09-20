/**
 * The one place a palette response is accepted or refused (issue #32).
 *
 * #33's recommendation responses must use these same functions: a response
 * carrying a revision older than the loaded one, or an epoch older than the
 * newest user action, cannot change what is on screen.
 */

import type { PaletteState } from "./paletteSlice";

/** How many times one conflicted action is retried before it is shown. */
export const MAX_CONFLICT_RETRIES = 1;

export interface PaletteResponse {
  paletteId: string;
  revision: number;
  issuedEpoch: number;
}

/** The revision in hand belongs to the loaded palette and is not older. */
export function isCurrentRevision(
  state: PaletteState,
  paletteId: string,
  revision: number,
): boolean {
  return state.paletteId === paletteId && revision >= state.revision;
}

/**
 * Whether one response may be applied.
 *
 * All three must hold: it names the palette this client loaded, it was issued
 * under the newest user action, and its revision is not older than the one in
 * hand. A response that fails any of them is dropped and counted, which is what
 * stops a slow answer for kick A from putting A back after the producer chose B.
 */
export function acceptsPaletteResponse(state: PaletteState, response: PaletteResponse): boolean {
  return (
    isCurrentRevision(state, response.paletteId, response.revision) &&
    response.issuedEpoch === state.epoch
  );
}

/**
 * Whether a conflicted action is still the newest one and may be retried once.
 *
 * A conflict for an action the producer has already replaced is dropped with no
 * retry: the newer action owns the slot.
 */
export function shouldRetryConflict(state: PaletteState, issuedEpoch: number, attempt: number): boolean {
  return issuedEpoch === state.epoch && attempt <= MAX_CONFLICT_RETRIES && state.conflict === null;
}
