import { createApi } from "@reduxjs/toolkit/query/react";

import type { HealthBody } from "../features/service/status";
import { createDynamicBaseQuery } from "./dynamicBaseQuery";

/** How long `GET /health` may take before it counts as a timeout. */
export const HEALTH_TIMEOUT_MS = 5000;

/** How often a healthy service is polled. */
export const SERVICE_POLL_INTERVAL_MS = 5000;

/**
 * The shell's one API, as a factory so a test can use a short timeout.
 *
 * It carries exactly one endpoint and never leaves the loopback interface.
 */
export function createTeraApi({ timeoutMs = HEALTH_TIMEOUT_MS }: { timeoutMs?: number } = {}) {
  return createApi({
    reducerPath: "teraApi",
    tagTypes: ["Palette"],
    baseQuery: createDynamicBaseQuery(timeoutMs),
    endpoints: (build) => ({
      getHealth: build.query<HealthBody, void>({
        query: () => "/health",
      }),
    }),
  });
}

/** The instance the app uses. */
export const teraApi = createTeraApi();

export const { useGetHealthQuery } = teraApi;
