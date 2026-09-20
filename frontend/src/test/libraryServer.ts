/**
 * A loopback stub of the six #27 routes this feature calls (issue #31).
 *
 * The approved dependency set installs no `@types/node` and the application code
 * imports no Node-only API, so the module is loaded through a computed specifier
 * exactly as `healthServer.ts` does. The import is confined to this file.
 */

export const HANG = "hang";

export interface RecordedRequest {
  method: string;
  url: string;
  headers: Record<string, string | string[] | undefined>;
  body: string;
}

export interface StubReply {
  status?: number;
  body?: unknown;
  raw?: string;
  contentType?: string;
  delayMs?: number;
}

export type StubHandler = (request: RecordedRequest) => StubReply | typeof HANG;

export interface LibraryStub {
  origin: string;
  requests: RecordedRequest[];
  close(): Promise<void>;
}

interface NodeRequest {
  url?: string;
  method?: string;
  headers?: Record<string, string | string[] | undefined>;
  on(event: string, listener: (chunk?: unknown) => void): void;
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

export async function startLibraryServer(handler: StubHandler): Promise<LibraryStub> {
  const specifier: string = ["node", "http"].join(":");
  const http: HttpModule = await import(specifier);
  const requests: RecordedRequest[] = [];

  const server = http.createServer((request, response) => {
    let body = "";
    request.on("data", (chunk) => {
      body += String(chunk);
    });
    request.on("end", () => {
      const recorded: RecordedRequest = {
        method: request.method ?? "GET",
        url: request.url ?? "",
        headers: request.headers ?? {},
        body,
      };
      requests.push(recorded);
      const reply = handler(recorded);
      if (reply === HANG) {
        return;
      }
      const send = () => {
        response.writeHead(reply.status ?? 200, {
          "content-type": reply.contentType ?? "application/json",
        });
        response.end(reply.raw ?? JSON.stringify(reply.body ?? {}));
      };
      if (reply.delayMs === undefined) {
        send();
      } else {
        setTimeout(send, reply.delayMs);
      }
    });
  });

  await new Promise<void>((resolve) => {
    server.listen(0, "127.0.0.1", resolve);
  });
  const address = server.address();
  if (address === null || typeof address === "string") {
    throw new Error("The stub service did not bind an ephemeral port.");
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
