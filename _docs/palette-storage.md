# Project and palette storage

This document is the storage model for a producer's projects, palettes and
palette selections (issue #24, plan section 10 storage model, section 7 domain
model, section 4 MVP workflow, Phase 1 step 9). It is the second document over
the one local SQLite file: [`library-storage.md`](library-storage.md) owns the
database location, the connection policy, the migration mechanism and the
sample, analysis, tag and pack tables, and [`library-jobs.md`](library-jobs.md)
owns the queue. This document owns the three tables issue #24 adds, the
revision and hash rules over them, and the palette repository operations.

Nothing here leaves the device. The tables hold sample ids and the producer's
selection; they hold no audio, no local path and no Jev payload. Identity,
digest and path conventions come from [`batch-analysis.md`](batch-analysis.md)
and stay #21's: `batch.canonical` and `batch.digest` for content identities
and `schema.utc_now()` for every timestamp.

## The migration

Migration 3 of `backend.library.schema.MIGRATIONS` adds the three tables and
two indexes; `SCHEMA_VERSION` becomes 3 and `PRAGMA user_version` is 3 after
`open_database()`. No second schema, no second migration runner and no table
created at runtime: the palette migration is the next entry in #21's chain, each
statement runs in the chain's one transaction, and a failure rolls the whole
migration back as `migration_failed`.

The migration adds tables and indexes only. No column, constraint or index of a
version-1 or version-2 table is created, altered or dropped and no row is
rewritten, so upgrading a database that already holds samples, features, keys,
tags, packs, analysis versions and queue rows keeps every one of those rows
identical. `palettes.project_id` is UNIQUE: the MVP has exactly one palette
per project.

## Tables

The user-table set becomes eleven: #21's six, #23's two and these three. After
`open_database()`, `verify(connection)` is `()`, `PRAGMA foreign_keys`
is 1 and `PRAGMA foreign_key_check` is empty. The slot vocabulary of
`palette_items.slot` is generated from `MVP_SLOTS` and its role vocabulary
from #9's `ROLES`, exactly as migration 1 generates the measurement and unit
vocabularies from `MEASURES`, so neither CHECK can drift from the code that
writes the rows.

### projects

| Column | Type | Keys and constraints |
| --- | --- | --- |
| `project_id` | TEXT | PRIMARY KEY, non-blank after trim |
| `name` | TEXT | NOT NULL, non-blank after trim |
| `created_at` | TEXT | NOT NULL, `YYYY-MM-DDTHH:MM:SSZ` from `schema.utc_now()` |
| `updated_at` | TEXT | NOT NULL |

There is no index beyond the primary key; the MVP lists projects in full.

### palettes

| Column | Type | Keys and constraints |
| --- | --- | --- |
| `palette_id` | TEXT | PRIMARY KEY |
| `project_id` | TEXT | NOT NULL, UNIQUE, REFERENCES `projects(project_id)` ON DELETE CASCADE |
| `name` | TEXT | NOT NULL, non-blank after trim |
| `revision` | INTEGER | NOT NULL DEFAULT 0, CHECK `>= 0` |
| `tempo_bpm` | REAL | NULL, CHECK `IS NULL OR > 0` |
| `tempo_confidence` | REAL | NULL, CHECK `IS NULL OR BETWEEN 0 AND 1` |
| `tempo_unavailable_reason` | TEXT | NULL, or non-blank after trim |
| `key_tonic` | TEXT | NULL, or one of the 12 contract tonics |
| `key_mode` | TEXT | NULL, or `major` / `minor` |
| `key_confidence` | REAL | NULL, CHECK `IS NULL OR BETWEEN 0 AND 1` |
| `key_unavailable_reason` | TEXT | NULL, or non-blank after trim |
| `genre` | TEXT | NULL, or non-blank after trim |
| `genre_unavailable_reason` | TEXT | NULL, or non-blank after trim |
| `created_at` | TEXT | NOT NULL |
| `updated_at` | TEXT | NOT NULL, rewritten by every committed mutation |

Table CHECKs: `(tempo_bpm IS NULL AND tempo_confidence IS NULL) OR (tempo_bpm
IS NOT NULL AND tempo_confidence IS NOT NULL)`; `(key_tonic IS NULL AND
key_mode IS NULL AND key_confidence IS NULL) OR (key_tonic IS NOT NULL AND
key_mode IS NOT NULL AND key_confidence IS NOT NULL)`; `genre IS NULL OR
genre_unavailable_reason IS NULL`. They mirror the contract, so a stored row
always rebuilds into a valid `SongContext` and a value never appears without
its confidence or beside a reason.

