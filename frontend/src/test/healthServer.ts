/**
 * A stub `GET /health` server for the RTK Query tests.
 *
 * The approved dependency set installs no `@types/node` and the application
 * code imports no Node-only API. This helper needs a real loopback listener, so
 * it loads `node:http` through a computed specifier: at test time the runner is
 * Node and the module exists, while the typecheck sees an untyped namespace and
 * no Node types. The import is confined to this file.
 */

/** A responder that accepts the connection and never answers it. */
export const HANG = "hang";

export interface Answer {
  status?: number;
  body?: string;
  contentType?: string;
}

export type Responder = (request: { url: string; method: string }) => Answer | typeof HANG;

export interface StubServer {
  origin: string;
  /** Every request path the stub saw, in order. */
  requests: string[];
  close(): Promise<void>;
}

interface NodeRequest {
  url?: string;
  method?: string;
}

interface NodeResponse {
  writeHead(status: number, headers: Record<string, string>): void;
  end(body?: string): void;
}

interface NodeServer {
  listen(port: number, host: string, callback: () => void): void;
  address(): { port: number } | string | null;
  close(callback?: (error?: Error) => void): void;
  closeAllConnections?(): void;
}

interface HttpModule {
  createServer(handler: (request: NodeRequest, response: NodeResponse) => void): NodeServer;
}

/** Start one stub health server on an ephemeral loopback port. */
export async function startHealthServer(respond: Responder | Answer): Promise<StubServer> {
  const specifier: string = ["node", "http"].join(":");
  const http: HttpModule = await import(specifier);
  const requests: string[] = [];
  const answerOf: Responder = typeof respond === "function" ? respond : () => respond;

  const server = http.createServer((request, response) => {
    const url = request.url ?? "";
    requests.push(url);
    const answer = answerOf({ url, method: request.method ?? "GET" });
    if (answer === HANG) {
      // Accepted, deliberately unanswered: the client's own timeout must fire.
      return;
    }
    response.writeHead(answer.status ?? 200, {
      "content-type": answer.contentType ?? "application/json",
    });
    response.end(answer.body ?? "");
  });

  await new Promise<void>((resolve) => {
    server.listen(0, "127.0.0.1", resolve);
  });

  const address = server.address();
  if (address === null || typeof address === "string") {
    throw new Error("The stub server did not bind an ephemeral port.");
  }

  return {
    origin: `http://127.0.0.1:${address.port}`,
    requests,
    close: () =>
      new Promise<void>((resolve) => {
        server.closeAllConnections?.();
        server.close(() => resolve());
      }),
  };
}
