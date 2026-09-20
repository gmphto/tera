# Background analysis jobs

The queue is the second half of the derived-work model #22 introduced. A scan
stores path rows for the local library and writes no job row, no attempt and no
progress; what still needs analysis is the query
`backend.library.indexer.pending_analysis(connection)`. This document describes
the module that materialises that query as bounded, persisted rows and drains
them: `backend.library.queue` holds the queue, `backend.library.worker` holds the
run loop and the command, and the two tables ship as migration 2 in #21's chain.

The job itself is #9's: each item is executed with
`backend.analysis.batch.snapshot(path)` and
`backend.analysis.batch.extract(data, path, role, fingerprint, version)`, and
what comes back is stored through `backend.library.repository`. Nothing in the
queue reimplements discovery, hashing, decoding, extraction or Sample assembly,
and nothing changes `backend/analysis/batch.py`, the extractors,
`backend/audio.py`, `backend/contracts.py` or a table #21 owns.

## Command and exit codes

Run from the repository root:

```text
uv run python -m backend.library.worker --database DB.sqlite3 [--workers N]
                                        [--max-attempts N] [--once] [--run-id RUN_ID]
                                        [--cancel] [--retry-failed] [--summary OUT.json]
```

| Flag | Meaning |
| --- | --- |
| `--database` | The local database. Pending migrations are applied by #21's `open_database`; a database whose stored version is newer, an integrity failure or a non-SQLite file exits 2 and is never reset. |
| `--workers N` | Extraction threads, 1..`MAX_WORKERS` (`4`). Anything else exits 2. Default `DEFAULT_WORKERS` = 1. |
| `--max-attempts N` | Attempts before an item is `failed`, 1..10. Anything else exits 2. Default `MAX_ATTEMPTS` = 3. |
| `--once` | Process only the work derived at start: the largest `item_id` after the first `enqueue` is the claim horizon, so work a scan queues mid-run waits for the next run. |
| `--run-id RUN_ID` | Adopt a run the caller opened with `queue.open_run` (`--workers`/`--max-attempts` are then the caller's). An unknown or non-running id exits 2. This is the path #27 uses to hand work to a service. |
| `--cancel` | Flag every live run for cancellation and exit 0 without extracting. |
| `--retry-failed` | Reset this version's `failed`/`orphaned` items to `pending` (attempts 0, error and run cleared) before draining. |
| `--summary OUT.json` | Write the same object that the last stdout line holds, atomically, to a local file. |

There is no `--role`: an item is analysed with the role the scan stored for its
path.

Exit codes are #9's:

| Code | Meaning |
| --- | --- |
| 0 | Finished; no per-item error records. |
| 1 | Finished; one or more items ended `failed`, `orphaned` or `superseded` (each is a record in the summary's `failures`, exactly as #9 records a refusal per file). |
| 2 | Invalid command, database, schema, worker count or attempt count, an unknown `--run-id`, another run holds a live lease, or a storage failure. |
| 130 | Cancelled or interrupted. Ctrl-C in the worker sets the same flag as `--cancel`. |

The run summary JSON is always the last line printed to stdout, even when the
run failed or was cancelled. Before it, the CLI prints one line per item that
reached a final state, reusing #9's keys:

```text
{"state": <run state>, "analysis_version": <64 hex>, "completed": n, "failed": n,
 "remaining": n, "analyzed": n, "reused": n, "pending": n, "running": n,
 "cancelled": n, "orphaned": n, "superseded": n}
```

With `--cancel` the printed object is the cancellation acknowledgement
(`{"queue_schema", "queue_policy_version", "state": "cancel_requested", "runs"}`),
not a run summary.

## Where the work comes from

One item is one *content identity* and one analysis version:

- `enqueue(connection, analysis_version)` calls #22's
  `pending_analysis(connection)` and takes only the requests at that version. It
  writes one `pending` row per request with
  `INSERT ... ON CONFLICT (sample_id, analysis_version) DO NOTHING`, then revives
  a `cancelled` row for the same key to `pending` with attempts 0 and run and
  error cleared. Every other existing state is left untouched.
- `job_items.sample_id` is the request's content identity,
  `sha256:<fingerprint>`. The migration's CHECK enforces that shape, so the
  expected fingerprint is always the id's suffix; `UNIQUE(sample_id,
  analysis_version)` bounds the queue by the library, not by runs. The stored
  path and role are evidence of what was queued; the `samples` row for the
  content identity is authoritative about where the file is now and which role it
  carries, and it is resolved again before the snapshot and again inside the
  commit transaction.
- Duplicate calls, a second connection, a scan running during a drain and a
  second worker therefore cannot create a second item for one key. History rows
  (`cancelled`, `failed`, `superseded`, `orphaned`) stay until #74 prunes them.

`job_items` has **no foreign key** to `samples`. Referential integrity is the
worker's explicit `sample_missing` check, so a concurrent sample delete can
neither cascade queued work away nor make ordinary queue writes raise. `run_id`
does not reference `job_runs` either: items outlive the run that touched them,
and reconcile clears the claim without deleting history.

## Queue policy and `QUEUE_POLICY_VERSION`

`QUEUE_POLICY_VERSION = "library-jobs-v1"`. Bump it when a rule a reader of an
older run's rows could misread changes: a new item or run state, a new retry
classification, a new claim or cancellation rule, or a different summary meaning.
Do not bump it for a fix that leaves every stored row's meaning intact.

## Migrations and the two tables

The queue tables are migration 2 of `backend.library.schema.MIGRATIONS`, so
`SCHEMA_VERSION` is 2 and `PRAGMA user_version` is 2. The migration adds tables
and indexes only: no column, constraint or index of a version-1 table is created,
altered or dropped, and no row is rewritten, so upgrading a database that already
holds samples and features keeps every row identical. Reapplying the migrations
after a run leaves every `job_runs` and `job_items` row identical (id, state,
attempts, `sample_id`, `analysis_version`). The worker never creates or alters a
table at runtime, and it writes raw SQL only for its own two tables.

### job_runs

| Column | Type | Keys and constraints |
| --- | --- | --- |
| `run_id` | TEXT | PRIMARY KEY, non-blank after trim, `run-<32 hex>` |
| `state` | TEXT | NOT NULL, CHECK IN `running`, `complete`, `cancelled`, `interrupted`, `failed` |
| `analysis_version` | TEXT | NOT NULL, 64 lowercase hex |
| `workers` | INTEGER | NOT NULL, CHECK BETWEEN 1 AND 4 |
| `max_attempts` | INTEGER | NOT NULL, CHECK BETWEEN 1 AND 10 |
| `owner_token` | TEXT | NOT NULL, random per process; the lease is refreshed only by its owner |
| `heartbeat_at` | TEXT | NOT NULL, refreshed at least every `HEARTBEAT_SECONDS` while an item is in flight |
| `started_at` | TEXT | NOT NULL |
| `finished_at` | TEXT | NULL exactly while the run is `running` |
| `cancel_requested` | INTEGER | NOT NULL DEFAULT 0, CHECK IN (0, 1) |

### job_items

| Column | Type | Keys and constraints |
| --- | --- | --- |
| `item_id` | INTEGER | PRIMARY KEY (rowid), the FIFO order |
| `sample_id` | TEXT | NOT NULL, `sha256:` + 64 lowercase hex |
| `analysis_version` | TEXT | NOT NULL, 64 lowercase hex |
| `path` | TEXT | NOT NULL, the local path as queued |
| `role` | TEXT | NOT NULL, CHECK IN `batch.ROLES` (generated from `ROLES`) |
| `state` | TEXT | NOT NULL, CHECK IN `pending`, `running`, `complete`, `failed`, `cancelled`, `orphaned`, `superseded` |
| `disposition` | TEXT | NULL, or #9's `analyzed`/`reused`; a `complete` row always carries one |
| `attempts` | INTEGER | NOT NULL, CHECK >= 0, incremented by the claim |
| `run_id` | TEXT | NULL until a run touches the row |
| `claimed_at` | TEXT | NULL until claimed; cleared when a retry or reconcile releases it |
| `finished_at` | TEXT | NULL until a terminal state |
| `error_stage` | TEXT | NULL, or one of `read`, `decode`, `extract`, `queue` |
| `error_code` | TEXT | NULL, or a code from the table below |
| `error_message` | TEXT | NULL, or a human message (stage/code/message are all set or all NULL) |
| `created_at` | TEXT | NOT NULL |

`UNIQUE(sample_id, analysis_version)` is the queue's bound.
`idx_job_items_claim(state, item_id)` supports the claim subselect and
`idx_job_items_run(run_id, state)` supports run-scoped counts and the summary.

## State transitions

Item states and the only ways they change:

| From | To | Who | When |
| --- | --- | --- | --- |
| (no row) | `pending` | `enqueue` | #22 reports a content identity with no current analysis at this version |
| `cancelled` | `pending` | `enqueue` | The same key comes back; attempts 0, run and error cleared |
| `pending` | `running` | `claim` | One conditional statement picks the lowest `item_id`; attempts +1 |
| `pending` | `superseded` | `claim` | The item's `analysis_version` is not the run's; never executed |
| `pending` | `cancelled` | `cancel_pending` | Cancellation was observed; the run claims nothing new |
| `running` | `pending` | `requeue` / `reconcile` | A retryable failure with attempts left, or a claim whose run is gone |
| `running` | `complete` | `finalize` | Features, the analysis version and the disposition committed |
| `running` | `failed` | `finalize` | A terminal code, or the attempt limit is reached |
| `running` | `orphaned` | `finalize` | No `samples` row (or a different content identity) at claim or inside the commit |
| `running` | `superseded` | `finalize` | The bytes no longer hash to the queued identity |
| `running` | `cancelled` | `finalize` | The flag was set while this file was being read or extracted |
| `failed`/`orphaned` | `pending` | `retry_failed` | `--retry-failed` only; attempts 0, error and run cleared |
| any terminal | — | — | Terminal rows never move again in this version |

Run states:

| From | To | Who | When |
| --- | --- | --- | --- |
| (no row) | `running` | `open_run` | Reconciliation found no live owner |
| `running` | `complete` | `run_queue` | The queue drained |
| `running` | `cancelled` | `run_queue` | `--cancel`, Ctrl-C, or cancellation seen between items |
| `running` | `failed` | `run_queue` | A storage failure; committed items stay readable |
| `running` | `interrupted` | `reconcile` | The heartbeat is older than `LEASE_SECONDS` |

## Stages and codes

The stage vocabulary is #9's `{stage, code, message}` shape plus `queue` for the
failures the queue decides itself. A code outside the retryable tuple is terminal
on the first attempt, so a defect cannot produce an endlessly retried item.

| Code | Stage | Class | Raised when |
| --- | --- | --- | --- |
| `not_found` | `read` | retryable | The file disappeared before the snapshot |
| `access_denied` | `read` | retryable | A permission failure while reading |
| `io_error` | `read` | retryable | Any other filesystem failure while reading |
| `source_changed` | `read` | retryable | The snapshot saw the file change while it was read |
| `not_file` | `read` | retryable | The stored path is no longer a regular file |
| `unsupported_format` | `decode` | terminal | The reader refuses the container |
| `unsupported_channels` | `decode` | terminal | The reader refuses a layout wider than two channels |
| `empty_audio` | `decode` | terminal | Zero frames |
| `invalid_audio` | `decode` | terminal | A truncated, malformed or non-finite sample |
| `extractor_failure` | `extract` | terminal | An extractor raised, or the assembled Sample failed `Sample.from_dict` |
| `sample_missing` | `queue` | terminal | No `samples` row for the content identity (or its identity changed) |
| `content_changed` | `queue` | terminal | The bytes hash to another identity than the queued one |
| `analysis_version_changed` | `queue` | terminal | A pending item at a version the run does not own |

`attempts` increments when an attempt starts. A retryable code returns the item
to `pending` while `attempts < max_attempts`; when the limit is reached the item
is `failed` and no later run retries it automatically. `--retry-failed` /
`retry_failed(connection, analysis_version)` is the only way back for a `failed`
or `orphaned` row.

## Numeric bounds

| Name | Value | Meaning |
| --- | --- | --- |
| `DEFAULT_WORKERS` | 1 | Threads when `--workers` is absent |
| `MAX_WORKERS` | 4 | Upper bound the CLI and `open_run` enforce |
| `MAX_ATTEMPTS` | 3 | Default attempt limit |
| `MAX_ATTEMPTS_LIMIT` | 10 | Upper bound `--max-attempts` accepts |
| `LEASE_SECONDS` | 60 | A `running` run whose heartbeat is this old is dead |
| `HEARTBEAT_SECONDS` | 15 | The owner refreshes the heartbeat at least this often |
| `COLLECT_INTERVAL` | 8 | Finalized items between two full collections; bounds the unreachable per-item residue |
| `QUEUE_POLICY_VERSION` | `library-jobs-v1` | The queue rules' version |
| `QUEUE_SCHEMA` | `1.0` | The summary schema literal |

## Runs, leases and reconciliation

- `open_run(connection, analysis_version, workers, max_attempts)` commits one
  `running` row and returns its id **without extracting anything**: it is the call
  a caller makes before handing work to a thread or process, so no UI thread ever
  decodes audio.
- Reconciliation runs first, then the live check. A `running` row whose heartbeat
  is younger than `LEASE_SECONDS` is a live owner: a second process exits 2 with a
  message naming the run id, writes no row and claims nothing.
- `reconcile(connection)` turns a stale `running` run into `interrupted` and
  returns every `running` item whose run is not live to `pending`, keeping its
  attempts and clearing its claim and error. `complete` items, features and
  analysis versions are never touched. It therefore covers both a killed owner
  and a run stopped by a storage failure.
- Because one extraction can outlive the lease, the owner refreshes
  `heartbeat_at` from a separate connection every `HEARTBEAT_SECONDS` while items
  are in flight, and it stops refreshing once it no longer owns the run. A slow
  item is never mistaken for a dead owner.
- A worker killed mid-run (`SIGKILL`, `os._exit`, a power cut) loses at most the
  in-flight item's work: the following run reconciles and finishes every remaining
  item, and already-stored analyses are not re-extracted (its `enqueue` inserts
  nothing for them).

## Cancellation

- `--cancel` sets `cancel_requested = 1` on every live run and exits 0 without
  extracting; Ctrl-C in a worker sets the same flag.
- The worker checks the flag before claiming each item and again after
  `batch.snapshot` and after `batch.extract` returns, before committing. Once it
  is set the run claims nothing new; every not-yet-started `pending` item of its
  version is finalized `cancelled`, the run is finalized `cancelled`, completed
  items and their features stay readable, and the exit code is 130.
- An item whose extraction was abandoned writes no feature and no
  analysis-version row and is finalized `cancelled` — never `complete`, never
  `failed` — with no penalty beyond the started attempt.
- **Granularity:** cancellation takes effect at the next item boundary and, for an
  in-flight file, at the end of that file's snapshot and extraction. The
  extractors are not interruptible without changing #9, so the remainder of one
  file's extraction is the documented bound; tightening it is #73, not this task.
- A later run revives the cancelled rows through `enqueue` and finishes the
  library.

## One transaction per item, none during extraction

Claiming, state transitions and completion are short transactions
(`created_at`/`claimed_at` writes of one row). While `batch.snapshot` and
`batch.extract` run, the worker holds **no** SQLite transaction:
`connection.in_transaction is False` inside an instrumented extractor.

The drain's own write transactions (a claim, a finalize and a commit — never a
snapshot or an extraction) are additionally serialized by one process-local lock,
because SQLite's busy handler does not queue and four workers taking the write
lock in turn can starve one of them into #21's 5 s `busy_timeout`. Connections
stay per-thread and SQLite's single-writer rule still does the serialising; the
measurement is in "Write-lock starvation".

For a successful item, the features for `(sample_id, analysis_version)`, the
analysis-version row and the item's `complete` state commit in one transaction,
so a crash or storage error can never publish features for an item recorded as
anything else. The samples row is checked again inside that transaction: a
concurrent delete orphans the item instead of recreating a row or raising. The
assembled Sample is validated with `backend.contracts.Sample.from_dict` before
storage; an invalid Sample is never written and the item fails with stage
`extract`, code `extractor_failure`.

A storage or write failure stops the run, rolls the item's transaction back,
marks the run `failed`, keeps every previously committed item readable and exits
2. The in-flight item stays `running` until the next reconciliation returns it to
`pending`.

## Reads never wait

Every queue read is an individual statement on a connection from
`schema.open_database` (isolation_level None, WAL, `busy_timeout` 5000 ms), and
every write is short, so a second connection can read `samples`,
`sample_features`, `analysis_versions` and call `pending_analysis` throughout a
run, including while an item is decoding or extracting, without
`database is locked` and without waiting beyond the documented busy timeout.

## The UI-thread rule

`open_run` returns the run id before any byte is read, and the blocking
`run_queue(connection, run_id, workers, max_attempts, once)` is called only by
the `backend.library.worker` process or by an explicitly started, non-UI service
thread that opened its own connection. A caller adopts a run it opened with
`--run-id`; #27 owns the service and endpoint integration, and no request
handler, UI event or import API call may call `run_queue` inline.

## Progress and summary

`status(connection, run_id=None)` answers from any connection with SQL aggregates
only:

```text
{"run_id", "state", "analysis_version", "workers", "started_at",
 "counts": {"pending", "running", "complete", "failed", "cancelled", "orphaned",
            "superseded", "remaining"},
 "current": {"sample_id", "path", "attempts"} or null}
```

Without `run_id` the newest run is reported; with no run at all the run fields
are null and the counts still describe the whole queue. `current` is the lowest
`item_id` this run has running. `remaining` is `pending + running`.

`progress(connection, run_id)` returns `{state, analysis_version, counts}` — the
`counts` object above and nothing else — and is what the per-item CLI line
reads. It exists because a summary's `failures` array holds one record per
*unfinished* item, so building a summary once per finished item would make the
line cost the run's remaining queue instead of the item that finished. Measured
on a 200-item queue: 2 937 bytes for one `progress` call against 136 415 bytes
for the summary (200 records at 683 bytes each). Both read the counts through
the same helper, so a progress line and the final summary cannot disagree.

`summary(connection, run_id)` returns:

| Field | Meaning |
| --- | --- |
| `queue_schema` | Literal `1.0` |
| `queue_policy_version` | `QUEUE_POLICY_VERSION` at the time of the run |
| `run_id`, `analysis_version`, `state`, `workers`, `max_attempts`, `started_at`, `finished_at` | The run row |
| `counts` | One number per item state, plus `analyzed`, `reused` and `remaining` |
| `failures` | One record per non-`complete` item: `{sample_id, path, role, stage, code, message, attempts}`, in item-id order |

The counts are the run's own rows: an item claimed or superseded by this run
(`run_id` is this run) plus unclaimed `pending` work at this run's version. A
finished run therefore reports exactly its persisted rows, and a run stopped
early reports the work it left behind in `pending`. The last stdout line and the
`--summary` file hold the same object; the file is written with #9's checkpoint
pattern (same-directory temporary file, flush, fsync, close, `os.replace`).

## Repository operations

The worker writes samples, features and analysis versions only through
`backend.library.repository`; raw SQL is used only for `job_runs` and
`job_items`. Where #21 already exposed an operation it is used unchanged:

| Need | Operation |
| --- | --- |
| Content lookup by fingerprint, including unanalysed rows | `find_path_records_by_content(content_sha256)` (existing) |
| Current-analysis check at a version | `has_current_analysis(content_sha256, analysis_version)` (existing) |
| Analysis-version registration | `register_analysis_version(descriptor)` (existing, reused by the store path) |
| Sample removal in tests and repair paths | `delete_sample(sample_id)` (existing) |
| **Storing an analysis for an existing path row** | `store_analysis(sample, *, content_sha256, sample_id=None, descriptor=None)` (**new**, the one operation #23 adds) |

`store_analysis` writes, inside the **caller's** transaction (it issues no BEGIN
and no COMMIT, and refuses to run outside one), the version's
`analysis_versions` row when `descriptor` is given, the 19 `sample_features`
rows and the `sample_keys` row for the named `samples` row — and no column of
`samples` itself. It exists because #21's `import_sample` writes the whole
`samples` row (path, filename, `file_status`, `updated_at`) and needs a
registered version at call time, while a queue item must store an analysis it
measured from a row that a scan already owns, in the same transaction that
records the item's `complete` state. It raises `invalid_sample`,
`invalid_content_identity` (the row no longer stores that content),
`unknown_sample` (the row is gone — the worker records `sample_missing`),
`unknown_analysis_version`, `analysis_version_mismatch` or `write_failed`.

The item's identity and the row's identity are different on purpose: a scanned
row's `sample_id` is minted from its path by #22 (`library:<digest>`), while #9's
extractor assembles a Sample whose identity is the content identity. The worker
resolves the row by content identity and names it in `store_analysis`, so
features are addressed by the record that will read them. Which bytes a stored
feature row was measured from is still not recorded: that is **#166**.

## Bounded memory

The drain's footprint is the work in flight, not the queue.

- One item's snapshot bytes, decoded arrays and assembled `Sample` are locals of
  one step of one worker thread; nothing but the item's row outlives the item,
  and the worker keeps no per-item list, cache or result.
- A progress line reads counts (`queue.progress`), never the summary's
  `failures` array; the array is built once, by the run's final `summary`.
- The one thing an item does leave behind is *unreachable* memory: validating the
  assembled `Sample` goes through #9's `backend/contracts.py`, whose
  `Model.__post_init__` and `Model.from_dict` re-evaluate every field annotation
  on every construction (`typing.get_type_hints`), so each call leaves reference
  cycles. Measured on this machine: `Sample.from_dict(payload)` keeps 6 308 bytes
  per call alive and `Sample.to_dict()` 1 946, all of it returned only by a
  generation-2 collection — minor collections reclaim none of it (a
  `gc.collect(0)` or `gc.collect(1)` per item leaves the peak growing ~4.4 KB per
  item). The drain therefore completes a full collection every
  `COLLECT_INTERVAL` finalized items: the residue one drain can hold is bounded
  by the interval instead of by the queue's length, and the collection runs
  between items, from the thread that finished one, with no claim and no
  transaction open. The measurement is in "Local verification"; the allocation
  itself is #9's file and out of this issue's set.

## What the worker must not do

It must not decode on a calling thread, block library reads, hold a transaction
while extracting, run more than `MAX_WORKERS` extractions at once, materialise
the queue or all features, re-measure a stored analysis, extract a file whose
bytes changed, write a `samples` row, change an owned table, retry a terminal
failure forever, prune history, or let the process's footprint grow with the
number of items it has drained. `queue.py` and `worker.py` import only the
standard library and `backend.audio`, `backend.analysis.*` and
`backend.library.*`: no `backend.intelligence`, no Jev credential or
configuration, no socket, no telemetry, no remote service.

Source audio is opened read-only and is never copied, renamed, deleted or
accompanied by a sidecar; the only files written are the SQLite database (with
its WAL/SHM sidecars) and the optional `--summary` file. Paths, fingerprints and
summaries are private local data: committed tests and documents use synthetic
paths under `tmp_path` and synthetic audio built by the `wav(...)` helper, and
nothing from a real library is sent anywhere.

## Documented limits and follow-ups

- **Wide channel layouts and per-path rows (#167).** The frozen schema stores one
  or two channels, so a three-channel file cannot be indexed by a scan and no
  queue item can exist for it. A decode-time `unsupported_channels` failure can
  therefore only be exercised on a row inserted for that content identity
  directly, which is what the queue test does. #167 owns both the wider layouts
  and several rows per content identity; `_resolve` already prefers the row whose
  own id is the item's identity and otherwise the smallest stored path.
- **A stored analysis is not bound to the bytes it measured (#166).** Features are
  addressed by `(sample_id, analysis_version)` and nothing records the fingerprint
  they came from; #22 invalidates the current version's rows when it repairs a
  content change, and this queue refuses to extract bytes that no longer hash to
  the given identity, which is the limit of what the schema can express today.
- **The residual per-item growth is #22's `pending_analysis` tuple.** `enqueue`
  must call `indexer.pending_analysis(connection)` (this issue's criterion 5) and
  that function returns every pending request at once: 513 bytes per request,
  103 KB for a 200-item queue, which is the 503 bytes of peak per item measured in
  "Bounded memory". Streaming it is a change to `backend/library/indexer.py`,
  which this issue's file set excludes; the criterion's 2x bound holds at 200
  items (1.15x-1.22x) and would eventually be exceeded by a queue several times
  larger, so a streaming `pending_analysis` is the follow-up if a library of that
  size needs the same bound.
- **The unreachable cycles an item leaves are #9's contract layer's.** `contracts
  .Model.__post_init__` and `Model.from_dict` re-evaluate every field annotation
  on every construction (`typing.get_type_hints`), so each construction leaves
  reference cycles that only a generation-2 collection returns: measured 6308
  bytes per `Sample.from_dict(payload)` call and 1946 per `Sample.to_dict()`.
  `COLLECT_INTERVAL` bounds what a drain accumulates, at the cost of completing a
  collection every 8 items; caching the evaluated hints in `backend/contracts.py`
  would remove the allocation itself, and that file is outside this issue's set.
- **Finer cancellation (#73)** and **pruning queue history (#74)** are out of
  scope here, as are the service start policy (#27), watching folders (#70),
  pruning unavailable records (#71) and editing a role after import (#72).
- **`_docs/library-storage.md` is #21's document, and this issue edits exactly
  the statements its migration 2 made false.** `202fcba` changed no other
  sentence: line 73 reads `currently `2``, lines 98-105 say "exactly these eight
  tables" and name migration 2 (`job_items`, `job_runs`) as the addition, line
  311 says "eight-table set" and the migration probe block records
  `user_version: 2` with the eight tables in full. The remaining "six tables"
  sentence at line 100 is correct — it describes what migration 1 created. Every
  other part of that document stays #21's, and nothing here needs a follow-up.
- A run exits 1 when any of its items ended `orphaned` or `superseded`, not only
  `failed`: a superseded item is a record in `failures`, and #9's contract
  defines exit 1 as "finished with one or more per-item errors".

## Tests

```text
uv run pytest tests/test_library_queue.py tests/test_library_worker.py tests/test_library_jobs_recovery.py
uv run pytest
```

All three files build synthetic trees with the helpers in `tests/test_audio.py`
under `tmp_path` and temporary databases created by #21's migrations; each case
asserts both the SQLite state and the printed summary or CLI output.

`tests/test_library_queue.py` covers the migration and its closed vocabularies
(the role CHECK against `batch.ROLES`, the state/stage/disposition CHECKs, the
column sets, the indexes), the upgrade of a version-1 database and the
row-identity of a re-migration, `enqueue` idempotence across calls and
connections (including a scan during the run), the cancelled-row revival, the
FIFO claim and its attempts, six concurrent claimers, the `--once` horizon, one
live run per database, the stale-lease and lost-lease reconciliation, the cancel
flag, `retry_failed`, the attempt limit, the `status`/`summary` schemas and their
counts, and `store_analysis` (its transaction guard, the row-identity write, the
missing row and the unregistered version).

`tests/test_library_worker.py` covers the command, its exit codes and parameter
bounds, an unsupported schema with zero writes, `--cancel`, reuse without
extraction (`snapshot`/`extract` patched to fail), a commit that loses the race
to another writer, a corrupt or unreadable sample that fails alone (empty,
non-finite, truncated, wide layout, denied read) with its stages and codes, the
retry limit and `--retry-failed`, a content change and a version change, progress
observed from another connection while a real second process drains (a
subprocess with its stdout on a file: this machine's sandbox refuses piped
stdio), reads and writes during a blocked extraction, `in_transaction` being
False inside the extractor, the in-flight bound of `--workers`, peak memory for
a 200-item run (200 files holding distinct content, asserted from the scan's own
counts) against a 20-item run, a progress line that reads counts instead of the
summary's records, the collection interval that bounds the per-item unreachable
residue, the caller keeping the run id while another process extracts, no Jev
module, no socket and an untouched library tree, and the import surface of both
modules.

`tests/test_library_jobs_recovery.py` covers a killed owner (a real process
terminated mid-run) and the lease takeover that finishes the library, a slow item
that keeps a young heartbeat while a second process is refused, cancellation
mid-file and mid-queue from a second connection with finished work still
readable, a sample row removed before the claim and between the claim and the
commit, a storage failure that stops the run and keeps committed items readable,
and a scan during a drain with `--once`.

## Local verification

Every command below was run from the repository root with the project
interpreter. `uv run` cannot capture a subprocess in this sandbox (it fails with
`PermissionError [WinError 5]` at `_winapi.CreatePipe`), so each run put a
`sitecustomize.py` on `PYTHONPATH`, passed `-p no:cacheprovider` and a
`--basetemp` outside the repository. The `sitecustomize.py` drops `os.mkdir`'s
mode argument, because a directory created with mode `0o700` cannot afterwards be
listed or written into in this sandbox (`PermissionError [WinError 5]`) and
pytest creates its basetemp and `tmp_path` directories exactly that way. Every tree and database was temporary and
synthetic, and no test in the three new files skipped. The interpreter was
CPython 3.13.5 on Windows with SQLite 3.47.1 (the atomic claim uses `RETURNING`).

```text
.venv\Scripts\python.exe -m pytest tests/test_library_queue.py tests/test_library_worker.py tests/test_library_jobs_recovery.py --basetemp <tmp>\bt-focused -p no:cacheprovider -q -rf
  -> 52 passed in 41.66s

.venv\Scripts\python.exe -m pytest tests/test_library_worker.py -k peak_memory --basetemp <tmp>\bt-memory -p no:cacheprovider -q
  -> 1 passed in ~22s; 7 of 8 runs passed and one ended with the run's storage failure before the write lock below, 10 of 10 after it

.venv\Scripts\python.exe -m pytest tests/test_library_schema.py tests/test_library_repository.py tests/test_library_scanner.py tests/test_library_scan_recovery.py tests/test_library_queue.py tests/test_library_worker.py tests/test_library_jobs_recovery.py --basetemp <tmp>\bt-seven -p no:cacheprovider -q -rf
  -> 161 passed in 48.72s

.venv\Scripts\python.exe -m pytest --basetemp <tmp>\bt-full -p no:cacheprovider -q -rf
  -> 4 failed, 1827 passed, 1 skipped in 363.38s (0:06:03)
```

The baseline at the reviewed parent commit `2321cfc` is `4 failed, 1775 passed,
1 skipped`; the reviewed issue commit `202fcba` was `4 failed, 1825 passed, 1
skipped` with `50 passed` in the focused files. This round adds the two tests
recorded above (`1827 - 1825 = 2`, `52 - 50 = 2`) and keeps the same four known
sandbox-only failures, all `PermissionError [WinError 5]` at
`_winapi.CreatePipe`:
`tests/test_batch.py::test_cli_empty_and_invalid_inputs`,
`tests/test_batch.py::test_cli_fresh_and_resume`,
`tests/test_evaluation_manifest.py::test_cli_build_validate_and_synthetic_shortfall`
and
`tests/test_evaluation_prepare.py::test_preparation_idempotence_source_preservation_and_collisions`;
the single skip is unchanged. The four files that carry #21's and #22's landed
expectations were re-run because the migration chain now reaches version 2, and
they stay green (161 passed with the three queue files).

One concrete probe result, against a fresh synthetic database (`open_database` on
a temporary file, then two synthetic 480-frame tones under `tmp_path`):

```text
fresh database user_version         -> 2
items inserted by first enqueue     -> 2
items inserted by second enqueue    -> 0
item identities are the content ids -> True 2
second item for the same key        -> refused: UNIQUE constraint failed: job_items.sample_id, job_items.analysis_version
role outside batch.ROLES            -> refused: CHECK constraint failed: role IN ('kick', 'bass', 'sub-bass')
second run while the first is live  -> refused: Another analysis run is live on this database: run-<32 hex> (heartbeat <stamp>)
after the lease expires              -> {'runs': 1, 'items': 0} then True
```

### Bounded memory

The bounded-memory claim was re-measured at this HEAD. The method is the
committed test's — `tracemalloc` around `worker.main` only (building and scanning
the library are outside the measured region), `--workers 4`, one untraced 2-item
warm-up, the 20-item run drawn before the 200-item one — and every file holds
distinct bytes, so the 200-file library really queues 200 content identities
(`added: 200`, `duplicate: 0`). The block recorded for `202fcba` was wrong on
exactly that point: its 200 files came from 40 tones, so the scan stored 40 rows
with 160 `duplicate` entries and the "200-item" run it measured was a 40-item one
(a re-drive of that library: `added: 40`, `duplicate: 160`, 3.75 s, peak
873197 bytes, against 18.5 s for 200 distinct items). QA's independent re-drive of
`202fcba` with distinct contents measured 505115 -> 1084417 bytes (2.147x) and
499311 -> 1098542 bytes (2.200x), 2.38x with `--workers 1` and 2.035x with fresh
processes.

```text
scratch probe (a temporary script under the session temp directory, never in the
repository; the committed test above is the durable version of it):
run 1       20 items -> 600408 bytes peak in 1.83s (236879 bytes still live)
            200 items -> 691036 bytes peak in 17.12s (47255 bytes still live)
            ratio 1.15x; 503 bytes of peak per item over the 180 extra items
run 2       20 items -> 576747 bytes; 200 items -> 702306 bytes; ratio 1.22x
fresh processes, no warm-up:
            20 items -> 616892 bytes in 1.90s
            200 items -> 720977 bytes in 18.14s and 701931 bytes in 17.87s (1.17x, 1.14x)
with COLLECT_INTERVAL disabled (the released behaviour for this residue):
            20 items 636510 -> 200 items 1103240 bytes, 1.73x, 2593 bytes per item

queue.progress(connection, run_id)     -> 2937 bytes ({state, analysis_version, counts})
queue.summary(connection, run_id)      -> 136415 bytes over 200 records (683 bytes per record)
indexer.pending_analysis(connection)   -> 102529 bytes over 200 requests (513 bytes per request)
```

What the numbers separate:

- One progress line is 2937 bytes and does not depend on the queue's length. The
  summary's 683-byte record is built once per run — the `failures` array is empty
  for a clean run and holds one record per non-`complete` item otherwise.
- The remaining per-item peak growth is #22's `pending_analysis` tuple: 513 bytes
  x 200 requests = 103 KB, materialised once by `enqueue`, which is the measured
  503 bytes per extra item. `enqueue` must call that function (#22's interface,
  this issue's criterion); streaming it is a change to `indexer.py`, which this
  issue's file set excludes, and is recorded here as the residual.
- Everything else an item leaves is bounded by `COLLECT_INTERVAL`: the ~4.4 KB of
  reference cycles one item's contract validation creates (6308 bytes for one
  `Sample.from_dict(payload)`, 1946 for one `Sample.to_dict()`, all of it
  reclaimed only by a generation-2 pass) is reclaimed every 8 items, so it
  contributes a constant to the peak rather than growth.

### Write-lock starvation

The same 200-item drain exposed a second, independent fact, measured with the
drain's own `transaction` wrapper: a transaction holds SQLite's write lock for
0.01-0.15 s, but writers that lost the race waited 2.0-5.0 s at
`BEGIN IMMEDIATE` (the 5 s `busy_timeout` #21 sets), and 3 of 8 instrumented
runs ended `database is locked` with items left `pending`. SQLite's busy handler
does not queue, so a loser can miss every round. The drain now serializes its own
write transactions with one process-local lock (`_WRITE_LOCK`), which turns the
race into a queue; the committed memory test went from 7 of 8 runs passing to 10
of 10, and the un-instrumented concurrency cases are unchanged.

The subprocess cases (progress polling, `--run-id` adoption, the killed owner and
the lease takeover) start a real second process with its stdout redirected to a
file rather than a pipe; that is the only reason the CLI is not exercised through
`uv run` above.
