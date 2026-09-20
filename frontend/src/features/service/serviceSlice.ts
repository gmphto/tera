import { createSlice, type PayloadAction } from "@reduxjs/toolkit";

import type { ServiceSnapshot } from "./status";

export interface ServiceState {
  /** The last snapshot the host emitted, or null before the first one. */
  snapshot: ServiceSnapshot | null;
  /** The origin the API layer reads on every request; null until it is known. */
  origin: string | null;
}

const initialState: ServiceState = { snapshot: null, origin: null };

const serviceSlice = createSlice({
  name: "service",
  initialState,
  reducers: {
    /** Apply one full snapshot. Every event carries all of these fields. */
    statusReceived(state, action: PayloadAction<ServiceSnapshot>) {
      state.snapshot = action.payload;
      state.origin = action.payload.origin;
    },
  },
});

export const { statusReceived } = serviceSlice.actions;
export default serviceSlice.reducer;
