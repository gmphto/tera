# Local folder scanning and reconciliation

This document is the scan policy for the local sample library behind the
kick-to-bass MVP (issue #22, plan step 9). One command keeps the SQLite index
current with a chosen local folder: it enumerates the folder, hashes each
candidate snapshot, gives every file exactly one reconciliation code, updates the
stored path rows and prints an import summary an operator can act on.

Identity, digest, snapshot and path conventions come from
[`batch-analysis.md`](batch-analysis.md); the stored rows, their tables, the
migration mechanism and the error base come from
[`library-storage.md`](library-storage.md) (issue #21). Nothing here invents a
second schema, a second migration mechanism, a second error base or a second
fingerprint scheme.

## Command and exit codes

```text
uv run python -m backend.library.scanner FOLDER --role {kick,bass,sub-bass} \
    --database DB.sqlite3 [--summary OUT.json]
```

`FOLDER` must be a local directory: a UNC path, a mapped network drive, a
directory reached through a symbolic link or junction, a missing path and a
non-directory are all refused. `--role` is required, applies to this run only
and is never inferred from a file or folder name; the vocabulary is
`backend.analysis.batch.ROLES`. `--database` is a database created by the
migrations in `backend.library.schema`: pending migrations are applied, and a
file that is not SQLite, a corrupt database or one whose stored schema version is
newer than the module knows is refused, never reset. `--summary` is optional;
the summary JSON is always printed to stdout, one canonical line, and the same
object is written atomically to `--summary` when given.

| Exit | Meaning |
| --- | --- |
| 0 | Finished with no per-file or discovery error (including an empty folder) |
| 1 | Finished with one or more per-file or discovery errors |
| 2 | Invalid command, root, database or summary; unsupported schema version |
| 130 | Interrupted; every committed file is committed |

A record is an error for this purpose when its code is `inaccessible` or
`unsupported`, and every discovery error is one. `missing` is a normal
reconciliation outcome — the operator deleted or moved the file — and does not
change the exit code, so a repeat scan after a deletion still exits 0.

## Scan policy and `SCAN_POLICY_VERSION`

`backend/library/scanner.py` defines `SCAN_POLICY_VERSION = "library-scan-v1"`
and one traversal function, `traverse(root)`, returning
`(wav_paths, discovery_errors, linked_paths)`:

- regular files whose suffix is `.wav` ignoring case, recursively;
- POSIX relative paths in deterministic lexical order;
- symbolic links, Windows junctions and every other reparse point are never
  entered, never imported and never given a row — their relative paths come back
  in `linked_paths` and their count is `counts.skipped_linked`;
- a nested directory that cannot be enumerated is a discovery error
  (`enumeration_failed`, or `entry_unavailable` for a stat failure) and the walk
  continues; a root that cannot be enumerated refuses the whole run before any
  write;
- nothing outside the canonical root is ever visited, and directories named
  `*.wav` are ignored rather than treated as failures.

The policy is #9's policy: `tests/test_library_scanner.py` asserts that for the
same fixture tree `wav_paths` and `discovery_errors` are identical to
`backend.analysis.batch.discover(root)`, so the two cannot drift.

Bump `SCAN_POLICY_VERSION` for any change to what the scan *selects* or *skips*:
the suffix rule, the recursion, the ordering, the link policy, the discovery
error vocabulary or the pre-filter rules below. Do not bump it for a change to
storage, to the summary schema or to a message; those are versioned by
`scan_schema` and by the code tables. A bump is a fact about the stored index,
so the value travels in every summary and in the documentation, never in the
database.

## Format policy and the no-decode boundary

An entry whose suffix is not `.wav` (ignoring case) and a directory named
`*.wav` are ignored and are not failures.

A regular `.wav` file whose byte snapshot is shorter than 12 bytes, or does not
carry `RIFF` at offset 0 and `WAVE` at offset 8, is reported with
reconciliation code `unsupported` and error code `unsupported_format`: no
library row is created or changed and nothing is queued. `snapshot_format_code`
is that pre-filter, and a conformance test asserts every snapshot it refuses is
also refused by `backend.audio.load_wav_bytes`, so the pre-filter can never
accept a file the reader rejects and can never reject one it accepts. For a
snapshot under 12 bytes the reader reports the narrower `invalid_audio`; both
refuse it.

The scanner never decodes audio. It reads the snapshot only with
`backend.analysis.batch.snapshot` and hashes it; a test monkeypatches
`backend.audio.load_wav_bytes`, `backend.audio.load_wav` and every
`measure_*` function in `backend.analysis.loudness`, `spectral`,
`transient` and `harmony` to fail if a scan calls them, and a full scan still
reconciles.

### The two documented limits of this version

The version-1 schema (#21, `library-storage.md`) stores four audio metadata
columns per row and constrains two of them. Two behaviours the criteria of #22
describe are impossible against it, and the scanner reports the honest outcome
instead of writing a row it cannot justify:

1. **A duplicated content identity cannot get its own row.**
   `samples.content_sha256` is `NOT NULL UNIQUE`, so the second path holding the
   same bytes is refused with `UNIQUE constraint failed: samples.content_sha256`
   (probe: two rows with one content identity). The scan therefore reports the
   reconciliation code `duplicate` with the content identity as its
   `sample_id`, leaves the holding row's path, role, availability and features
   exactly as they were, and writes nothing for the second path. Two rows for one
   content identity — and the same rule for a second root — need a migration
   that drops that UNIQUE constraint.
2. **A channel layout wider than two channels cannot be stored.** `samples`
   requires `channels IN (1, 2)`, so a three-channel file that passes the
   pre-filter is reported as `unsupported` with the reader's own
   `unsupported_channels` code rather than indexed under a false channel count.

For the same reason the four metadata columns are filled from the RIFF header of
the snapshot the scan hashed — the chunk headers only, never sample data, never
the reader. A file whose header carries no usable format/data pair is
`unsupported` for the same reason: no storable metadata, no honest row. Deeper
reader failures on files whose header *is* storable (`empty_audio`,
`invalid_audio`) are analysis-time errors owned by #23: the scan indexes those
files and queues them, and #23 records the failure.

## Content identity and analysis version

- The content fingerprint is the lowercase 64-hex SHA-256 of the exact byte
  snapshot returned by `backend.analysis.batch.snapshot(path)`; the content
  identity is `"sha256:" + fingerprint`. The scanner computes both with
  `hashlib.sha256` over that snapshot and defines no second fingerprint scheme.
- The analysis version is
  `backend.analysis.batch.digest(backend.analysis.batch.analysis_descriptor())`,
  imported, never re-declared.
- A row's `sample_id` is a *record* identity, not a content identity: the
  scanner mints `"library:" + digest({"library_path": path_key})` once, at
  insert, and never rewrites it. `samples` requires a `sha256:` id to spell its
  own `content_sha256`, so a content-addressed record could not survive the edit
  that criterion `modified` describes, and a palette reference to it could not
  stay valid. The summary reports the *content* identity in its `sample_id`
  field.

## Reconciliation codes

One code per file, from a closed set, decided in this order:

| Code | Meaning | Stored effect | `analysis` |
| --- | --- | --- | --- |
| `inaccessible` | The entry was enumerated but its byte snapshot failed | An existing row is kept and marked unavailable; no row is created for a new path | `none` |
| `unsupported` | The snapshot failed the pre-filter, or its header cannot supply storable metadata | No row is created or changed | `none` |
| `unchanged` | A row matches this path with the same content identity | Nothing, unless the stored availability was not `present` (a returning file), which is restored | `current` or `queued` |
| `modified` | A row matches this path with a different content identity | That row's content identity and metadata are updated; row identity, path, role, `imported_at` and the previous content's analysis are kept | `current` or `queued` |
| `moved` | The same content identity was held by a row this run did not enumerate, or the stored path differs from the enumerated one only by case or Unicode form | The existing row's path is updated, its identity and features are kept, availability is restored and nothing is queued by the move | `current` or `queued` |
| `duplicate` | The content identity is already held by a row at another path, under this root, under another root or by another file in this scan | Nothing (see the documented limit above); the holding row is untouched | `current`, `queued` or `none` |
| `added` | No row matches this path and no row holds this content identity | One row is inserted for (root, path, role, content identity) with no analysis | `queued` |
| `missing` | A row inside the root was not enumerated this run and enumeration completed | The row is kept with its identity, path, role, content identity and features, and its availability is set unavailable | `none` |

Precedence and pairing rules:

- `inaccessible` beats `moved`: a row whose file was enumerated but unreadable
  is never treated as missing and never re-paired to another path. A *new* path
  holding that content is `duplicate`, not `moved`.
- A path match beats a content match: the same bytes at the same path are
  `unchanged` or `modified`, never `moved` or `duplicate`.
- When one scan yields both new paths and rows that were not enumerated with the
  same content identity, they are paired one-to-one in normalised-relative-path
  order: the pairs are `moved`, the leftovers are `added` or `missing`. The same
  pairing is produced on every run of the same tree.
- The `missing` sweep runs only after every enumerated file was reconciled. An
  interrupted run skips it, so no row that a later read would have reconciled is
  marked missing.
- The code an `unchanged` or `moved` record gets for a case-only rename is
  `moved`, never `added` plus `missing`, and the stored path keeps the casing
  that is on disk.

### Analysis state

`analysis` is derived, never recorded:

- `current` — a stored analysis exists for that content identity at the current
  analysis version;
- `queued` — no analysis for the current version exists and the content identity
  is available, so it appears in `pending_analysis`;
- `none` — the file is unavailable (`missing`, `inaccessible`), unsupported, or
  the content identity is held only by unavailable rows.

Queueing is expressed only as the derived query
`backend/library/indexer.py: pending_analysis(connection)`, which returns one
`AnalysisRequest(sample_id, fingerprint, analysis_version, path, role)` per
content identity lacking a current analysis, ordered by
`(analysis_version, sample_id)`, choosing the lexicographically smallest
normalised path when several rows share a content identity. Rows whose availability
is `missing` are not requested, because #23 cannot read them; a scan that finds
the file again restores availability and the row returns to the queue by itself.
The scanner writes no job row, no attempt and no progress, and starts nothing:
running, retrying and recording a failed analysis belong to #23.

## Error codes and stages

`inaccessible` keeps the original stable code from the reader or the snapshot:
`not_found`, `access_denied`, `io_error`, `not_file` or `source_changed`.
`not_found` covers a file that disappeared between enumeration and read, and
`source_changed` covers size, mtime, ctime or byte changes detected while the
snapshot was read — a torn or renamed-while-reading file is never stored under
another byte sequence's identity, and the next scan retries it.

| Stage | Used by |
| --- | --- |
| `read` | `inaccessible` (the snapshot or the header read) and `unsupported` |
| `discovery` | `missing` and every `discovery_errors` record |

A discovery error is `{"path": <root-relative POSIX path>, "error": {"stage":
"discovery", "code": "enumeration_failed" | "entry_unavailable", "message": ...}}`,
the shape `backend.analysis.batch` uses for its manifest.
`inaccessible`, `unsupported` and `missing` records carry `error_code`,
`stage` and a human `message` naming the local cause; every other code carries
`null` there, except `duplicate`, which carries an informational message and a
null `error_code`.

## Summary schema 1.0

| Field | Meaning |
| --- | --- |
| `scan_schema` | Literal `1.0` |
| `scan_policy_version` | `SCAN_POLICY_VERSION` at the time of the run |
| `root` | Canonical absolute local root |
| `role` | The explicit role of this run |
| `database` | Canonical absolute local database path |
| `analysis_version` | The current analysis digest |
| `state` | `complete` or `interrupted` |
| `counts` | `discovered`, `skipped_linked`, one count per reconciliation code, `queued_analysis`, `discovery_errors` |
| `files` | One record per file whose code is not `unchanged`, in normalised-path order |
| `discovery_errors` | #9-shaped discovery error records |

Each `files` record has exactly `path` (root-relative POSIX), `code`,
`analysis` (`queued`, `current` or `none`), `sample_id` (the content identity,
or `null` for a path with no row), `error_code`, `stage` and `message`.

`counts.queued_analysis` is not "work queued by this run": it is the size of the
derived queue after the run, so an identical scan of an unchanged tree reports
the same number and makes `pending_analysis` return the same set.

## Case-insensitive paths

Path matching uses a normalised, case-insensitive form —
`NFC` then `casefold` over the root-relative POSIX path — while the stored path
preserves the casing on disk. Two states cannot be repaired automatically:

- one directory genuinely holds two entries whose normalised paths collide
  (possible on a case-sensitive volume, and on any volume for two names that
  differ only by Unicode normal form): the scan fails with exit 2 *before any
  write*, names both paths and imports neither;
- two library rows inside the scanned root have paths that normalise equal: the
  scan fails with exit 2 before any write, names both rows, and never picks one
  or merges them.

Both are manual-repair states: rename one entry, or repair the rows by hand
before scanning again.

## Repository operations

The scanner writes through `backend.library.repository`, never with its own SQL
against a library table. Where #21 already exposed an operation it is used
unchanged; the gaps it does not cover are added to that file:

| Need | Operation |
| --- | --- |
| Content lookup by fingerprint | `find_path_records_by_content(content_sha256)` for rows without analysis (new); `find_by_content` needs a complete analysis and raises `incomplete_features` for a scan-created row |
| Path lookup scoped to a root | `list_path_records(root)` (new); `find_sample_by_path(path)` is the single-path form and needs a complete analysis |
| Insert of a path row | `insert_path_record(path, *, role, content_sha256, sample_rate_hz, channels, frame_count, duration_ms, file_status, sample_id)` (new) |
| Content-identity update | `update_path_record_content(sample_id, *, content_sha256, ...)` (new) |
| Availability update | `mark_file_status(sample_id, file_status)` (existing, reused) |
| Path update for a move | `relocate_path_record(sample_id, path, file_status)` (new) |
| Current-analysis check at a version | `has_current_analysis(content_sha256, analysis_version)` (new) |
| Derived queue | `backend.library.indexer.pending_analysis(connection)` (new module) |

`LibraryPathRecord` is the row-level read those operations return. Every write
runs in exactly one `transaction`, so an interruption or a failure can never
leave a half-applied row; the connection comes from
`backend.library.schema.open_database`, never from a hand-rolled
`sqlite3.connect`.

## Recovery procedure

Every failure state is cleared by fixing the local cause and re-running the
identical command; nothing was deleted in the meantime.

| Record | Local cause to fix |
| --- | --- |
| `inaccessible` with `access_denied` | Release the lock or grant read rights, then re-run; an existing row stays unavailable until then |
| `inaccessible` with `not_found` | The file was removed while the scan ran; put it back or ignore the record, which will be `missing` on the next scan |
| `inaccessible` with `source_changed` | The file was being written; re-run once the writing process has finished. A file whose bytes were replaced after the snapshot keeps the identity of the bytes that were hashed |
| `unsupported` | Delete or move the bad file, or convert it to a supported RIFF/WAVE layout; no row is affected |
| `missing` | Restore the file at its stored path, or leave it: the row keeps its identity, role, content identity and features so a palette reference can still resolve to a recoverable record |
| `duplicate` | Nothing to repair: the bytes are already in the library at another path (see the documented limit; the second path is not indexed by this version) |

An interrupted run leaves the state a later run continues from: re-running the
identical command re-reports the files that were already committed as
`unchanged` with `queued` or `current`, finishes the rest, and reaches the same
library state as an uninterrupted run. A hard kill leaves the database readable
and resumable, because every write is one transaction.

## What the scanner must not do

It does not decode audio (beyond the header read above), extract features, run or
wait on analysis, call Jev, use the network, write telemetry, write `projects`,
`palettes` or `palette_items`, create or infer packs from folder names, mutate
source files (every open is read-only; no rename, copy, delete or sidecar),
watch the filesystem, or start a long-running service. It returns once
reconciliation is committed. Local paths, fingerprints and summaries stay on the
device, and no test, fixture or committed document carries a real library path or
a byte of real audio.

## Tests

```text
uv run pytest tests/test_library_scanner.py tests/test_library_scan_recovery.py
uv run pytest
```

Both files build synthetic trees with the `wav(...)` helper from
`tests/test_audio.py` under a temporary directory and a temporary database
created by the #21 migrations. `tests/test_library_scanner.py` covers the entry
point and exit codes, the summary schema and its code sets, the traversal
conformance with `batch.discover`, the pre-filter and its conformance with the
reader, the no-decode boundary, ignored entries, `unsupported` snapshots,
reader-rejected files that are still indexed and queued, `added`, `unchanged`,
`modified`, `moved` (including a case-only rename), `duplicate` inside one scan
and across two roots, repeat scans that write no row, the derived queue and the
analysis states, and the untouched table set.
`tests/test_library_scan_recovery.py` covers `missing` and its recovery,
`inaccessible` from a vanishing file, a denied read and a source that changed
while reading, the precedence of `inaccessible` over `moved`, a duplicate of an
unreadable row, interruption and resume, a hard kill inside a write, a subdirectory
enumeration error, both case-collision refusals, and link skipping (which skips
with an explicit reason on a platform that cannot create the link).

## Local verification

Both commands were run from the repository root with the project interpreter.
`uv run` cannot capture a subprocess in this sandbox (it fails with
`PermissionError [WinError 5]` at `_winapi.CreatePipe`), so each run put a
`sitecustomize.py` on `PYTHONPATH`, passed `-p no:cacheprovider` and a
`--basetemp` outside the repository. Every tree and database was temporary.

```text
.venv\Scripts\python.exe -m pytest tests/test_library_scanner.py tests/test_library_scan_recovery.py --basetemp <tmp>\bt-focus3 -p no:cacheprovider -q -rf
  -> 49 passed in 6.90s

.venv\Scripts\python.exe -m pytest tests/test_library_scanner.py tests/test_library_scan_recovery.py --collect-only -q -p no:cacheprovider
  -> 49 tests collected in 0.81s

.venv\Scripts\python.exe -m pytest --basetemp <tmp>\bt-full -p no:cacheprovider -q -rf
  -> 4 failed, 1771 passed, 1 skipped in 459.58s (0:07:39)
```

The baseline at `97c7b5f` before this change was `4 failed, 1722 passed,
1 skipped`, the four failures being the known sandbox-only `PermissionError
[WinError 5]` at `_winapi.CreatePipe`:
`tests/test_batch.py::test_cli_empty_and_invalid_inputs`,
`tests/test_batch.py::test_cli_fresh_and_resume`,
`tests/test_evaluation_manifest.py::test_cli_build_validate_and_synthetic_shortfall`
and
`tests/test_evaluation_prepare.py::test_preparation_idempotence_source_preservation_and_collisions`.
The change adds 49 passing tests (`1771 - 1722 = 49`, matching the 49 collected
in the two new files), the same four sandbox-only failures and no new failure;
the link fixture passed here rather than skipping, so the platform could create
the symbolic link.

One concrete probe result, against the version-1 schema of #21 (`open_database`
on a fresh temporary database, then two inserts with synthetic
`C:/tera-probe/...` paths):

```text
two rows with one content identity -> accepted
second row, same content identity -> refused: UNIQUE constraint failed: samples.content_sha256
three-channel reader metadata     -> refused: CHECK constraint failed: channels IN (1, 2)
unknown rate metadata             -> refused: CHECK constraint failed: sample_rate_hz > 0
```

That refusal is why the duplicate case is reported rather than stored and why a
channel layout wider than two channels is `unsupported` instead of indexed under
a false channel count; see "The two documented limits of this version" above.
