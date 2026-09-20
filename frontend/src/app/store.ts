import { configureStore, createSlice } from "@reduxjs/toolkit";
import { setupListeners } from "@reduxjs/toolkit/query";

import browserReducer from "../features/palette/state/browserSlice";
import { paletteReducer } from "../features/palette/state/paletteSlice";
import serviceReducer from "../features/service/serviceSlice";
import { teraApi } from "./api";

/**
 * The client timestamp `app.openedAt`, taken once when this module is evaluated.
 *
 * Nothing re-creates the store: a stop and a retry reuse this instance, so the
 * window keeps its history, its scroll position and this timestamp.
 */
const appSlice = createSlice({
  name: "app",
  initialState: { openedAt: new Date().toISOString() },
  reducers: {},
});

export const store = configureStore({
  reducer: {
    app: appSlice.reducer,
    service: serviceReducer,
    browser: browserReducer,
    palette: paletteReducer,
    [teraApi.reducerPath]: teraApi.reducer,
  },
  middleware: (getDefaultMiddleware) => getDefaultMiddleware().concat(teraApi.middleware),
});

setupListeners(store.dispatch);

export type RootState = ReturnType<typeof store.getState>;
export type AppDispatch = typeof store.dispatch;
