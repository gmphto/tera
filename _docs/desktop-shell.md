# The desktop shell (issue #30)

The development window: a Tauri host, a React and TypeScript UI built by Vite,
Redux Toolkit for client workflow state, RTK Query for the one call it makes, and
always-visible status for the local service from
[#27](https://github.com/gmphto/tera/issues/27).

The shell is a **read-only consumer** of #27's contract. It owns no route, no
status file, no second port mechanism and no second supervisor. It computes no
similarity, compatibility, confidence, score or ranking, and it keeps DSP facts,
model judgments and product ranking out of the window entirely.

## Prerequisites

Recorded when this task was groomed; confirm them rather than assume them.

| Component | Version |
| --- | --- |
| Windows | 10.0.26200 x64 |
| WebView2 runtime | 153.0.4234.32 |
| Node / npm | 26.2.0 / 12.0.2 |
| rustc / cargo | 1.96.0, host `x86_64-pc-windows-msvc` |
| MSVC Build Tools | 18, `link.exe` 14.50 |
| uv | 0.12.7 |

## Clean checkout

```sh
uv sync --locked                      # the Python service and pytest
cd frontend && npm ci                 # the pinned npm tree, no writes outside it
npm test                              # vitest: the status table and the API pipeline
npm run build                         # tsc --noEmit, then dist/index.html + hashed assets
npm run tauri dev                     # the window, with Vite on http://localhost:1420
npm run tauri build                   # the release binary
```

Expected artifacts: `frontend/dist/index.html` with `dist/assets/*.js` and
`*.css`; the development window served from port 1420; and, for
`npm run tauri build`, `frontend/src-tauri/target/release/tera-shell.exe`.
`bundle.active` is `false`, so no installer is produced — packaging is
[#38](https://github.com/gmphto/tera/issues/38).

The npm cache may live outside the workspace on some machines. When it does,
point it somewhere writable first, for example
`npm ci --cache "$env:TEMP/tera-npm-cache"`.

## Layout

```text
frontend/
  package.json  package-lock.json  tsconfig.json  vite.config.ts  index.html
  src/
    main.tsx  App.tsx  styles.css
    app/       store.ts  hooks.ts  api.ts  dynamicBaseQuery.ts  api.test.ts
    features/service/  status.ts  status.test.ts  serviceSlice.ts  bridge.ts
                       ServiceStatusPanel.tsx  ServiceControls.tsx
    test/      healthServer.ts
  src-tauri/
    Cargo.toml  Cargo.lock  build.rs  tauri.conf.json  capabilities/default.json
    icons/     icon.ico  32x32.png  128x128.png  128x128@2x.png
    src/       main.rs  service.rs  paths.rs
```

## The process contract the host consumes

Quoted from `_docs/local-service.md`; the service, not this document, owns it.

The spawn line, in the default mode:

```text
<root>\.venv\Scripts\python.exe -m backend.api.service
    --database <database> --host 127.0.0.1 --port <port>
    --port-file <data>\service-port.json
    --dev-origin http://localhost:1420 --dev-origin http://127.0.0.1:1420
```

The one listening line on stdout, and the same object in the port file:

```json
{"api_schema":"1.0","event":"listening","host":"127.0.0.1","pid":1234,"port":7391}
```

The exit-2 refusal line on stderr:

```json
{"code":"port_in_use","event":"bind_failed","host":"127.0.0.1","port":7391}
```

`--port 0` asks for an ephemeral port, which is the default the shell uses;
`TERA_SERVICE_PORT` is passed through explicitly when it is set, preserving
#27's precedence. Exit codes are 0 for a clean shutdown, 2 for a configuration or
bind refusal and 130 for a cancelled import. A refused connection **is** the
whole availability contract: the client treats "nothing answered" as the
service being unavailable, and reads no second status file to decide otherwise.

The host pipes both streams, keeps the last `LOG_LINES_KEPT = 20` lines of each
for diagnostics, and reads only the `listening` line and the
`bind_failed`/`port_in_use` pair out of them. Any other line is ignored, and no
free text is pattern-matched into a reason. It deletes a pre-existing
`service-port.json` before spawning and treats the file only as a fallback,
polling every 100 ms for at most `SERVICE_START_TIMEOUT_SECONDS = 120`.

## Environment variables

| Variable | Meaning | Refusal |
| --- | --- | --- |
| `TERA_PYTHON` | Absolute interpreter path; otherwise `<root>\.venv\Scripts\python.exe` | A missing file is `unavailable` / `python_not_found`; a relative path is `invalid_configuration` |
| `TERA_DEV_DATA_DIR` | Absolute data directory; otherwise `%LOCALAPPDATA%\tera\dev` | A relative path is `invalid_configuration`; no `%LOCALAPPDATA%` and no override is `dev_data_dir_unavailable` |
| `TERA_SERVICE_DATABASE` | Absolute database path; otherwise `<data>\tera.sqlite3` | A relative path is `invalid_configuration` |
| `TERA_SERVICE_PORT` | Passed as `--port`; otherwise `--port 0` | A non-numeric or out-of-range value is `invalid_configuration` **before** any spawn, with `exit_code: null` and no child |
| `TERA_SERVICE_URL` | `http://127.0.0.1:<port>` or `http://localhost:<port>`; the host spawns nothing and reports `mode: "attached"` | Any other shape is `invalid_configuration` |

## The data directory

`<data>` holds `tera.sqlite3`, `service-port.json` and `service-instance.json`
and nothing else. The host creates `<data>` recursively before spawning, because
#27 exits 2 with `database_not_found` when the parent directory is missing. It
never deletes, recreates or rewrites `tera.sqlite3`, and it writes or removes
only the other two files. On first run the directory is created, the window
reaches `running`, and closing and reopening it leaves the database in place
with the same `database.schema_version` and `library.samples`.

`service-instance.json` is schema-versioned:

```json
{"schema":1,"owner_pid":1111,"service_pid":1234,"port":7391,
 "origin":"http://127.0.0.1:7391","mode":"owned",
 "database_path":"<data>/tera.sqlite3","started_at":"...","heartbeat_at":"..."}
```

The owner refreshes `heartbeat_at` every `INSTANCE_HEARTBEAT_SECONDS = 2` and
writes the record atomically (a temporary file renamed over the target).

## Phases, reasons and recovery

The panel renders one phase and at most one reason from the closed unions in
`frontend/src/features/service/status.ts`. No other value appears in code, the
UI or this document.

| Phase | Reason | The sentence the panel shows |
| --- | --- | --- |
| `starting` | — | A spawn is in flight, or a live child has no health success yet |
| `running` | — | A successful `GET /health` reported `state: ok` with a live child or attached mode |
| `degraded` | — | The same, with `state: degraded`; `database.state` and `database.code` are shown |
| `stopping` | — | A stop is in flight |
| `unavailable` | `python_not_found` | Run uv sync in the repository root, then Retry… |
| `unavailable` | `dev_data_dir_unavailable` | Set TERA_DEV_DATA_DIR to a writable absolute directory… |
| `unavailable` | `invalid_configuration` | Correct or unset the TERA_* variable named above… |
| `unavailable` | `connection_refused` | Nothing is listening at the recorded origin… |
| `unavailable` | `stopped_by_user` | This window stopped the service. Start service launches it again… |
| `failed` | `spawn_failed` | The interpreter could not be started… |
| `failed` | `start_timeout` | The service never reported listening… |
| `failed` | `port_in_use` | Free the port named above, or unset TERA_SERVICE_PORT… |
| `failed` | `service_exited` | The service exited on its own; its exit code and last diagnostic line are shown… |
| `failed` | `service_lost` | The attached service stopped answering… |
| `failed` | `health_timeout` | GET /health did not answer in time… |
| `failed` | `health_error` | GET /health answered with an error status or an unreadable body… |

`REASON_RECOVERY` is total over the twelve reasons; `status.test.ts` asserts
that, and that every sentence is non-empty and at most
`RECOVERY_MAX_LENGTH = 200` characters. The precedence rule is stated and tested
in `deriveServiceStatus`: a stop in flight wins; a recorded failure or
unavailability, or a failed health result, wins over stale health data;
`running` and `degraded` need a current successful `GET /health` plus a live
child or attached mode; anything else is `starting`. No reason alone can show
`running`.

## Origins

The development origin is `http://localhost:1420`, passed to the service with
`--dev-origin` (and `http://127.0.0.1:1420` beside it). Vite is pinned to port
1420 with `strictPort: true`, because the service's Origin allowlist and the
host's `devUrl` both name 1420: a Vite that silently moved to another port would
have every request refused by #27's policy.

In a packaged build the window's origin is `tauri://localhost` or
`http://tauri.localhost`, which #27 already allows. This task serves the
development window only.

## Ownership, heartbeat, reap and shutdown

On startup the host reads `service-instance.json`, probes `GET /health` at the
recorded origin for at most 2 s, and applies exactly these rules:

- A live service whose reported `pid` equals the record's `service_pid` with a
  `heartbeat_at` older than `INSTANCE_STALE_SECONDS = 10` is an orphan of a dead
  window: it is terminated (logged as `service_reaped`) before this window spawns
  its own.
- A live service with a fresh heartbeat belongs to another live window: this
  window attaches (`mode: "attached"`), never spawns a second service, and never
  terminates it on exit. Only the owner stops its child.
- A record whose service does not answer is overwritten, and the process id in
  it is **never** killed, because a recycled pid must not be terminated.

On every normal exit the owner kills its child, waits at most
`SERVICE_STOP_TIMEOUT_SECONDS = 5` for it to disappear, removes
`service-port.json` and `service-instance.json`, and only then lets the app exit.
`WindowEvent::CloseRequested` and `RunEvent::ExitRequested` both reach this path.

**Known deviation from #27.** The shell *terminates* the child rather than
delivering the console signal #27 handles for its clean-shutdown path, because a
Windows GUI host cannot deliver that signal. The port file and the instance
record are removed by the host instead, which is the observable part of the same
outcome. A stronger guarantee — a Windows Job Object that kills the service even
on a hard kill of the host — is [#82](https://github.com/gmphto/tera/issues/82),
because it needs another crate.

## Endpoints and client state

RTK Query carries exactly one endpoint, `getHealth`, whose request is exactly
`<origin>/health` with no query string, no header of the shell's own, no
credential and no proxy. `dynamicBaseQuery` reads the origin from the current
state on every request, because the port is dynamic. The panel skips the query
while the origin is null, so no request is issued before the port is known. A
healthy service is polled every `SERVICE_POLL_INTERVAL_MS = 5000`; a fetch that
does not answer within `HEALTH_TIMEOUT_MS = 5000` is `health_timeout`; a non-200
or unreadable body is `health_error` with the status recorded; a refusal after
at least one healthy poll is `service_lost`, and a refusal that never succeeded
is `connection_refused`.

`store.ts` creates one store instance at module scope with `app.openedAt` set
once. A retry dispatches through the host and never reloads the page or
re-creates the client, so the window keeps its history and its scroll position
across a stop and a retry.

## Approved dependencies

Approved by the user before implementation on the terms below; grooming is not
approval, and this table records the approval rather than implying one.

| Kind | Packages |
| --- | --- |
| npm dependencies | `react` 19.3.0, `react-dom` 19.3.0, `@reduxjs/toolkit` 2.12.0, `react-redux` 9.3.0, `@tauri-apps/api` 2.11.1 |
| npm devDependencies | `@tauri-apps/cli` 2.11.4, `vite` 8.3.0, `vitest` 5.0.1, `@vitejs/plugin-react` 6.1.1, `typescript` 7.0.2, `@types/react` 19.3.0, `@types/react-dom` 19.3.0 |
| Rust crates | `tauri` =2.11.5, `tauri-build` 2, `serde` 1, `serde_json` 1 |

No Tauri plugin crate, no jsdom or Testing Library, no ESLint or formatter, no
CSS framework, no router, no HTTP client, no mocking library and no Python
dependency. `tauri` carries an exact pin because a caret range resolved 2.11.6,
and the committed `Cargo.lock` must resolve the approved version.

## Deviations recorded during implementation

1. **`skipLibCheck` is `true`, not `false`.** With `skipLibCheck: false` and no
   `@types/node`, `tsc` reports errors inside `vitest`'s own declaration files
   (`BufferEncoding`, `NodeJS`, `node:stream`, `node:path`, …), because
   `vite.config.ts` imports `defineConfig` from `vitest/config`. Those three
   requirements — `skipLibCheck: false`, no `@types/node`, and the
   `vitest/config` import — cannot hold together. The application and test
   sources are still checked strictly; only dependency declaration files are
   skipped.
2. **`types: ["vite/client"]`.** `main.tsx` imports `./styles.css`, which only
   Vite's client types declare. No Node types are included.
3. **`healthServer.ts` loads `node:http` through a computed specifier.** The
   approved set has no `@types/node`, and the stub server needs a real loopback
   listener, so the module name is built at runtime: the test runner is Node and
   resolves it, while the typecheck sees an untyped namespace and no Node types.
   The import is confined to that one test helper.
4. **The Rust host is not compiled in the authoring session.** `cargo` cannot
   spawn `rustc` under the agent sandbox (`could not execute process rustc.exe …
   Access is denied`). `cargo generate-lockfile` succeeds, so the dependency
   graph is resolved and `Cargo.lock` is committed; the window criteria are
   therefore **unverified**, and the smoke check below is the developer's to run.

## Troubleshooting

| Case | What you see | What to do |
| --- | --- | --- |
| The service is absent or not installed | `unavailable` / `python_not_found` | Run `uv sync` in the repository root, then Retry. Set `TERA_PYTHON` if the interpreter lives elsewhere |
| The service exits mid-session | `failed` / `service_exited` with the exit code and last diagnostic line | Read the diagnostic, fix the cause, then Retry — it starts exactly one new service |
| The port is already held | `failed` / `port_in_use` naming the port, after the parsed `bind_failed` line | Free the port, or unset `TERA_SERVICE_PORT` so the shell asks for an ephemeral one |
| Development origin versus production origin | Every request refused with 403 | Use `http://localhost:1420` in development; `tauri://localhost` and `http://tauri.localhost` belong to a packaged build |
| The first-run data directory | An empty history, `<data>` created on launch | Expect exactly `tera.sqlite3`, `service-port.json` and `service-instance.json`; the panel shows the resolved directory |
| Shutdown left an orphan process | `Get-Process` still lists the recorded `service_pid` | Check `service-instance.json`; a hard kill of the host is the case [#82](https://github.com/gmphto/tera/issues/82) owns |

## Smoke check

A green typecheck, test and web build is **not** evidence for the window
criteria. The check that is:

1. Delete `<data>`, launch `npm run tauri dev`, and read `data-phase`,
   `data-reason` and the directory listing.
2. Rename `<root>\.venv` away, relaunch, and read `data-phase="unavailable"`,
   `data-reason="python_not_found"`.
3. Hold `TERA_SERVICE_PORT` with another listener, relaunch, and read
   `data-phase="failed"`, `data-reason="port_in_use"`.
4. Start the service by hand with `uv run`, set `TERA_SERVICE_URL`, relaunch, and
   confirm `mode: "attached"`.
5. Kill the child while the window is open and read `data-phase` before and
   after.
6. Close and reopen the window, then check `Get-Process` and
   `Get-NetTCPConnection -LocalPort <port>`.

Absolute paths are redacted to placeholders in any recorded evidence.
