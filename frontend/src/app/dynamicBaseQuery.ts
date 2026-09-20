import type { BaseQueryFn, FetchArgs, FetchBaseQueryError } from "@reduxjs/toolkit/query";
import { fetchBaseQuery } from "@reduxjs/toolkit/query/react";

import type { RootState } from "./store";

/** The error the base query reports before the service's port is known. */
export const ORIGIN_UNKNOWN = "The service origin is not known yet.";

/**
 * `fetchBaseQuery` with the base URL read from the current state.
 *
 * The service is started with `--port 0` by default, so its port — and therefore
 * its origin — is only known once it reports listening. A module-level constant
 * cannot express that. This wrapper adds no header, no credential and no proxy:
 * every request is a plain loopback GET.
 */
export function createDynamicBaseQuery(
  timeoutMs?: number,
): BaseQueryFn<string | FetchArgs, unknown, FetchBaseQueryError> {
  return async (args, api) => {
    const origin = (api.getState() as RootState).service.origin;
    if (origin === null) {
      return { error: { status: "CUSTOM_ERROR", error: ORIGIN_UNKNOWN } };
    }
    const query = fetchBaseQuery({ baseUrl: origin, timeout: timeoutMs });
    return query(args, api, {});
  };
}

/** The app's base query, at the documented health timeout. */
export const dynamicBaseQuery = createDynamicBaseQuery();