### palette_items

| Column | Type | Keys and constraints |
| --- | --- | --- |
| `item_id` | TEXT | PRIMARY KEY, non-blank after trim; opaque, never reused |
| `palette_id` | TEXT | NOT NULL, REFERENCES `palettes(palette_id)` ON DELETE CASCADE |
| `slot` | TEXT | NOT NULL, CHECK IN `MVP_SLOTS` |
| `sample_id` | TEXT | NOT NULL, non-blank after trim; no foreign key (see below) |
| `role` | TEXT | NOT NULL, CHECK IN `kick`, `bass`, `sub-bass` |
| `added_revision` | INTEGER | NOT NULL, CHECK `>= 0`; the revision this item was added at |
| `added_at` | TEXT | NOT NULL |
| `removed_revision` | INTEGER | NULL, CHECK `IS NULL OR >= added_revision` |
| `removed_at` | TEXT | NULL |

Table CHECK: `(removed_revision IS NULL) = (removed_at IS NULL)`. Indexes:
`ux_palette_items_active_slot` UNIQUE on `(palette_id, slot)` WHERE
`removed_at IS NULL` -- at most one active item per slot is enforced by
SQLite, not only by application code -- and `idx_palette_items_sample` on
`sample_id`.

`palette_items.sample_id` deliberately has **no foreign key** to `samples`.
The reference is validated when an item is written (an unknown id raises
`unknown_sample`) and tolerated when it is read (the item stays active and
reports `removed`), so operator pruning of unavailable library records (#71)
can never cascade a palette item away. Deleting a sample never deletes,
re-roles or hides a palette item.

## Slots, roles and the reusable item representation

`MVP_SLOTS = ("kick", "bass")` is the slot vocabulary and `SLOT_ROLES =
{"kick": ("kick",), "bass": ("bass", "sub-bass")}` the roles each slot accepts,
which is the same rule `RecommendationBatch` validates. The two accepted sets
are disjoint, so one sample can never be active in two slots.

An item stores the role it was selected with; a read reports the referenced
sample's current role and flags `slot_role_mismatch` when that role is not in
`SLOT_ROLES[slot]`. Issue #72's role edit therefore never makes a stored item
unreadable: the read raises nothing, re-roles nothing, replaces nothing and
deletes nothing, and the repair decision stays with #32.

Later roles (snare, hats, percussion and the rest of the plan's vocabulary) are
new slot literals, a new `SLOT_ROLES` entry and a new migration -- never a new
table and never a changed item shape. More than one active item per slot is
issue #43's change, not this one.

## The song context: known, unknown and unset

Each context field is in exactly one of three states, and the states are
distinct in storage and in the record:

| State | Columns | Meaning |
| --- | --- | --- |
| `known` | the value (and, for tempo and key, the confidence) is set; every reason is NULL | The context measured it. |
| `unknown` | every value column is NULL and the field's unavailable reason is stored | The value is explicitly absent, and the reason is preserved verbatim. |
| `unset` | every column for the field is NULL | Nothing has been stored for it -- how a palette is created. |

`PaletteRecord.context_state` is a `PaletteContextState(tempo, key, genre)`
with one value per field from `CONTEXT_STATES = ("known", "unknown", "unset")`.
`PaletteRecord.song` is a `backend.contracts.SongContext`: because the
contract has no third state for an absent value, an `unset` field reports
`SONG_CONTEXT_ABSENT_REASON` in it. `to_context()` re-applies the same
mapping, so the assembled context is identical however the record was built.
`SONG_CONTEXT_ABSENT_REASON = "song_context_absent"` equals
`backend.intelligence.questions.SONG_CONTEXT_ABSENT`, and a test asserts the
equality so the two cannot drift.

`set_palette_context(palette_id, song, *, expected_revision)` writes values and
reasons in one transaction. It only ever stores `known` or `unknown`: a
`SongContext` always carries either a value or a reason. A context that
violates the contract -- a tempo value without its confidence, a key tonic
without a mode, a value and a reason together, a blank reason -- raises
`invalid_context` with the contract error as `__cause__` and writes nothing.
A call that repeats the stored context is a no-op: `changed=False` and no
revision change.

## Revisions, conflicts and the staleness rule #32 consumes

`palettes.revision` starts at 0 at creation and is a strict per-palette total
order: every committed mutation that changes observable palette content (adding,
replacing or removing an item, or changing any context value or reason)
increases it by exactly one and returns the new value. Reads, no-ops,
rolled-back writes and `revision_conflict` failures never change it; it never
decreases and a value is never reused.

Every palette-mutating method requires the caller's `expected_revision`
keyword and compares it with the stored one inside its transaction, using the
compare-and-set

```sql
UPDATE palettes SET revision = revision + 1, updated_at = ? WHERE palette_id = ? AND revision = ?
```

Zero updated rows raises `revision_conflict` with the caller's expectation in
`error.expected_revision` and the stored value in `error.current_revision`,
and writes nothing. A stale expectation is a conflict even when the requested
end state already holds, so two identical submissions at the same revision
produce exactly one winner; a caller that re-reads and retries with the new
revision gets the no-op it wanted.

`PaletteContext.revision` returned by `to_context()` is that stored counter.
**The staleness rule #32 consumes:** a response carrying a lower revision than
the palette currently loaded is stale and must be discarded; a writer holding a
stale revision must re-read and retry, and no response from an older revision
may overwrite a newer one.

## palette_hash

`palette_hash(record)` returns the SHA-256 of `batch.canonical`'s JSON of

| Input | Content |
| --- | --- |
| `palette_hash_version` | `PALETTE_HASH_VERSION = "palette-hash-v1"` |
| `palette_id` | the palette's id |
| `items` | the active items sorted by slot, each as its slot, sample id and reported role |
| `context` | tempo value and confidence, key tonic, mode and confidence, genre, and every stored unavailable reason |

digested with `batch.digest` (SHA-256 over those canonical bytes), returned as
64 lowercase hex. It is content, not history: `revision`, timestamps,
`item_id` values, removed items and the referenced samples' availability are
outside it. Two palettes with the same content and the same id hash equal
whatever the insertion order; a no-op mutation, a read and a remove-then-re-add
that restores the same active content leave the hash equal while the revision
has advanced; unset and explicitly unknown contexts hash differently. Because
the hash covers the role the record reports, a #72 role edit changes it even
though no palette row was written.

## Removal, deletion and retention

- Replacing or removing an item marks the row removed (`removed_at`,
  `removed_revision`) and never deletes it: removed items stay in
  `PaletteRecord.removed_items`, keep their `item_id`, `added_at` and role,
  and are excluded from `active_items` and from `palette_hash`. Retention is
  what lets #29 and #36 later link an outcome to the revision that removed an
  item without #24 storing any outcome.
- A remove-then-re-add produces a new `item_id` while the removed row keeps its
  own; ids are opaque (a prefix plus 32 random hex characters) and never reused.
- `delete_project(project_id)` is the only operation in this task that deletes
  palette rows: `projects` to `palettes` to `palette_items` cascade in one
  transaction. It returns `True` once and `False` afterwards, and never
  reads, updates or deletes a `samples`, `sample_features`, `sample_keys`
  or `sample_tags` row. Two projects may reference the same `sample_id` and
  stay independent: changing or removing one palette's item changes the other's
  rows not at all, and deleting the first project leaves the second palette, its
  revision and the shared sample row unchanged.

## LibraryRepository: the palette operations

`LibraryRepository(connection)` wraps one connection from
`schema.open_database`. The palette operations are:

| Method | Inputs | Returns | Raises |
| --- | --- | --- | --- |
| `create_project(name, *, palette_name="Main")` | two nonblank names | `ProjectRecord` with the new `palette_id` | `write_failed`, `database_locked` |
| `get_project(project_id)` | id | `ProjectRecord` or None | -- |
| `list_projects()` | -- | tuple of `ProjectRecord`, ordered by `created_at`, then `project_id` | -- |
| `delete_project(project_id)` | id | True once, then False | `database_locked`, `write_failed` |
| `load_palette(palette_id)` | id | `PaletteRecord` or None | -- |
| `list_palettes(project_id)` | id | tuple of `PaletteRecord`, ordered by `created_at`, then `palette_id` | `unknown_project` |
| `set_palette_item(palette_id, slot, sample_id, *, expected_revision)` | palette, slot, sample id, revision | `PaletteMutation` | `unknown_palette`, `unknown_slot`, `invalid_sample`, `unknown_sample`, `role_mismatch`, `revision_conflict`, `database_locked`, `write_failed` |
| `remove_palette_item(palette_id, slot, *, expected_revision)` | palette, slot, revision | `PaletteMutation` | `unknown_palette`, `unknown_slot`, `revision_conflict`, `database_locked`, `write_failed` |
| `set_palette_context(palette_id, song, *, expected_revision)` | palette, `SongContext` or its wire dict, revision | `PaletteMutation` | `invalid_context`, `unknown_palette`, `revision_conflict`, `database_locked`, `write_failed` |

`ProjectRecord(project_id, name, created_at, updated_at, palette_id)`,
`PaletteContextState(tempo, key, genre)`,
`PaletteItemRecord(item_id, slot, sample_id, role, added_revision, added_at,
removed_revision, removed_at, sample_state, sample_error_code,
slot_role_mismatch)`,
`PaletteRecord(palette_id, project_id, name, revision, song, context_state,
active_items, removed_items)` and
`PaletteMutation(palette_id, revision, changed, active_item, previous_item)`
are frozen records defined in `backend.palette.model`, which is pure: it
imports the standard library, `backend.contracts`, `backend.analysis.batch`
and the one coded failure `to_context()` raises, reaches the database only
through these repository operations, opens no file and touches no network.

`PaletteMutation.revision` is the revision after the call (unchanged for a
no-op); `active_item` is the item a `set_palette_item` left active and
`previous_item` the item a replacement or removal took out of the slot; both
are None for a context change, which touches no item. `sample_state` is the
referenced row's file status (`present`, `missing`, `unknown`) or
`removed` when no `samples` row exists any more (#71's pruning);
`sample_error_code` carries the analysis-queue error code #23 stored for the
row's content identity, or None. Re-selecting the sample that is already active
in a slot is a no-op: `changed=False`, no new row, `added_at` and
`revision` unchanged.

## Error codes

One `LibraryError` base with a `.code` and one subclass per code, all in
`backend/library/errors.py`; no `sqlite3` exception escapes and no palette
code exists outside that module. The codes this task adds:

| Code | Raised by |
| --- | --- |
| `unknown_project` | `list_palettes` for a project with no row |
| `unknown_palette` | `set_palette_item`, `remove_palette_item`, `set_palette_context` for a palette with no row (`load_palette` returns None instead) |
| `unknown_slot` | `set_palette_item`, `remove_palette_item` for a slot outside `MVP_SLOTS` |
| `role_mismatch` | `set_palette_item` when the sample's stored role is not in `SLOT_ROLES[slot]`; the message names the slot, the accepted roles, the stored role and the sample id |
| `invalid_context` | `set_palette_context` when the song context fails contract validation; the contract error is the `__cause__` |
| `revision_conflict` | every palette-mutating method when `expected_revision` is not the stored revision; `expected_revision` and `current_revision` are readable on the error |
| `palette_incomplete` | `PaletteRecord.to_context()` when no kick is active |

The codes this task reuses: `unknown_sample` (`set_palette_item` for a
`sample_id` with no `samples` row), `invalid_sample` (a blank sample id),
`write_failed` (a refused write, for example the partial unique index or a
trigger; the `sqlite3` error is the `__cause__`, and `create_project` uses
it for a blank name), `database_locked` (a writer that cannot take the lock
within #21's `busy_timeout`), `migration_failed`, `not_a_database`,
`database_corrupt` and `schema_version_newer` (the migration and the
`open_database` refusals the palette migration inherits unchanged).

Following #21's convention, "nothing to do or nothing there" is a return value
for `get_project`, `load_palette`, `delete_project` and a no-op
`remove_palette_item`, while an operation given a project, palette or slot that
does not exist raises the code naming it.

## What is never stored

- No BLOB column and no audio bytes anywhere in the three tables; the only
  audio-derived values are the sample ids and the context numbers.
- No local path: `palette_items` references `sample_id` only, and no palette
  code opens, stats or decodes an audio file.
- No Jev request, judgment, payload, credential or endpoint, and no
  recommendation, score or outcome row. Issue #26 owns the decision cache, #29
  the outcome events and #27 the local API.
- No cache of a `palette_hash` and no table created at runtime.

## Names and deviations from the issue body

The issue was groomed against #21's planned names; the landed surface differs
in three places, and this task uses the landed ones:

| The issue body's wording | The landed name used here |
| --- | --- |
| one big error module | unchanged: `UnknownProject`, `UnknownPalette`, `UnknownSlot`, `RoleMismatch`, `InvalidContext`, `RevisionConflict` and `PaletteIncomplete` in `backend/library/errors.py` |
| ad-hoc transactions | #21's `transaction(connection)` and `LibraryRepository._writing()` |
| `sample_state` from a scan record | the row's `samples.file_status`, plus #23's `job_items.error_code` reached through `samples.content_sha256` for `sample_error_code` |

Two further decisions are recorded here because they are visible in the code:

- `backend/palette/model.py` imports `PaletteIncomplete` from
  `backend/library/errors.py`. The issue asks for `to_context()` to raise that
  code *and* for the module to import only the standard library,
  `backend.contracts` and `backend.analysis.batch`; one error vocabulary
  (`LibraryError` subclasses in one file) wins, and the module still never
  imports `sqlite3`, opens a file or touches the network.
- Migration 3 advances `SCHEMA_VERSION` to 3 and adds three user tables, so
  three existing expectation tests could not stay as they were:
  `tests/test_library_schema.py` (the table set, the column map and
  `SCHEMA_VERSION`), `tests/test_library_queue.py` (the version literals) and
  `tests/test_library_scanner.py` (the exact table list and the "the three
  palette tables stay empty after a scan" probe). Every change is an expectation
  update, exactly as #23's migration updated the same assertions.
- `_docs/library-storage.md` still says the user-table set is eight tables.
  That document is not in this task's file list, so this document is the record:
  the set is eleven tables and the three palette tables are documented above.
  Updating the older document is a follow-up.

## Tests

```bash
uv run pytest tests/test_palette_repository.py tests/test_palette_migration.py tests/test_palette_recovery.py
uv run pytest
```

`tests/fixtures/palette/palette-cases.json` holds sixteen named cases with
hand-written expectations for revisions, `changed` flags, sample states,
slot-role flags and context states, plus the hash equal/different matrix.
`tests/test_palette_repository.py` covers the round trip, the contract
assembly, the context states, slot and role rejection, atomic replacement, the
partial unique index, no-op retries, removal and retention, project deletion
with shared samples, id and revision monotonicity, the hash matrix, the error
codes, privacy and the document agreement.
`tests/test_palette_migration.py` covers the fresh database, the upgrade from
version 2 with library and queue rows present, idempotent reopen, a further
upgrade step of the chain, the three refusals with bytes and rows unchanged, and
the failed-migration rollbacks. `tests/test_palette_recovery.py` covers
missing, unknown, pruned and role-changed samples, the stored queue error code,
the restore-after-scan case, and that no palette read writes, opens a file or
raises.

## Local verification

Both commands were run from the repository root with the project interpreter.
`uv run pytest` cannot capture a subprocess in this sandbox (it fails with
`PermissionError [WinError 5]` at `_winapi.CreatePipe`), so each run put a
`sitecustomize.py` on `PYTHONPATH`, passed `-p no:cacheprovider` and a
`--basetemp` outside the repository. Every database was a temporary file.

```text
.venv\Scripts\python.exe -m pytest tests/test_palette_repository.py tests/test_palette_migration.py tests/test_palette_recovery.py --basetemp <tmp>\bt-focus -p no:cacheprovider -q -rf
  -> 70 passed in 13.82s

.venv\Scripts\python.exe -m pytest --basetemp <tmp>\bt-full -p no:cacheprovider -q -rf
  -> 4 failed, 1897 passed, 1 skipped in 382.84s
```

The baseline measured in this workspace under the same sandbox before this
change, at commit `975da46`, was `4 failed, 1827 passed, 1 skipped`. The four
failures are the known sandbox-only ones, all `PermissionError [WinError 5]` at
`_winapi.CreatePipe`: `tests/test_batch.py::test_cli_empty_and_invalid_inputs`,
`tests/test_batch.py::test_cli_fresh_and_resume`,
`tests/test_evaluation_manifest.py::test_cli_build_validate_and_synthetic_shortfall`
and
`tests/test_evaluation_prepare.py::test_preparation_idempotence_source_preservation_and_collisions`.
The change adds 70 passing tests (`1897 - 1827 = 70`, and the three new files
collect 70) and no new failure and no new skip.

One concrete probe result, printed by a script that opened a fresh temporary
database with `open_database`, stored two analysed samples, selected one kick
and one bass, and then pruned the bass row with `delete_sample`:

```text
verify(): ()
user_version: 3
user_tables: analysis_versions, job_items, job_runs, palette_items, palettes, projects, sample_features, sample_keys, sample_packs, sample_tags, samples
palette_indexes: idx_palette_items_sample, sqlite_autoindex_palette_items_1, ux_palette_items_active_slot
foreign_key_check: []
pruned_read: bass bass-001 removed None
revision_and_hash_unchanged: True True
to_context_selected_bass: bass-001
```

The pruned item is still active and still reports its slot and sample id; its
`sample_state` is `removed`, the palette's revision and hash are unchanged
and `to_context()` still returns the sample id #32's repair state reads.
