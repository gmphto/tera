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
  /**
   * Never answer until the request is aborted, for the cancel case.
   *
   * A real fetch settles only when its signal aborts, so this is what makes the
   * "abort records one request and applies nothing" assertion possible: the
   * request stays in flight while the test cancels it.
   */
  pending?: boolean;
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
    // The real pipeline hands `fetch` a `Request`, not a string plus `init`, so
    // the method and the body are read from the request it built rather than
    // from `init`: reading only `init` recorded every request as a GET with no
    // body, which is what the request-log assertions exist to catch.
    const request = new Request(input as RequestInfo, init);
    const body = request.body === null ? "" : await request.clone().text();
    const call: RecordedCall = { method: request.method.toUpperCase(), url: request.url, body };
    calls.push(call);
    const reply = handler(call);
    if (reply.pending === true) {
      return await new Promise<Response>((_resolve, reject) => {
        const signal = request.signal;
        const abort = () => reject(new DOMException("The request was aborted.", "AbortError"));
        if (signal.aborted) {
          abort();
          return;
        }
        signal.addEventListener("abort", abort, { once: true });
      });
    }
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
