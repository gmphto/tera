/**
 * A loopback stub of the five palette and project routes (issue #32).
 *
 * Loaded through a computed specifier for the same reason as `healthServer.ts`
 * and `libraryServer.ts`: the approved dependency set installs no `@types/node`
 * and the application code imports no Node-only API.
 */

import type { PaletteWire } from "../features/palette/api/paletteApi";

export interface RecordedRequest {
  method: string;
  url: string;
  headers: Record<string, string | string[] | undefined>;
  body: string;
}export interface StubReply {
  status?: number;
  body?: unknown;
  raw?: string;
  contentType?: string;
}

export type StubHandler = (request: RecordedRequest) => StubReply;

export interface PaletteStub {
  origin: string;
  requests: RecordedRequest[];
  close(): Promise<void>;
  /** The path of every request, in order, for asserting an exact sequence. */
  path(): string[];
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

export async function startPaletteServer(handler: StubHandler): Promise<PaletteStub> {
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
      response.writeHead(reply.status ?? 200, {
        "content-type": reply.contentType ?? "application/json",
      });
      response.end(reply.raw ?? JSON.stringify(reply.body ?? {}));
    });
  });

  await new Promise<void>((resolve) => {
    server.listen(0, "127.0.0.1", resolve);
  });
  const address = server.address();
  if (address === null || typeof address === "string") {
    throw new Error("The palette stub did not bind an ephemeral port.");
  }

  return {
    origin: `http://127.0.0.1:${address.port}`,
    requests,
    path: () => requests.map((request) => `${request.method} ${request.url}`),
    close: () =>
      new Promise<void>((resolve) => {
        server.closeAllConnections?.();
        server.close(() => resolve());
      }),
  };
}

/** One palette body in the documented projection shape. */
export function paletteBody(
  revision: number,
  kick: string | null,
  projectId = "project-001",
): { api_schema: string; palette: PaletteWire } {
  return {
    api_schema: "1.0",
    palette: {
      palette_id: "palette-001",
      project: { project_id: projectId, name: "Track A" },
      name: "Main",
      revision,
      context: { tempo: { state: "unset" }, key: { state: "unset" }, genre: { state: "unset" } },
      items: {
        kick:
          kick === null
            ? null
            : {
                slot: "kick",
                sample_id: kick,
                role: "kick",
                added_revision: revision,
                sample_state: "present",
                sample_error_code: null,
                slot_role_mismatch: false,
              },
        bass: null,
      },
    },
  };
}

/** One #27 error envelope. */
export function errorBody(code: string, details: Record<string, unknown> = {}) {
  return { api_schema: "1.0", error: { code, message: "refused", details } };
}
