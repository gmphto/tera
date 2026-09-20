/**
 * The palette slice (issue #32).
 *
 * It holds what the panel renders and nothing a server owns: the loaded palette
 * as the client received it, the editor's draft, and the bookkeeping that makes
 * a stale response refusable. No palette semantic lives in a component, which is
 * why every rule below is covered by a node-environment test.
 */

import { createSlice, type PayloadAction } from "@reduxjs/toolkit";

import type { LibraryError } from "../api/errors";
import type { PaletteItemWire, PaletteWire } from "../api/paletteApi";
import { fromPaletteContext, type ContextDraft } from "./contextDraft";
import { acceptsPaletteResponse } from "./revisionGuard";

export type PaletteStatus = "idle" | "loading" | "ready" | "no-project" | "error";

export interface PaletteDraftState {
  /** The editor's values, or null while the panel derives them from the store. */
  values: ContextDraft | null;
  dirty: boolean;
  saveState: "idle" | "saving" | "error";
  errorField: string | null;
  errorCode: string | null;
}

export interface PaletteState {
  status: PaletteStatus;
  project: { project_id: string; name: string } | null;
  paletteId: string | null;
  /** The last applied revision; -1 before the first response. */
  revision: number;
  /** How many user-initiated palette actions have been issued. */
  epoch: number;
  items: { kick: PaletteItemWire | null; bass: PaletteItemWire | null };
  context: PaletteWire["context"] | null;
  draft: PaletteDraftState;
  pending: { epoch: number; action: string } | null;
  conflict: { attempts: number } | null;
  lastError: LibraryError | null;
  /** How many responses the guard refused; a test proves they were refused. */
  droppedResponses: number;
}

const initialState: PaletteState = {
  status: "idle",
  project: null,
  paletteId: null,
  revision: -1,
  epoch: 0,
  items: { kick: null, bass: null },
  context: null,
  draft: { values: null, dirty: false, saveState: "idle", errorField: null, errorCode: null },
  pending: null,
  conflict: null,
  lastError: null,
  droppedResponses: 0,
};

const paletteSlice = createSlice({
  name: "palette",
  initialState,
  reducers: {
    paletteRequested(state) {
      state.status = state.status === "ready" ? "ready" : "loading";
    },
    paletteLoaded(state, action: PayloadAction<PaletteWire | null>) {
      if (action.payload === null) {
        state.status = "no-project";
        state.project = null;
        state.paletteId = null;
        state.revision = -1;
        state.items = { kick: null, bass: null };
        state.context = null;
        return;
      }
      const palette = action.payload;
      state.status = "ready";
      state.project = palette.project;
      state.paletteId = palette.palette_id;
      state.revision = palette.revision;
      state.items = palette.items;
      state.context = palette.context;
      state.lastError = null;
      state.conflict = null;
    },
    paletteActionIssued(state, action: PayloadAction<string>) {
      state.epoch += 1;
      state.pending = { epoch: state.epoch, action: action.payload };
    },
    paletteResponseApplied(
      state,
      action: PayloadAction<{
        paletteId: string;
        revision: number;
        issuedEpoch: number;
        palette: PaletteWire;
      }>,
    ) {
      // The guard decides here, in the one place a palette response can change
      // what is on screen: a late answer for a superseded action, or one for a
      // lower revision, is counted and dropped rather than applied.
      if (!acceptsPaletteResponse(state, action.payload)) {
        state.droppedResponses += 1;
        return;
      }
      const palette = action.payload.palette;
      state.status = "ready";
      state.project = palette.project;
      state.paletteId = palette.palette_id;
      state.revision = palette.revision;
      state.items = palette.items;
      state.context = palette.context;
      state.pending = null;
      state.conflict = null;
      state.lastError = null;
    },
    paletteResponseDropped(state, _action: PayloadAction<{ issuedEpoch: number }>) {
      state.droppedResponses += 1;
    },
    paletteSaveFailed(state, action: PayloadAction<{ code: string; field: string | null }>) {
      state.draft.saveState = "error";
      state.draft.errorCode = action.payload.code;
      state.draft.errorField = action.payload.field;
      state.pending = null;
    },
    paletteDraftChanged(state, action: PayloadAction<import("./contextDraft").ContextDraft>) {
      state.draft.values = action.payload;
      state.draft.dirty = true;
      state.draft.saveState = "idle";
      state.draft.errorField = null;
      state.draft.errorCode = null;
    },
    paletteDraftCleared(state) {
      // The next render derives a fresh draft from the stored context.
      state.draft = {
        values: null,
        dirty: false,
        saveState: "idle",
        errorField: null,
        errorCode: null,
      };
    },
    paletteConflictCleared(state) {
      state.conflict = null;
    },
    paletteConflicted(state, action: PayloadAction<number>) {
      state.conflict = { attempts: action.payload };
      state.pending = null;
    },
    paletteFailed(state, action: PayloadAction<LibraryError>) {
      state.lastError = action.payload;
      state.pending = null;
      if (state.status !== "ready") {
        state.status = "error";
      }
    },
  },
});

export const {
  paletteRequested,
  paletteLoaded,
  paletteActionIssued,
  paletteResponseApplied,
  paletteResponseDropped,
  paletteSaveFailed,
  paletteDraftChanged,
  paletteDraftCleared,
  paletteConflictCleared,
  paletteConflicted,
  paletteFailed,
} = paletteSlice.actions;

export const paletteReducer = paletteSlice.reducer;
export default paletteSlice.reducer;

// ---------------------------------------------------------------------------
// selectors
// ---------------------------------------------------------------------------

interface WithPalette {
  palette: PaletteState;
}

export function selectPalette(state: WithPalette): PaletteState {
  return state.palette;
}

export function selectPaletteStatus(state: WithPalette): PaletteStatus {
  return state.palette.status;
}

export function selectPaletteRevision(state: WithPalette): number {
  return state.palette.revision;
}

/** The selected kick, or null when the slot is empty. */
export function selectKick(state: WithPalette): PaletteItemWire | null {
  return state.palette.items.kick;
}

/**
 * The kick slot's state: `none` when empty, `role-mismatch` when the sample was
 * re-roled out of the slot, otherwise the item's own sample state.
 */
export function selectKickState(state: WithPalette): string {
  const kick = state.palette.items.kick;
  if (kick === null) {
    return "none";
  }
  return kick.slot_role_mismatch ? "role-mismatch" : kick.sample_state;
}

export function selectContextDraft(state: WithPalette): PaletteDraftState {
  const draft = state.palette.draft;
  if (draft.values !== null || state.palette.context === null) {
    return draft;
  }
  // Nothing typed yet: the editor starts from what is stored.
  return { ...draft, values: fromPaletteContext(state.palette.context) };
}

export function selectPalettePending(state: WithPalette): PaletteState["pending"] {
  return state.palette.pending;
}

export function selectPaletteConflict(state: WithPalette): PaletteState["conflict"] {
  return state.palette.conflict;
}
