/**
 * The browse and import state (issue #31).
 *
 * It holds the query arguments and the selection, and nothing else: no page, no
 * sample row, no file name and no cached response. The rendered list always
 * comes from the RTK Query cache entry for the current arguments, so a late
 * response for superseded arguments lands under its own key and is never shown.
 */

import { createSlice, type PayloadAction } from "@reduxjs/toolkit";

import { DEFAULT_PAGE_SIZE, PALETTE_ROLES, type PaletteRole } from "../api/types";

export interface BrowserState {
  /** Selected role filters, in the fixed `PALETTE_ROLES` order. */
  roles: PaletteRole[];
  /** What the input shows right now. */
  text: string;
  /** What the list queries: committed `SEARCH_DEBOUNCE_MS` after the last keystroke. */
  committedText: string;
  limit: number;
  /** Positions already used; `cursorStack[pageIndex]` is the cursor in force. */
  cursorStack: (string | null)[];
  pageIndex: number;
  /** The one identity this feature stores. */
  selectedSampleId: string | null;
  /** Set when the service answered 404 for the selection. */
  selectionStale: boolean;
  importRole: PaletteRole;
  importRunId: string | null;
}

const initialState: BrowserState = {
  roles: [],
  text: "",
  committedText: "",
  limit: DEFAULT_PAGE_SIZE,
  cursorStack: [null],
  pageIndex: 0,
  selectedSampleId: null,
  selectionStale: false,
  importRole: "kick",
  importRunId: null,
};

/** Any change to the query arguments starts the list again at the first page. */
function restartPaging(state: BrowserState) {
  state.cursorStack = [null];
  state.pageIndex = 0;
}

function canonicalRoles(roles: PaletteRole[]): PaletteRole[] {
  return PALETTE_ROLES.filter((role) => roles.includes(role));
}

const browserSlice = createSlice({
  name: "browser",
  initialState,
  reducers: {
    searchTextChanged(state, action: PayloadAction<string>) {
      state.text = action.payload;
    },
    searchCommitted(state, action: PayloadAction<string>) {
      if (state.committedText === action.payload) {
        return;
      }
      state.committedText = action.payload;
      restartPaging(state);
    },
    rolesChanged(state, action: PayloadAction<PaletteRole[]>) {
      state.roles = canonicalRoles(action.payload);
      restartPaging(state);
    },
    pageSizeChanged(state, action: PayloadAction<number>) {
      state.limit = action.payload;
      restartPaging(state);
    },
    pagePushed(state, action: PayloadAction<string | null>) {
      state.cursorStack = [...state.cursorStack.slice(0, state.pageIndex + 1), action.payload];
      state.pageIndex += 1;
    },
    pagePopped(state) {
      if (state.pageIndex > 0) {
        state.cursorStack = state.cursorStack.slice(0, state.pageIndex);
        state.pageIndex -= 1;
      }
    },
    pagingReset(state) {
      restartPaging(state);
    },
    sampleSelected(state, action: PayloadAction<string>) {
      state.selectedSampleId = action.payload;
      state.selectionStale = false;
    },
    selectionKept(state) {
      state.selectionStale = false;
    },
    selectionRefused(state) {
      state.selectedSampleId = null;
      state.selectionStale = true;
    },
    importRoleChanged(state, action: PayloadAction<PaletteRole>) {
      state.importRole = action.payload;
    },
    importRunTracked(state, action: PayloadAction<string>) {
      state.importRunId = action.payload;
    },
  },
});

export const {
  searchTextChanged,
  searchCommitted,
  rolesChanged,
  pageSizeChanged,
  pagePushed,
  pagePopped,
  pagingReset,
  sampleSelected,
  selectionKept,
  selectionRefused,
  importRoleChanged,
  importRunTracked,
} = browserSlice.actions;

export default browserSlice.reducer;
