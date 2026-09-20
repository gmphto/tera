# Local service API

This document is the contract of the local HTTP service behind the kick-to-bass
MVP (issue #27, plan step 9). One process serves one local SQLite library over
HTTP/1.1 on the IPv4 loopback interface so a desktop client can start a folder
import, watch its progress, cancel or retry it, search and page samples by role
and read the stored measurements of one sample.

The service is a thin transport over the landed storage, scan and job layers:
identity, migrations, the repository and the error base come from
[`library-storage.md`](library-storage.md) (issue #21); discovery, the scan
summary and its codes come from [`library-scan.md`](library-scan.md) (issue
#22); runs, items, leases, retry classification and the progress summary come
from [`library-jobs.md`](library-jobs.md) (issue #23). Nothing here invents a
second schema, a second scan policy, a second queue, a second error vocabulary
or a second route table.

The service is deliberately narrow. It binds loopback only, returns no
filesystem path beyond a stored file's own name, serves no audio byte, holds no
credential, and opens no outbound connection: the recommendation operation is
issue #28, audition playback is #34, the desktop shell is #30, and the client
panels are #31 to #35.

## Command and exit codes

```text
uv run python -m backend.api.service --database DB.sqlite3 [--host 127.0.0.1]
                                     [--port 7391] [--port-file PATH]
                                     [--dev-origin ORIGIN]...
```

| Flag | Meaning |
| --- | --- |
| `--database` | The local library database. A parent directory that does not exist and a path that is a directory are refused with `database_not_found` before anything is opened, so a mistyped path never creates a file. A file that exists but is not SQLite, a corrupt database or one whose stored schema is newer is the database's own state: the service starts `degraded` and keeps answering `/health`. |
| `--host` | The bind address. Only `127.0.0.1` is accepted; anything else exits 2 with `invalid_bind_host`. |
| `--port` | The TCP port, `1024` to `65535`, or `0` for an ephemeral one. Precedence is the flag, then `TERA_SERVICE_PORT`, then `DEFAULT_PORT` = 7391. |
| `--port-file` | A local file to write the listening object to, atomically, after the socket is bound. It is removed on a clean shutdown. |
| `--dev-origin` | One extra allowed development origin, exactly `http://localhost:PORT` or `http://127.0.0.1:PORT`. May repeat. |

| Exit | Meaning |
| --- | --- |
| 0 | Clean shutdown, including `--help` |
| 2 | Invalid command, configuration, database path, bind host, port, port file or dev origin, or an address that is already in use |
| 130 | A stop signal arrived a second time during shutdown |

### The listening line and the port file

After the listen socket is bound and before the first request is served, exactly
one JSON line is printed to stdout:

```json
{"api_schema":"1.0","event":"listening","host":"127.0.0.1","pid":1234,"port":7391}
```

With `--port 0` the `port` is the bound ephemeral port, so a client can spawn
the service without choosing one. With `--port-file PATH` the same object is
written to that file, atomically (a temporary file in the same directory and an
`os.replace`), after binding and before the line is printed.

A stop signal is `SIGINT` or `SIGTERM` (`SIGBREAK` is accepted on Windows so a
console event can reach the process). Availability is a refused connection:
there is no state file, no health file and no other status channel. A client
that finds the port closed starts the service again.

A refused configuration prints exactly one JSON line on stderr and exits 2:

```json
{"code":"port_in_use","event":"bind_failed","host":"127.0.0.1","port":7391}
{"code":"invalid_port","event":"configuration_error","host":"127.0.0.1","port":null}
```

The socket is bound before the database is touched, so a port that is already in
use writes nothing to the database and never opens it.

## The route table

Seven operations, and nothing else. An unknown path is 404 `unknown_route`; a
known path with an unsupported method is 405 `method_not_allowed` with an
`Allow` header naming the methods that path accepts. Every method is dispatched,
so an invented method is a 405 rather than the standard library's 501.

| Method | Path | Request | Success | Success body |
| --- | --- | --- | --- | --- |
| GET | `/health` | none | 200 | The whole status document, degraded or not |
| POST | `/imports` | `{"root", "role"}` | 202 | `{"api_schema", "import": {"run_id", "state", "phase", "role", "analysis_version", "started_at", "status_path"}}` with `Location` |
| GET | `/imports/{run_id}` | none | 200 | `{"api_schema", "import": {run fields, "scan", "counts", "current", "failures"}}` |
| POST | `/imports/{run_id}/cancel` | none | 202 | `{"api_schema", "import": {"run_id", "state", "cancel_requested", "status_path"}}` |
| POST | `/imports/{run_id}/retry` | none | 202 | `{"api_schema", "import": {"run_id", "retried", "status_path"}}` |
| GET | `/library/samples` | query | 200 | `{"api_schema", "items", "page", "query"}` |
| GET | `/library/samples/{sample_id}` | none | 200 | `{"api_schema", "sample": {..., "features"}}` |

The same table as the code spells it (`service.ROUTES`), which is the one route
table the rest of Phase 1 reads:

- `GET /health`
- `POST /imports`
- `GET /imports/{run_id}`
- `POST /imports/{run_id}/cancel`
- `POST /imports/{run_id}/retry`
- `GET /library/samples`
- `GET /library/samples/{sample_id}`

No route serves an audio byte, a filesystem path or a file:
`GET /library/samples/{sample_id}/audio`, `GET /library/audio` and every other
audio-shaped path are 404 `unknown_route`.

### `GET /health`

```json
{"api_schema":"1.0","database":{"code":null,"journal_mode":"wal","schema_version":4,"state":"ok"},
 "import":{"phase":null,"run_id":null,"started_at":null,"state":"idle"},
 "library":{"by_role":{"bass":0,"kick":0,"sub-bass":0},"pending_analysis":0,"roots":0,"samples":0},
 "limits":{"max_concurrent_requests":8,"max_page_size":200,"max_request_bytes":65536},
 "pid":1234,"roles":["kick","bass","sub-bass"],
 "service":"tera-local-service","service_version":"0.1.0",
 "started_at":"2026-09-20T05:21:08Z","state":"ok"}
```

`state` is `ok` or `degraded`. The service starts and keeps serving when #21's
open or verify fails: `state` is `degraded`, `database.code` holds #21's code
and `database.schema_version` and `database.journal_mode` are null, because
neither can be read. Every other route then answers 503
`database_unavailable` while `/health` still answers. In a degraded service the
`import` block is null and the `library` counts are null: no run row and no
count can be read, so none is invented.

`library.pending_analysis` is the length of #22's
`indexer.pending_analysis(connection)`; `library.roots` is the number of
distinct directories that hold at least one stored sample; `analysis_version`
is `digest(analysis_descriptor())`; `roles` is #9's `ROLES`. `import` reports
the live run (#23's rule: state `running` with a heartbeat younger than
`LEASE_SECONDS`), so a client that lost a `run_id` recovers it here, or from
the 409 body of a new `POST /imports`.

Documented errors: 503 `database_unavailable` on every other route. `/health`
answers within 1 s while an import is running and while another client holds a
request: it reads through the handler connection and #21 configures WAL, so a
reader never waits for the import's writer.

### `POST /imports`

```json
{"root": "C:\\Samples\\Kicks", "role": "kick"}
```

`root` is required and validated with #22's root rules — an absolute local
directory, existing, not UNC, not a mapped network drive and reached through no
linked ancestor — and `role` is required and must be in #9's `ROLES`. A failure
is 400 `invalid_root` or 400 `invalid_role` with no scan, no run row and no
queue write.

The response echoes no path, no directory component and no `root`:

```json
{"api_schema":"1.0","import":{"analysis_version":"0b51...ea564","phase":"scanning",
 "role":"kick","run_id":"run-1e2ce2e140063b8fd2ab0cc64001488b","started_at":"2026-09-20T05:21:08Z",
 "state":"running","status_path":"/imports/run-1e2ce2e140063b8fd2ab0cc64001488b"}}
```

The client already knows the folder it chose, a run is identified by its opaque
`run_id`, and the folder itself is described by `root_label` (the last path
component only) inside `GET /imports/{run_id}`. A root that is a drive root has
an empty `root_label` rather than a drive letter.

The request thread validates, opens the run through #23's `open_run` and hands
the work to a service-owned runner thread; it never scans, decodes or extracts
inline, so a request returns in well under a second while an extraction is still
blocked. The runner drives #22's `scanner.reconcile` and then #23's
`worker.run_queue`, unchanged.

Documented errors: 400 `invalid_json`, 400 `invalid_body`, 400
`missing_field`, 400 `invalid_field_type`, 400 `unknown_field`, 400
`invalid_root`, 400 `invalid_role`, 403 `host_not_allowed`, 403
`origin_not_allowed`, 405 `method_not_allowed`, 409
`import_already_running`, 411 `length_required`, 413 `request_too_large`,
415 `unsupported_media_type`, 503 `database_unavailable`, 503
`server_busy`, 500 `internal_error`.

### `GET /imports/{run_id}`

```json
{"api_schema":"1.0","import":{"analysis_version":"0b51...ea564","cancel_requested":false,
 "counts":{"analyzed":2,"cancelled":0,"complete":2,"failed":0,"orphaned":0,"pending":0,
 "remaining":0,"reused":0,"running":0,"superseded":0},
 "current":null,"failures":[],"finished_at":"2026-09-20T05:21:08Z","phase":"complete",
 "role":"kick","root_label":"Kicks","run_id":"run-1e2c...488b","scan":null,
 "started_at":"2026-09-20T05:21:08Z","state":"complete"}}
```

`state` is exactly #23's run state (`running`, `complete`, `cancelled`,
`interrupted`, `failed`) and `counts` is exactly #23's summary counts. Per-file
failures never change `state`: they appear as `counts.failed > 0` and one
`failures` record each, which keeps #23's outcome that one corrupt sample does
not abort the import visible to the client.

`phase` is `scanning` while #22 reconciles the folder, `analyzing` while #23
drains, and the run's own state once it is terminal. A run this process is not
driving has no observable phase, so it is reported with its #23 state
(`running`) rather than guessed at.

`scan` is the #22 scan summary projected: `root` and `database` are dropped,
every `path` becomes `file_name`, `counts` is passed through verbatim
(including `discovery_errors`) and each per-file record keeps only
`{sample_id, file_name, code, analysis, error_code, stage}`. The human
`message` #22 and #23 attach to a failure is never returned, because it may
quote a local path; the mapping table below is what a client shows instead. The
summary is null when this process did not run the scan, so after a restart the
persisted #23 counts and failures are still reported and the scan counts are
null. Every path in the #23 status and summary output is projected to its
basename: no field of this response holds `path`, `root`, a directory
component or an absolute path.

A run's `role` is the role of the import when this process started it. A run
another process started has no role column to read, so the role is derived from
the run's unfinished items when they agree on one, and is null when they do not.

Documented errors: 400 `invalid_run_id`, 404 `unknown_import`, 403
`host_not_allowed`, 403 `origin_not_allowed`, 405 `method_not_allowed`, 503
`database_unavailable`, 503 `server_busy`, 500 `internal_error`.

### `POST /imports/{run_id}/cancel`

No request body. Sets #23's cancellation flag and returns without waiting for
the run to stop; the run reaches `cancelled` at the next item boundary, and
everything already committed stays readable.

```json
{"api_schema":"1.0","import":{"cancel_requested":true,"run_id":"run-1e2c...488b",
 "state":"running","status_path":"/imports/run-1e2c...488b"}}
```

It also cancels a run another process started, because the flag lives in that
run's row. Cancelling a run that is not live writes nothing and is 409
`import_not_live`; an unknown id is 404 `unknown_import`.

A cancelled run's scan summary is not lost: cancellation takes effect at the
phase boundary, so a run cancelled while it is still scanning finishes that scan
and is cancelled in the drain (the finer bound, cancelling inside the scan
phase, is issue #78, because #22's scan exposes no cancel flag).

Documented errors: 400 `invalid_run_id`, 404 `unknown_import`, 409
`import_not_live`, 403, 405, 411, 415, 503 `database_unavailable`, 503
`server_busy`, 500 `internal_error`.

### `POST /imports/{run_id}/retry`

No request body. Calls #23's `retry_failed` for the current analysis version,
opens a new run and drains pending work on a runner thread.

```json
{"api_schema":"1.0","import":{"retried":1,"run_id":"run-9c02...77aa",
 "status_path":"/imports/run-9c02...77aa"}}
```

`retried` is the number of items #23 reset; `retried: 0` (nothing failed) is a
normal 202, not an error. A live run refuses the request with 409
`import_already_running` before anything is reset, and an unknown id is 404
`unknown_import`. The new run inherits the retried run's `root_label` so a
client can still name the import it is finishing, and its `scan` is null: no
scan ran.

Documented errors: 400 `invalid_run_id`, 404 `unknown_import`, 409
`import_already_running`, 403, 405, 411, 415, 503 `database_unavailable`,
503 `server_busy`, 500 `internal_error`.

### `GET /library/samples`

```text
GET /library/samples?role=bass&role=sub-bass&q=text&limit=50&cursor=CURSOR
```

```json
{"api_schema":"1.0",
 "items":[{"analysis":{"analysis_version":"0b51...ea564","state":"current"},
   "audio":{"channels":1,"duration_ms":500.0,"frame_count":24000,"sample_rate_hz":48000},
   "file_name":"kick-55.wav","file_status":"present","role":"kick",
   "sample_id":"sha256:c951...a0e2"}],
 "page":{"count":1,"has_more":false,"limit":50,"next_cursor":null},
 "query":{"roles":["kick"],"text":null}}
```

`role` may repeat and is OR-ed; every value must be in #9's `ROLES` (else 400
`invalid_role`). `q` is a case-insensitive substring match on the stored file
name only, never on a path, and must be 1 to `MAX_QUERY_LENGTH` = 200
characters (else 400 `invalid_query`). `limit` is the page size, an integer in
`[1, 200]` (else 400 `invalid_page_size`); `limit=0`, `limit=201` and
`limit=1.5` are all refused. Any other parameter is 400
`unknown_query_parameter`. A malformed, non-base64url, wrong-version or
non-`sample_id` cursor is 400 `invalid_cursor`.

`sample_id` is the content identity `sha256:<content_sha256>`, which is the one
identity a stored sample cannot change; the row's own minted `library:...` id
stays inside #21. `file_status` is the stored #21 value (`present`, `missing`
or `unknown`). `analysis.state` is one of `current`, `stale`, `pending`,
`failed` or `absent`, and `analysis.analysis_version` is the stored version
only for `current`. `audio` is the stored `AudioMetadata` projection with
`local_path` dropped, so it holds no path.

The state is decided by one rule, in one order: what is stored beats what is
queued. A sample that stores the current analysis version is `current`; one
that stores another version is `stale`; one with nothing stored and a
`pending` or `running` #23 item is `pending`; one with a `failed` item is
`failed`; anything else is `absent`.

One page is one SQL statement however large it is (`READ_STATEMENT_BUDGET` = 3
is the bound a test asserts against), because the page reads `limit + 1` rows
so `has_more` costs no second statement.

#### Paging and cursors

Ordering is by content identity ascending, which is `sample_id` ascending. The
cursor is:

```text
cursor = base64url(canonical_json({"v": 1, "after": "<sample_id>"}))
```

with `CURSOR_VERSION` = 1 and #9's canonical JSON (sorted keys, compact). The
read is strictly greater than `after`, so a cursor is a position and never a
filter that could re-admit a row outside the request's roles or text, and a
cursor naming a sample that no longer exists is still accepted. `next_cursor`
is non-null exactly when `has_more` is true, and `has_more` means at least one
more matching sample exists. Inserting samples between two page requests cannot
duplicate or skip a row that existed for the whole sequence, and pruning one
simply removes it from the remaining pages. A cursor in the standard base64
alphabet, an unpadded one, one of another version or one naming something other
than a `sha256:` id is refused.

Documented errors: 400 `invalid_role`, 400 `invalid_query`, 400
`invalid_page_size`, 400 `unknown_query_parameter`, 400 `invalid_cursor`,
403, 405, 503 `database_unavailable`, 503 `server_busy`, 500
`internal_error`.

### `GET /library/samples/{sample_id}`

```json
{"api_schema":"1.0","sample":{
 "analysis":{"analysis_version":"0b51...ea564","analyzed_at":"2026-09-20T05:21:08Z",
   "attempts":1,"error_code":null,"state":"current","stored_versions":["0b51...ea564"],
   "stored_versions_truncated":false},
 "audio":{"channels":1,"duration_ms":500.0,"frame_count":24000,"sample_rate_hz":48000},
 "features":{"analysis_version":"0b51...ea564",
   "key":{"confidence":null,"mode":null,"tonic":null,"unavailable_reason":"insufficient_active_frames"},
   "measurements":[{"confidence":0.99,"name":"fundamental","unit":"Hz",
     "unavailable_reason":null,"value":54.999994},
    {"name":"loudness","unit":"LUFS","unavailable_reason":"too_short_for_integrated_loudness","value":null}],
   "schema_version":"1.0"},
 "file_name":"kick-55.wav","file_status":"present","role":"kick",
 "sample_id":"sha256:c951...a0e2"}}
```

This is the only route that returns measurements, and it returns them exactly as
stored. `features` is the stored `backend.contracts.AudioFeatures` projected
field for field: every `MEASURES` name appears exactly once in contract order,
`unit` is the contract unit for that name, and nothing is imputed, normalised,
rounded, renamed or derived. A known value carries its confidence (null where
the contract makes it optional); an unknown value keeps `value: null` with its
stored `unavailable_reason` and carries no `confidence` key at all, because the
contract forbids an unknown value from claiming one. The response adds no
similarity, compatibility, score, warning or ranking field.

`features` is non-null only when `analysis.state` is `current`; for `stale`,
`pending`, `failed` and `absent` the answer is still 200 with `features: null`
and the structured `analysis` object, which is the recoverable answer for an
unavailable analysis. `stored_versions` lists every stored analysis version
ascending, truncated to `MAX_STORED_VERSIONS` = 8 entries with
`stored_versions_truncated: true` when more exist. `analyzed_at`, `attempts`
and `error_code` come from the sample's #23 item row at the current version and
are null when that row does not exist; a sample whose analysis was stored
without a queue row reports `analyzed_at` null rather than inventing one.

A malformed id is 400 `invalid_sample_id`; a well-formed unknown id is 404
`unknown_sample` with `details: {"sample_id"}`. The response carries no
`local_path`, no root-relative path, no pack name and no audio byte.

Documented errors: 400 `invalid_sample_id`, 404 `unknown_sample`, 403, 405,
503 `database_unavailable`, 503 `server_busy`, 500 `internal_error`.

## The closed code tables

### HTTP codes

One code, one status, one constant sentence of at most
`MAX_MESSAGE_LENGTH` = 200 characters. The sentence is path-free, SQL-free and
traceback-free, and `details` carries ids, codes, counts and the fixed names of
request fields only — never a value a caller supplied and never a search text.

| Status | Code | When |
| --- | --- | --- |
| 400 | `invalid_json` | The body is not valid JSON |
| 400 | `invalid_body` | The body is not a JSON object |
| 400 | `missing_field` | A required field is missing |
| 400 | `invalid_field_type` | A field has the wrong type |
| 400 | `unknown_field` | The object carries a field the operation does not accept |
| 400 | `invalid_root` | The import root is not an existing local directory |
| 400 | `invalid_role` | The role is not in #9's `ROLES` |
| 400 | `invalid_run_id` | `run_id` is not `[A-Za-z0-9_-]{1,64}` |
| 400 | `invalid_sample_id` | `sample_id` is not `^sha256:[0-9a-f]{64}$` |
| 400 | `invalid_query` | The search text is not 1 to 200 characters |
| 400 | `invalid_page_size` | The page size is not an integer in `[1, 200]` |
| 400 | `unknown_query_parameter` | The query carries a parameter the operation does not accept |
| 400 | `invalid_cursor` | The cursor is malformed or of another version |
| 403 | `host_not_allowed` | The `Host` header is not the loopback host |
| 403 | `origin_not_allowed` | The `Origin` header is not allowed |
| 404 | `unknown_route` | No operation is served at this path |
| 404 | `unknown_import` | No run has this id |
| 404 | `unknown_sample` | No stored sample has this id |
| 405 | `method_not_allowed` | The path does not accept this method; `Allow` names the ones it does |
| 409 | `import_already_running` | A run is live |
| 409 | `import_not_live` | The run is not live, so cancelling it would write nothing |
| 411 | `length_required` | No valid integer `Content-Length` |
| 413 | `request_too_large` | The body is larger than `MAX_REQUEST_BYTES` |
| 415 | `unsupported_media_type` | A non-GET request without `Content-Type: application/json` |
| 500 | `internal_error` | An unexpected failure; the constant sentence only |
| 503 | `database_unavailable` | #21's open or verify failed; `/health` still answers |
| 503 | `server_busy` | No request slot inside `REQUEST_QUEUE_TIMEOUT_SECONDS`; carries `Retry-After: 1` |

Every non-2xx response is one envelope:

```json
{"api_schema":"1.0","error":{"code":"unknown_route","details":{},
 "message":"No operation is served at this path."}}
```

`details` holds `{"field": ...}` for a body failure, `{"parameter": ...}` for a
query failure, `{"run_id": ...}` or `{"sample_id": ...}` for an unknown id, and
`{"run_id", "state", "phase", "started_at", "status_path"}` for
`import_already_running`.

### Configuration codes

These are exit-2 stderr codes, not responses, so they live beside the command
(`service.CONFIG_CODES`) and not in the HTTP table:

| Code | When |
| --- | --- |
| `invalid_bind_host` | `--host` is not in `ALLOWED_BIND_HOSTS` |
| `invalid_port` | `--port` or `TERA_SERVICE_PORT` is not an integer |
| `port_out_of_range` | The port is outside `[1024, 65535]` and is not the `0` sentinel |
| `database_not_found` | The database parent is missing, the path is a directory, it is not a local path, or the file cannot be opened as SQLite |
| `invalid_port_file` | The port file parent is missing or the path is a directory |
| `invalid_dev_origin` | A `--dev-origin` value is not exactly `http://localhost:PORT` or `http://127.0.0.1:PORT` |
| `port_in_use` | The address is already bound; the line's event is `bind_failed` |

## Numeric bounds

| Bound | Value | Meaning |
| --- | --- | --- |
| `SERVICE_VERSION` | `"0.1.0"` | The service's own version |
| `API_SCHEMA_VERSION` | `"1.0"` | The `api_schema` of every body, success or error |
| `DEFAULT_PORT` | 7391 | The port without `--port` and without `TERA_SERVICE_PORT` |
| `PORT_MIN` / `PORT_MAX` | 1024 / 65535 | The accepted port range |
| `MAX_PAGE_SIZE` | 200 | `PAGE_SIZE_MAX`, the largest page |
| `DEFAULT_PAGE_SIZE` | 50 | The page size without `limit` |
| `PAGE_SIZE_MIN` | 1 | The smallest page |
| `MAX_QUERY_LENGTH` | 200 | The longest search text |
| `CURSOR_VERSION` | 1 | The only cursor encoding version |
| `MAX_STORED_VERSIONS` | 8 | How many stored versions a detail lists |
| `READ_STATEMENT_BUDGET` | 3 | The most statements one page may run |
| `MAX_REQUEST_BYTES` | 65536 | The largest request body, refused before it is read |
| `MAX_MESSAGE_LENGTH` | 200 | The longest error sentence |
| `MAX_CONCURRENT_REQUESTS` | 8 | Requests handled at once |
| `REQUEST_QUEUE_TIMEOUT_SECONDS` | 5 | How long a request waits for a slot |
| `REQUEST_TIMEOUT_SECONDS` | 30 | The socket timeout of one connection |
| `LISTEN_BACKLOG` | 32 | The listen backlog |
| `SHUTDOWN_GRACE_SECONDS` | 5 | The shutdown grace for requests and the runner |

A `--port 0` value is the one accepted value outside `[PORT_MIN, PORT_MAX]`:
issue #27 asks for an ephemeral port in two places ("`--port 0` binds an
ephemeral port reported by the listening line and the port file") and for the
range check in another ("a value outside `[PORT_MIN, PORT_MAX]` is a
configuration error"). The two are reconciled as a sentinel: `0` asks the
operating system for a port, and every other value outside the range is
`port_out_of_range`.

## Access policy

### Loopback only

The listen socket binds an address from `ALLOWED_BIND_HOSTS` = `("127.0.0.1",)`.
`--host` with any other value exits 2 with `invalid_bind_host`, and no flag,
environment variable or request can make the service bind `0.0.0.0`, `::`, a
LAN address or a hostname. On Windows the socket is bound exclusively (no
`SO_REUSEADDR`), because that option would let a second socket take an address
that is already listening and turn `port_in_use` into a silent success.

### Host and Origin

Every request is rejected with 403 `host_not_allowed` unless its `Host` header
is `127.0.0.1` or `localhost`, optionally followed by the bound port. A host
the service did not bind is refused, so a browser page whose own name resolves
to the loopback address cannot reach an operation (DNS rebinding).

A request carrying an `Origin` header is rejected with 403
`origin_not_allowed` unless that exact origin is in `ALLOWED_ORIGINS` =
`("tauri://localhost", "http://tauri.localhost", "https://tauri.localhost")` or
in the `--dev-origin` allowlist. A refused host or origin is refused before any
database read. An allowed cross-origin response echoes that one origin in
`Access-Control-Allow-Origin` and adds `Vary: Origin`; it never sends `*` and
never sends `Access-Control-Allow-Credentials`. An `OPTIONS` request is a
preflight: 204 for an allowed origin and 403 for any other. The service sets no
cookie and no credential header.

### Media type, length and body

Every non-GET request must carry `Content-Type: application/json` (else 415
`unsupported_media_type`) and a valid integer `Content-Length` (else 411
`length_required`), so a cross-site form post cannot reach an operation. That
holds for an operation with no request body too: a `POST` that carries nothing
still names the media type, and `Content-Length: 0` is a valid length. A body
larger than `MAX_REQUEST_BYTES` is refused from its header alone (413
`request_too_large`) and is never read. An operation with no request body
accepts an empty body or `{}` and refuses anything else as `unknown_field`.

### Response headers

Every response, including an error and a preflight, carries
`Content-Type: application/json; charset=utf-8`, an exact `Content-Length`,
`Cache-Control: no-store` and `X-Content-Type-Options: nosniff`. A 405 adds
`Allow`, a 503 `server_busy` adds `Retry-After: 1`, and `POST /imports` adds
`Location`.

### No path, no audio byte, no credential, no outbound network

No response body, response header, error message or access-log line contains an
absolute path, a root-relative path, a directory component other than the single
`file_name` basename or the `root_label` last component, an audio byte, or a
media type other than `application/json`. `backend/api/*` imports no
`backend.intelligence.*` module, reads no `TERA_JEV_*` value and opens no
outbound socket: a test monkeypatches `socket.create_connection` and
`socket.socket.connect` to record any attempt, calls every route and asserts
that every documented response still succeeded and that no connection was
attempted.

## Concurrency, timeouts and shutdown

At most `MAX_CONCURRENT_REQUESTS` = 8 requests are handled at once. A request
that cannot get a slot within `REQUEST_QUEUE_TIMEOUT_SECONDS` = 5 returns 503
`server_busy` with `Retry-After: 1`. The listen backlog is `LISTEN_BACKLOG` =
32 and every connection has a `REQUEST_TIMEOUT_SECONDS` = 30 socket timeout,
after which it is closed without a response body and its slot is released. A
test issues 40 concurrent requests across every route and asserts that each
answer is a documented status, that none is 500, that every response has
complete and length-accurate headers and that the service still answers
`/health` afterwards; another opens a connection, sends a partial request and
asserts the service closes it at the timeout and keeps serving.

`SIGINT` or `SIGTERM` stops the listen socket, lets in-flight requests
complete for up to `SHUTDOWN_GRACE_SECONDS` = 5, requests #23 cancellation for
a live run and joins the runner thread for the same grace, closes the database
connection, removes the port file when `--port-file` was used and exits 0. A
connection that cannot complete inside the grace is closed without a partial
JSON body. A second signal exits 130 at once.

A run that was in flight stays recoverable under #23's lease rules: a restart
reports the persisted `running` or `interrupted` state and invents no scan
summary, and the next `POST /imports` reconciles the stale run and finishes the
remaining work without re-extracting an item that is already complete.

## The access log

One JSON line per request on stdout, written with one `os.write` so two threads
cannot interleave a line:

```json
{"code":null,"duration_ms":1,"event":"request","method":"GET",
 "route":"GET /library/samples","status":200}
```

`route` is the route template of the request's method, so a request for one
sample logs `GET /library/samples/{sample_id}` and never the id. For a path
that matches no template the route is `"<unknown>"`; a method that is not a
token is logged as `"<unknown>"` as well. No query string, search text, sample
id, file name, run id or path appears in the line, so stdout can be shipped to a
log without leaking the library.

stdout carries three kinds of JSON line: this one, the one listening line at
startup, and #23's own per-item progress lines, which the runner thread's drain
prints. Every line is JSON and none carries a path, an id or a file name.

## Failure mapping for #31

#22 and #23 attach a stable `stage` and `code` to every per-file failure and a
human `message` that may quote a local path. The API returns the codes and
never the message; the client shows one sentence per pair. `stage` is null for
a scan record that is not a failure.

| `stage` | `code` | What a client shows |
| --- | --- | --- |
| `read` | `not_found` | The file is no longer where the scan found it. |
| `read` | `access_denied` | The file could not be read; it may be locked or private. |
| `read` | `io_error` | The file could not be read because of a disk error. |
| `read` | `not_file` | The stored path is no longer a regular file. |
| `read` | `source_changed` | The file changed while it was being read; import it again. |
| `decode` | `unsupported_format` | The file is not a WAV file this build can read. |
| `decode` | `unsupported_channels` | The file has more than two channels. |
| `decode` | `empty_audio` | The file holds no audio frames. |
| `decode` | `invalid_audio` | The file's audio data is malformed or truncated. |
| `extract` | `extractor_failure` | The extractor failed on this file; its measurements are missing. |
| `queue` | `sample_missing` | The library no longer holds this file; scan the folder again. |
| `queue` | `content_changed` | The file's bytes changed; the next scan repairs the row. |
| `queue` | `analysis_version_changed` | A newer analysis replaced the queued work. |
| `discovery` | `enumeration_failed` | A folder could not be listed. |
| `discovery` | `entry_unavailable` | A folder entry could not be inspected. |
| null | null | The file was imported without a problem. |

## Reuse and mapping notes for #21, #22 and #23

The service adds no schema, no migration and no second vocabulary. What it
reuses, and the short list of additions:

- #21 (`backend/library/`): `open_database`, `SCHEMA_VERSION`,
  `LibraryRepository`, `transaction(connection)` and `LibraryError`. Three
  reads were added to `repository.py`, under its naming, because the service
  needs them and no landed read serves them:
  `page_samples(analysis_version, *, roles, text, after, limit)` (one page, one
  statement, ordered by content identity), `sample_detail(sample_id)` (one
  sample addressed by its content identity, with its stored versions and its
  #23 item state) and `sample_counts()` (the whole-library counts `/health`
  reports). The same file also gained the content-identity helpers
  `content_identity`/`content_fingerprint` and the closed analysis-state
  vocabulary `ANALYSIS_STATES`, which is how a client-visible state is named
  once.
- #22 (`backend/library/scanner.py`): `validate_root(folder)` (the #22 root
  rules, public so the service validates a `POST /imports` root with exactly
  the command's rules) and `reconcile(root, role, connection, database)` (the
  scan without its command: it prints nothing, because the scan summary carries
  the root's path and stdout is the access-log channel). `run` now calls both,
  so the command and the service cannot disagree about what a scan is.
- #23 (`backend/library/queue.py`): `live_run(connection)`, the liveness rule
  `open_run` refuses with (state `running` and a heartbeat younger than
  `LEASE_SECONDS`), added as one statement so `/health` can report whether an
  import is live without writing; `open_run` itself now calls it, so the rule
  is written once. The runner uses `worker.run_queue` unchanged, so an import
  behaves exactly like the standalone worker command.
- #9 (`backend/analysis/batch.py`): `ROLES`, `canonical` (every stdout and
  response line), `analysis_descriptor`/`digest` (through
  `indexer.current_analysis_version()`), and `local_path` for the
  `--database` and `--port-file` path rules, which is the same call #21 and
  #23 use for theirs.
- No dependency is added. Transport is `http.server.ThreadingHTTPServer`,
  `json`, `urllib.parse`, `base64`, `socketserver`, `signal`,
  `threading`, `tempfile` and `os`; `socket` and `http.client` appear in
  the tests only.

Two behaviours are worth naming because they are easy to misread:

- One handler-side connection serves every request, on the one thread that
  opened it (a SQLite connection belongs to its thread). The runner thread opens
  its own. That is what keeps a page's cost at the statements it runs instead of
  a database open per request; #21's WAL plus its 5 s busy timeout is what keeps
  readers from waiting for an import's writer.
- A run this process drives reports its phase from the in-memory registry
  (bounded to the newest `REGISTRY_LIMIT` = 32 runs, because the registry is a
  convenience, not a contract). A run another process drives, or one that has
  fallen out of the registry, is reported from its #23 row alone: no scan, no
  role unless its items agree on one, and its own state as its phase.

## Recorded conflicts and deviations

Issue #27's body conflicts with the landed layers in two places. Neither is
silently resolved: the landed behaviour is what the code does, and this is the
record.

1. **A three-channel file never becomes a failed job item.** The acceptance
   criterion says: "a terminal `state: "complete"` with `counts.complete`
   equal to the number of readable files, `counts.failed` equal to the failing
   file count and one `failures` record with `code: "unsupported_channels"`".
   The landed #22 scanner refuses a header wider than two channels while it
   scans — `read_wav_header` returns `unsupported_channels` for
   `channels > 2` — and `_Scan._unsupported` records it as an `unsupported`
   scan record with "No library row is created, changed or queued for an
   unsupported file". #23 therefore never sees the file and the run's
   `counts.failed` stays 0 with an empty `failures` array. The service reports
   the landed classification instead: the file appears in
   `scan.files` with `code: "unsupported"`, `error_code:
   "unsupported_channels"` and `stage: "read"`, and `counts.unsupported` is 1.
   Owner: #22's scan classification (`backend/library/scanner.py`); making the
   criterion literal would mean queueing a file #22 has no row for, which is a
   second write path and a new issue.
2. **`file_status` has three stored values, not four.** The criterion says
   "`file_status` is the stored #21 value (the `present`, `missing`,
   `unknown`, `unreadable` vocabulary [#24] and [#25] consume)". #21's schema
   constrains the column to `CHECK (file_status IN ('present', 'missing',
   'unknown'))`, and `library-storage.md` and `feature-retrieval.md` record
   `unreadable` as "not stored today". The route returns the stored value and
   never invents a fourth one; a client that needs the availability mapping
   finds it in #25's `library/retrieval.py`, where `unreadable` is the
   forward-compatible case. Owner: #21's storage contract.

Two further choices are the service's own, recorded so a reader does not have to
infer them:

- A run this process did not start reports `phase: "running"` (its #23 state)
  rather than guessing between `scanning` and `analyzing`, and its `role` is
  derived from its unfinished items when they agree on one and is null when they
  do not.
- A retry run inherits the retried run's `root_label` and has `scan: null`,
  because no scan runs for it.

## Reproduction commands

```text
uv run python -m backend.api.service --database DB.sqlite3 --port 0 --port-file port.json
uv run python -m backend.library.scanner FOLDER --role kick --database DB.sqlite3
uv run pytest tests/test_api_contracts.py tests/test_api_policy.py tests/test_api_library.py tests/test_api_imports.py
uv run pytest
```

The service tests are self-contained: every root, file name and fingerprint is
synthetic and written under a temporary directory, no audio file outside that
directory is read, and the subprocess tests redirect the child's stdout and
stderr to files rather than pipes.

## Local verification

Recorded on the machine this issue was implemented on (Windows, Python 3.13,
the project interpreter `.venv\\Scripts\\python.exe` from the repository
root). Every command below was run in this order, and the summary lines are
copied from what was printed.

```text
> uv run pytest tests/test_api_contracts.py tests/test_api_policy.py tests/test_api_library.py tests/test_api_imports.py
126 passed in 173.56s (0:02:53)
# and again after this section was added, to confirm it reproduces:
126 passed in 174.15s (0:02:54)

> uv run pytest
4 failed, 2117 passed, 1 skipped in 510.98s (0:08:30)
```

The four failures are the four sandbox-blocked `CreatePipe` failures recorded
in this repository's baseline (`tests/test_batch.py::test_cli_empty_and_invalid_inputs`,
`tests/test_batch.py::test_cli_fresh_and_resume`,
`tests/test_evaluation_manifest.py::test_cli_build_validate_and_synthetic_shortfall`,
`tests/test_evaluation_prepare.py::test_preparation_idempotence_source_preservation_and_collisions`),
each a `PermissionError: [WinError 5]` from `subprocess.py`. They are not
caused by this change: the baseline is `4 failed, 1991 passed, 1 skipped`, so
the delta is the 126 API tests added here, with no new failure and the same one
skip.

### Probe 1: one page is one SQL statement

```text
> python -c "import sys,tempfile; sys.path.insert(0,'.'); from pathlib import Path; from tests.api_client import build_library, insert_samples, start_service; d=Path(tempfile.mkdtemp()).resolve(); root=build_library(d,{'kicks':{'k.wav':b'never read'}}); db=d/'library.sqlite3'; insert_samples(db,200,directory=root/'synth'); s=start_service(str(db)); seen=[]; s.app.database.run(lambda c: c.set_trace_callback(seen.append)); r=s.client.get('/library/samples?limit=200'); s.app.database.run(lambda c: c.set_trace_callback(None)); print('items=%d has_more=%s statements=%d' % (len(r.body['items']), r.body['page']['has_more'], len(seen))); s.stop()"

{"code":null,"duration_ms":4,"event":"request","method":"GET","route":"GET /library/samples","status":200}
items=200 has_more=False statements=1
```

The `set_trace_callback` is installed from the connection's own thread (a
SQLite connection belongs to the thread that opened it), and the line above is
the whole cost of a 200-sample page: one statement, inside the documented
`READ_STATEMENT_BUDGET` = 3, and the same number for a 5-sample page. Re-running
the command reproduces it exactly.

### Probe 2: a three-channel file is a scan refusal, not a failed job

```text
> python -c "import sys,tempfile; sys.path.insert(0,'.'); from pathlib import Path; from tests.api_client import build_library, scan, three_channel; d=Path(tempfile.mkdtemp()).resolve(); root=build_library(d,{'kicks':{'wide.wav':three_channel()}}); s=scan(root/'kicks', d/'library.sqlite3', 'kick'); print('files=%s counts=%s' % ([(r['code'],r['error_code'],r['stage']) for r in s['files']], {k:s['counts'][k] for k in ('discovered','unsupported','queued_analysis')}))"

files=[('unsupported', 'unsupported_channels', 'read')] counts={'discovered': 1, 'unsupported': 1, 'queued_analysis': 0}
```

This is the evidence for the first recorded conflict above: #22 refuses the
header while it scans and queues nothing, so the run's `counts.failed` is 0 and
the refusal is reported through `scan.files`. Re-running the command
reproduces it exactly.

### Machine limitations

- The sandbox refuses a subprocess whose stdout is a pipe
  (`PermissionError: [WinError 5]`), so the subprocess tests here redirect the
  service's stdout and stderr to files under `tmp_path`, and the `.venv`
  interpreter's own `uv run` cannot be used to capture a child. Every command
  above was run with the project interpreter directly.
- The sandbox's temporary directory is addressed by its 8.3 short name
  (`...\\GIFTM~1.YUJ\\...`), while `Path.resolve()` expands it. A probe that
  builds a root with `tempfile.mkdtemp()` without resolving it makes #9's
  `local_path` disagree with the path it is handed, which the snapshot reports
  as `source_changed`. That is a property of this machine's temp path, not of
  the service; the probes above resolve the directory, and pytest's `tmp_path`
  is already resolved.
- The `.venv\\Scripts\\python.exe` on this machine re-executes the base
  interpreter, so a child's `os.getpid()` differs from the pid `Popen` reports.
  The shutdown test therefore asserts that the listening line names a live
  process other than the test's, not that it equals `Popen.pid`.
- A console control event (`CTRL_BREAK_EVENT`) does reach a child started with
  `CREATE_NEW_PROCESS_GROUP` here, so the shutdown and second-signal tests run
  rather than skip. The service accepts `SIGBREAK` as well as `SIGINT` and
  `SIGTERM` for exactly that reason; no case was skipped.
- The loopback socket, the in-process `ThreadingHTTPServer` and `http.client`
  round trips all work here, so every route is exercised over a real socket as
  well as through its handler, and the subprocess test drives a real child over
  HTTP.
