/**
 * A recording `fetch` double for the recommendation pipeline (issue #33).
 *
 * #30's base query builds its `fetchBaseQuery` per request, so the global is
 * read at request time and this double sees exactly what the real pipeline
 * sends -- which is what makes the request log assertions possible.
 */

export interface RecordedCall {
  method: string;
  url: string;
  body: string;
}

export interface StubReply {
  status?: number;
  body?: unknown;
  raw?: string;
  contentType?: string;
  /** Reject instead of answering, for the unreachable-service case. */
  reject?: boolean;
}

export type StubHandler = (call: RecordedCall) => StubReply;

export interface FetchDouble {
  calls: RecordedCall[];
  /** Every call as `METHOD path`, for asserting an exact sequence. */
  path(): string[];
  restore(): void;
}

export function installStubFetch(handler: StubHandler): FetchDouble {
  const original = globalThis.fetch;
  const calls: RecordedCall[] = [];
  const double = (async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
    const method = (init?.method ?? "GET").toUpperCase();
    const body = typeof init?.body === "string" ? init.body : "";
    const call: RecordedCall = { method, url, body };
    calls.push(call);
    const reply = handler(call);
    if (reply.reject === true) {
      throw new TypeError("Failed to fetch");
    }
    return new Response(reply.raw ?? JSON.stringify(reply.body ?? {}), {
      status: reply.status ?? 200,
      headers: { "content-type": reply.contentType ?? "application/json" },
    });
  }) as typeof fetch;
  globalThis.fetch = double;
  return {
    calls,
    path: () => calls.map((call) => `${call.method} ${new URL(call.url).pathname}`),
    restore: () => {
      globalThis.fetch = original;
    },
  };
}
