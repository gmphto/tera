# Local sample and analysis storage

This document is the storage model for the local SQLite library behind the
kick-to-bass MVP (issue #21, plan sections 9 and 10, Phase 1 step 9). The
database is a single local file owned by the device. It holds sample identity,
versioned analysis results, musical key, tags and pack identity, and it holds
nothing else: no audio bytes, no Jev payload, no credential, no network state.

Identity, digest and local-path conventions come from
[`batch-analysis.md`](batch-analysis.md): a sample is identified by its
`sample_id` in the contract, its content by the SHA-256 of the exact source
bytes, and an analysis version by the digest of its canonical analysis
descriptor.

## Database location

`schema.default_database_path()` returns the absolute local path of the
database:

| Condition | Path |
| --- | --- |
| `TERA_LIBRARY_DB` is set | the absolute form of that environment variable |
| Windows | `%LOCALAPPDATA%\tera\library.sqlite3` |
| Other platforms | `$XDG_DATA_HOME/tera/library.sqlite3`, else `~/.local/share/tera/library.sqlite3` |

`schema.open_database(path)` is the only way to obtain a connection. It
resolves the path with `backend.analysis.batch.local_path`, so a UNC path, a
mapped network drive, a linked ancestor and a directory are all refused, then
creates the parent directory when it is missing. Nothing else in the library
layer opens a file for writing.

The database is local-only and is never committed. `.gitignore` carries
`*.sqlite3`, `*.sqlite3-wal` and `*.sqlite3-shm` so a database, its WAL and
its shared-memory file cannot enter the repository, and every test uses a
temporary directory.

## Connection policy

`open_database` returns a connection with `isolation_level = None`
(explicit transaction control) and `row_factory = sqlite3.Row`, and applies
these pragmas in this order:

| Pragma | Value | Why |
| --- | --- | --- |
| `foreign_keys` | `ON` | Every declared reference is enforced on every connection. |
| `busy_timeout` | `5000` | A competing writer is waited for, then reported as `database_locked`. |
| `journal_mode` | `WAL` | Readers never block the writer and see only committed state. |
| `synchronous` | `FULL` | A committed import survives power loss. Set after WAL, which resets it. |

Before any pragma is applied, the file is checked: a non-empty file that does
not begin with the SQLite header is refused with `not_a_database` and left
byte-for-byte unchanged, and a header-valid file that fails
`PRAGMA integrity_check` is refused with `database_corrupt`.

## Transaction lifecycle

- Every public write method runs inside exactly one `transaction(connection)`
  block, which issues `BEGIN IMMEDIATE`, commits on success and rolls back on
  any exception before re-raising it. A failed write therefore leaves no
  partial `samples`, `sample_features`, `sample_keys` or `sample_tags` row.
  `transaction` is public so callers can group their own statements.
- Reads are individual statements. Under `isolation_level = None` each one
  runs in its own short read transaction, so no transaction is ever held
  between calls and a reader never blocks a writer or reports
  `database_locked`.
- A write that cannot take the lock within `busy_timeout` raises
  `database_locked`; the same write succeeds once the holder commits.
- Library code never nests transactions; a second `BEGIN` inside an open one
  is a programming error.

## Migrations

`schema.SCHEMA_VERSION` is the version this module knows (currently `1`) and
`schema.MIGRATIONS` is an ordered tuple of `(version, script)` pairs, each
script holding one or more SQL statements.

`open_database` calls `schema.migrate(connection)` on every new connection,
so the database is always at the current version by the time a caller sees it.
`migrate(connection, migrations=MIGRATIONS)`:

1. Sorts the migrations by version ascending and refuses a duplicate version
   (`migration_failed`).
2. Reads `PRAGMA user_version` **before any DDL**. A stored version newer than
   the highest available migration raises `schema_version_newer` and nothing
   is created, altered or dropped, so an unknown future database is never
   reset.
3. Applies every migration whose version is above the stored version, each one
   in its own `BEGIN IMMEDIATE` transaction that also stores the new
   `user_version`. Any failure rolls the whole migration back and raises
   `migration_failed` with the sqlite3 error as `__cause__`.
4. Returns the resulting `user_version`.

Calling `migrate` again with the same sequence is a no-op: no version change
and no row change.

## Tables

The user-table set is exactly these six tables. `PRAGMA user_version` is
`SCHEMA_VERSION`, and no table, column or index outside this list is created.
`projects`, `palettes`, `palette_items`, `compatibility_scores`,
`recommendation_outcomes` and `decision_model_versions` belong to later
issues and do not exist here.

### analysis_versions

| Column | Type | Keys and constraints |
| --- | --- | --- |
| `analysis_version` | TEXT | PRIMARY KEY, 64 lowercase hex, `digest(descriptor)` |
| `descriptor` | TEXT | NOT NULL, non-empty, `canonical(descriptor)` JSON |
| `created_at` | TEXT | NOT NULL, `YYYY-MM-DDTHH:MM:SSZ` from `schema.utc_now()` |

### sample_packs

| Column | Type | Keys and constraints |
| --- | --- | --- |
| `pack_id` | TEXT | PRIMARY KEY, non-blank after trim |
| `name` | TEXT | NOT NULL, non-blank after trim |
| `vendor` | TEXT | NULL, or non-blank after trim |
| `created_at` | TEXT | NOT NULL, set once and kept across updates |

### samples

| Column | Type | Keys and constraints |
| --- | --- | --- |
| `sample_id` | TEXT | PRIMARY KEY, the contract identity |
| `schema_version` | TEXT | NOT NULL, CHECK `= '1.0'` |
| `content_sha256` | TEXT | NOT NULL, UNIQUE, 64 lowercase hex |
| `role` | TEXT | NOT NULL, CHECK IN `kick`, `bass`, `sub-bass` |
| `original_path` | TEXT | NOT NULL, the local path exactly as imported |
| `path_key` | TEXT | NOT NULL, UNIQUE, `os.path.normcase(os.path.abspath(original_path))` |
| `filename` | TEXT | NOT NULL, final segment of `original_path` |
| `pack_id` | TEXT | NULL, REFERENCES `sample_packs(pack_id)` ON DELETE SET NULL |
| `file_status` | TEXT | NOT NULL, CHECK IN `present`, `missing`, `unknown` |
| `sample_rate_hz` | INTEGER | NOT NULL, CHECK `> 0` |
| `channels` | INTEGER | NOT NULL, CHECK IN `1`, `2` |
| `frame_count` | INTEGER | NOT NULL, CHECK `>= 0` |
| `duration_ms` | REAL | NOT NULL, CHECK `>= 0` |
| `imported_at` | TEXT | NOT NULL, set on the first import and never rewritten |
| `updated_at` | TEXT | NOT NULL, rewritten by every later write to the row |

Table CHECK: a `sample_id` that spells `sha256:...` must equal
`'sha256:' || content_sha256`. Index `idx_samples_role` on `role`.

### sample_features

| Column | Type | Keys and constraints |
| --- | --- | --- |
| `sample_id` | TEXT | NOT NULL, REFERENCES `samples(sample_id)` ON DELETE CASCADE |
| `analysis_version` | TEXT | NOT NULL, REFERENCES `analysis_versions` ON DELETE RESTRICT |
| `measurement` | TEXT | NOT NULL, CHECK IN the 19 `MEASURES` names |
| `unit` | TEXT | NOT NULL, CHECK IN `Hz`, `ms`, `BPM`, `linear`, `ratio`, `LUFS`, `normalized` |
| `value` | REAL | NULL |
| `unavailable_reason` | TEXT | NULL |
| `confidence` | REAL | NULL, CHECK BETWEEN 0 AND 1 |

PRIMARY KEY (`sample_id`, `analysis_version`, `measurement`). Table CHECK:
`(value IS NULL) = (unavailable_reason IS NOT NULL)`, mirroring
`backend/contracts.py`. Index `idx_sample_features_analysis` on
`analysis_version`. The measurement and unit vocabularies are generated from
`backend.contracts.MEASURES`, so they cannot drift from the contract.

### sample_keys

| Column | Type | Keys and constraints |
| --- | --- | --- |
| `sample_id` | TEXT | NOT NULL, REFERENCES `samples(sample_id)` ON DELETE CASCADE |
| `analysis_version` | TEXT | NOT NULL, REFERENCES `analysis_versions` ON DELETE RESTRICT |
| `tonic` | TEXT | NULL, CHECK IN the 12 contract tonics |
| `mode` | TEXT | NULL, CHECK IN `major`, `minor` |
| `confidence` | REAL | NULL, CHECK BETWEEN 0 AND 1 |
| `unavailable_reason` | TEXT | NULL |

PRIMARY KEY (`sample_id`, `analysis_version`). Table CHECK: an unknown key has
`tonic`, `mode` and `confidence` all NULL with a non-blank reason; a known key
has all three set and no reason. `AudioFeatures` carries the key outside
`measurements`, so this is the one table added beyond plan section 10; it is
versioned per analysis version like every other feature.

### sample_tags

| Column | Type | Keys and constraints |
| --- | --- | --- |
| `sample_id` | TEXT | NOT NULL, REFERENCES `samples(sample_id)` ON DELETE CASCADE |
| `tag` | TEXT | NOT NULL, CHECK `tag = lower(trim(tag))` and length 1..64 |
| `added_at` | TEXT | NOT NULL, set once per tag |

PRIMARY KEY (`sample_id`, `tag`).

## LibraryRepository

`LibraryRepository(connection)` wraps one connection from `open_database`.

| Method | Inputs | Returns | Raises |
| --- | --- | --- | --- |
| `register_analysis_version(descriptor)` | any canonically serialisable value | the 64-hex version | `invalid_sample`, `analysis_version_mismatch`, `database_locked`, `write_failed` |
| `import_sample(sample, *, content_sha256, pack_id=None, tags=(), file_status="unknown")` | a `Sample` or its wire dict | `ImportResult` | `invalid_sample`, `invalid_content_identity`, `invalid_tag`, `invalid_file_status`, `unknown_analysis_version`, `duplicate_content`, `path_conflict`, `database_locked`, `write_failed` |
| `get_sample(sample_id, analysis_version=None)` | id, optional version | `StoredSample` or None | `unknown_analysis_version`, `incomplete_features`, `analysis_version_mismatch` |
| `list_samples(role=None)` | optional role | tuple of `StoredSample` | `incomplete_features`, `analysis_version_mismatch` |
| `list_analysis_versions(sample_id)` | id | tuple of versions, ascending | `analysis_version_mismatch` |
| `find_sample_by_path(path)` | any path | `StoredSample` or None | `incomplete_features`, `analysis_version_mismatch` |
| `find_by_content(content_sha256)` | 64-hex hash | `StoredSample` or None | `incomplete_features`, `analysis_version_mismatch` |
| `mark_file_status(sample_id, file_status)` | id, status | None | `invalid_file_status`, `unknown_sample`, `database_locked`, `write_failed` |
| `delete_sample(sample_id)` | id | bool | `database_locked`, `write_failed` |
| `add_tag(sample_id, tag)` | id, tag | bool | `invalid_tag`, `unknown_sample`, `database_locked`, `write_failed` |
| `remove_tag(sample_id, tag)` | id, tag | bool | `invalid_tag`, `unknown_sample`, `database_locked`, `write_failed` |
| `list_tags(sample_id)` | id | tuple of tags, ascending | `unknown_sample` |
| `upsert_pack(pack_id, name, vendor=None)` | pack identity | None | `write_failed`, `database_locked` |
| `set_pack(sample_id, pack_id)` | id, pack id or None | None | `unknown_sample`, `write_failed`, `database_locked` |

`ImportResult`, `StoredSample` and `transaction` are module-level and frozen:

```python
ImportResult(sample_id, analysis_version, created, path_changed, file_status)
StoredSample(sample, analysis_version, pack_id, tags, file_status, imported_at, updated_at)
```

`StoredSample.sample` is a validated `backend.contracts.Sample`, never a dict,
and its measurements are rebuilt in `MEASURES` order so
`stored.sample.to_dict()` equals the imported `Sample.to_dict()` exactly,
including unknown measurements and their `unavailable_reason` values.

Behaviour worth stating explicitly:

- Identity. `sample_id` plus `content_sha256` identify a record. Importing the
  same id and content from a second path updates `original_path`, `path_key`,
  `filename` and `updated_at` in place and returns
  `created=False, path_changed=True`; `imported_at` and every feature row stay.
  The same content under a second `sample_id` raises `duplicate_content`, which
  names the stored `sample_id`. A different content on an occupied `path_key`
  raises `path_conflict`. The same id with a different content hash raises
  `invalid_content_identity`.
- Versions. Registering a second descriptor gives a second version. Features
  are stored per version and a newer import never deletes or overwrites an
  older one. `get_sample(id)` without a version picks the version with the
  greatest `analysis_versions.created_at`, ties broken by `analysis_version`
  ascending. `get_sample(id, version)` for a version with no stored features
  under that id raises `unknown_analysis_version`; for an unknown id it
  returns None.
- Completeness. Every stored (`sample_id`, `analysis_version`) has exactly the
  19 `MEASURES` rows and one key row. A read that finds fewer raises
  `incomplete_features` rather than returning a partial `Sample`. Import
  re-writes all 19 rows, so re-importing repairs a truncated version.
- Audio. Import never opens, stats or reads the audio file. An import for a
  file that has disappeared succeeds with `file_status` `unknown`, and
  `mark_file_status(sample_id, "missing")` records the loss without deleting
  any row. `list_samples` is a database read only.
- Tags. `add_tag` trims outer whitespace, lowercases, is idempotent and
  returns False when the tag already exists, leaving `added_at` untouched;
  `remove_tag` returns True then False; `list_tags` returns them sorted.
- Packs. `upsert_pack` creates or updates and keeps `created_at`;
  `set_pack(sample_id, None)` clears the link; deleting a `sample_packs` row
  sets the linked `samples.pack_id` to NULL through `ON DELETE SET NULL`
  instead of deleting samples. A pack that is not registered raises
  `write_failed`, because the code set has no pack-specific code.
- Deletion. `delete_sample` removes the sample and, through `ON DELETE
  CASCADE`, its features, key and tags. Registered analysis versions and packs
  survive.

## Error codes

`backend/library/errors.py` defines one `LibraryError` base carrying a
`.code` string and one subclass per code. Callers branch on the code, not on
the message.

| Code | Raised by |
| --- | --- |
| `invalid_database_path` | `open_database`: directory, non-local or remote path, unreadable file, unusable directory, or a database that cannot be opened for writing |
| `not_a_database` | `open_database`: a non-empty file without the SQLite header |
| `database_corrupt` | `open_database`: a SQLite header file that fails `verify` |
| `schema_version_newer` | `migrate`: stored `user_version` above every available migration |
| `migration_failed` | `migrate`: malformed migration sequence, or a migration that raised and rolled back |
| `database_locked` | any write: another connection held the write lock past the busy timeout |
| `write_failed` | any write: the database refused it (for example a CHECK or trigger); the sqlite3 error is `__cause__`. Also `upsert_pack` validation and `set_pack` to an unregistered pack |
| `invalid_sample` | `import_sample`: payload failed `Sample` validation or disagrees with `MEASURES`; `register_analysis_version`: descriptor is not canonically serialisable |
| `invalid_content_identity` | `import_sample`: `content_sha256` is not 64 lowercase hex, a content-addressed `sample_id` disagrees with it, or the stored id already carries another hash |
| `duplicate_content` | `import_sample`: another `sample_id` already stores this content hash |
| `path_conflict` | `import_sample`: another `sample_id` already stores this `path_key` |
| `unknown_analysis_version` | `import_sample`: version is not registered; `get_sample`: the id has no stored features under that version |
| `analysis_version_mismatch` | any read of an `analysis_versions` row whose stored descriptor is not canonical JSON re-digesting to its own version |
| `incomplete_features` | any read that needs a complete version and finds fewer than 19 measurements or no key row |
| `unknown_sample` | `mark_file_status`, `add_tag`, `remove_tag`, `list_tags`, `set_pack` for an id with no `samples` row. `delete_sample` returns False instead of raising |
| `invalid_tag` | `add_tag`, `remove_tag`, `import_sample`: tag is not a string, or is empty or longer than 64 characters once trimmed |
| `invalid_file_status` | `import_sample`, `mark_file_status`: status is not `present`, `missing` or `unknown` |

## What is never stored

- No audio bytes and no BLOB column anywhere; the only audio-derived values are
  the contract measurements and key, plus `content_sha256`.
- No library path outside `samples.original_path`, `samples.path_key` and
  `samples.filename`. No `backend/library` code opens an audio file.
- No Jev decision, Jev payload, model prompt, credential, endpoint, telemetry,
  encryption key or evaluation data. Issue #26 owns decision caching.
- No network access of any kind.

## Tests

```bash
uv run pytest tests/test_library_schema.py tests/test_library_repository.py
uv run pytest
```

Both files use temporary databases only. `tests/test_library_schema.py` covers
the fresh and zero-byte database, migration idempotence, refusal of a
non-database file with its bytes unchanged, refusal of a newer schema version
with every row unchanged, corrupt databases, an upgrade that preserves rows, a
failing migration that rolls back, the connection pragmas, `verify` reporting a
foreign-key violation, `default_database_path` honouring the override, and the
six-table set with no BLOB column. `tests/test_library_repository.py` covers
the round trip of `silent-sample.json` and of both `hybrid.json` samples,
coexisting analysis versions, the deterministic default version, duplicate
content on one id moving the path, duplicate content on another id being
refused, path conflicts, invalid content identity, unregistered versions,
rollback on a mid-write failure, the `transaction` context manager, incomplete
features, tag normalisation and uniqueness, the pack link and its
`ON DELETE SET NULL` behaviour, a missing file keeping its rows, cascading
deletion, a reader seeing committed state only, the second writer timing out,
and an import that never opens or stats the audio file.

## Local verification

Both commands were run from the repository root with the project interpreter.
`uv run pytest` cannot capture a subprocess in this sandbox (it fails with
`PermissionError [WinError 5]` at `_winapi.CreatePipe`), so each run put a
`sitecustomize.py` on `PYTHONPATH`, passed `-p no:cacheprovider` and a
`--basetemp` outside the repository. Every database was a temporary file.

```text
.venv\Scripts\python.exe -m pytest tests/test_library_schema.py tests/test_library_repository.py --basetemp <tmp>\bt-focus -p no:cacheprovider -q -rf
  -> 56 passed in 6.47s

.venv\Scripts\python.exe -m pytest --basetemp <tmp>\bt-full -p no:cacheprovider -q -rf
  -> 4 failed, 1722 passed, 1 skipped in 363.84s
```

The baseline measured in this workspace under the same sandbox before commit
`3369304` was `4 failed, 1666 passed, 1 skipped`. The four failures are the
known sandbox-only ones, all `PermissionError [WinError 5]` at
`_winapi.CreatePipe`:
`tests/test_batch.py::test_cli_empty_and_invalid_inputs`,
`tests/test_batch.py::test_cli_fresh_and_resume`,
`tests/test_evaluation_manifest.py::test_cli_build_validate_and_synthetic_shortfall`
and
`tests/test_evaluation_prepare.py::test_preparation_idempotence_source_preservation_and_collisions`.
The change adds 56 passing tests (`1722 - 1666 = 56`, and the two files collect
56) and no new failure.

One `verify()` result, printed by a probe that opened a fresh temporary
database with `open_database`:

```text
verify(): ()
user_version: 1
user_tables: analysis_versions, sample_features, sample_keys, sample_packs, sample_tags, samples
journal_mode: wal
synchronous: 2
foreign_keys: 1
busy_timeout: 5000
isolation_level: None
row_factory: sqlite3.Row
```

`tests/test_library_schema.py::test_a_newer_schema_version_is_refused_unchanged`
additionally asserts that a `schema_version_newer` refusal leaves the database
file byte-identical while it is in WAL and after it has been switched to
`journal_mode = delete`: the stored version is read before the connection
pragmas run, so refusing a newer schema never rewrites the header of a file
that is not in WAL. A probe against the pre-change module
(`git show HEAD:backend/library/schema.py` from commit `3369304`) reproduced
the opposite result on the same database: the same error code, but the bytes
differed.
