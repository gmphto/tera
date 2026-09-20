# Versioned decision caching

This document is the contract for the local decision cache (issue #26, plan
section 6 ranking pipeline and section 10 storage model, Phase 1 kick-to-bass
MVP). It is the third document over the one local SQLite file:
[`library-storage.md`](library-storage.md) owns the database location, the
connection policy, the migration mechanism and the sample tables;
[`library-jobs.md`](library-jobs.md) owns the analysis queue;
[`palette-storage.md`](palette-storage.md) owns projects and palettes. This
document owns the two tables issue #26 adds and the operations over them.

Nothing here leaves the device. The two tables hold ids, digests, version
strings, #13 response documents and the numbers the caller produced; they hold
no audio byte, no local path, no filename, no library root, no pack name, no
credential, no endpoint, no rank and no rendered reason. Identity conventions
stay #21's: `batch.canonical` and `batch.digest` for every digest and
`schema.utc_now()` for every timestamp.

## The migration

Migration 4 of `backend.library.schema.MIGRATIONS` adds two tables and two
indexes; `SCHEMA_VERSION` becomes 4 and `PRAGMA user_version` is 4 after
`open_database()`. No second schema and no second migration runner: the cache
migration is the next entry in #21's chain, each statement runs in the chain's
one `BEGIN IMMEDIATE` transaction, and a failure rolls the whole migration
back as `migration_failed`.

The migration adds tables and indexes only. No column, constraint or index of a
version-1, version-2 or version-3 table is created, altered or dropped and no
row is rewritten, so upgrading a database that already holds samples, features,
keys, tags, packs, analysis versions, queue rows, projects, palettes and
palette items keeps every one of those rows identical. The user-table set
becomes thirteen: #21's six, #23's two, #24's three and these two.

## Tables

After `open_database()`, `verify(connection)` is `()`, `PRAGMA foreign_keys`
is 1 and `PRAGMA foreign_key_check` is empty.

### decision_cache

One row is one stored decision. The primary key `cache_key` is
`cache_key_digest(key)`, a bare 64-character lowercase SHA-256 over the key's
canonical JSON, so it never contains a local path, a filename or a library
name.

| Column | Type | Keys and constraints |
| --- | --- | --- |
| `cache_key` | TEXT | PRIMARY KEY, non-blank after trim |
| `cache_key_version` | TEXT | NOT NULL, exactly `decision-cache-v1` |
| `decision_kind` | TEXT | NOT NULL, `judgment` or `candidate_decision` |
| `source` | TEXT | NOT NULL, `interface` or `double` |
| `interface_name` | TEXT | NOT NULL, non-blank after trim |
| `adapter_version` | TEXT | NOT NULL, non-blank after trim |
| `prompt_version` | TEXT | NOT NULL, non-blank after trim |
| `model_version` | TEXT | NOT NULL, non-blank after trim |
| `palette_hash` | TEXT | NOT NULL, non-blank after trim; #24's content hash |
| `palette_hash_version` | TEXT | NOT NULL, non-blank after trim; #24's hash version |
| `candidate_id` | TEXT | NOT NULL, non-blank after trim |
| `candidate_content_fingerprint` | TEXT | NOT NULL, non-blank after trim |
| `candidate_analysis_version` | TEXT | NOT NULL, non-blank after trim |
| `dimension` | TEXT | NULL, or non-blank after trim; set only for `judgment` |
| `question_id` | TEXT | NULL, or non-blank after trim; set only for `judgment` |
| `kick_id` | TEXT | NULL, or non-blank after trim; set only for `candidate_decision` |
| `kick_content_fingerprint` | TEXT | NULL, or non-blank after trim; set only for `candidate_decision` |
| `kick_analysis_version` | TEXT | NULL, or non-blank after trim; set only for `candidate_decision` |
| `questions_digest` | TEXT | NULL, or non-blank after trim; set only for `candidate_decision` |
| `ranking_version` | TEXT | NULL, or non-blank after trim; set only for `candidate_decision` |
| `weight_table_id` | TEXT | NULL, or non-blank after trim; set only for `candidate_decision` |
| `baseline_ranking_version` | TEXT | NULL, or non-blank after trim; set only for `candidate_decision` |
| `baseline_weight_table_id` | TEXT | NULL, or non-blank after trim; set only for `candidate_decision` |
| `payload_json` | TEXT | NOT NULL, `json_valid(payload_json) AND json_type(payload_json) = 'object'` |
| `created_at` | TEXT | NOT NULL, `YYYY-MM-DDTHH:MM:SSZ` from `schema.utc_now()` |

The table's CHECKs are exactly the ones issue #26 publishes: for every column
whose value is present only for one kind, `(decision_kind = '<kind>') = (column
IS NOT NULL)`, plus `(decision_kind = 'judgment') = (dimension IS NOT NULL)`,
`(decision_kind = 'judgment') = (question_id IS NOT NULL)`, the single
candidate-side CHECK that every one of its eight columns is NULL for a judgment,
and `CHECK (decision_kind <> 'candidate_decision' OR dimension IS NULL)`. A row
whose stored values contradict its own kind therefore cannot be written, by the
module or by direct SQL.

The CHECK expressions, read from the migrated table, are:

```
CHECK (length(trim(cache_key)) > 0)
CHECK (cache_key_version = 'decision-cache-v1')
CHECK (decision_kind IN ('judgment', 'candidate_decision'))
CHECK (source IN ('interface', 'double'))
CHECK (length(trim(<each NOT NULL text column>)) > 0)         -- interface_name, adapter_version,
                                                              -- prompt_version, model_version,
                                                              -- palette_hash, palette_hash_version,
                                                              -- candidate_id,
                                                              -- candidate_content_fingerprint,
                                                              -- candidate_analysis_version
CHECK (<each nullable text column> IS NULL OR length(trim(<column>)) > 0)
CHECK (json_valid(payload_json) AND json_type(payload_json) = 'object')
CHECK ((decision_kind = 'judgment') = (dimension IS NOT NULL))
CHECK ((decision_kind = 'judgment') = (question_id IS NOT NULL))
CHECK ((decision_kind = 'judgment') = (kick_id IS NULL AND kick_content_fingerprint IS NULL
       AND kick_analysis_version IS NULL AND questions_digest IS NULL AND ranking_version IS NULL
       AND weight_table_id IS NULL AND baseline_ranking_version IS NULL
       AND baseline_weight_table_id IS NULL))
CHECK ((decision_kind = 'candidate_decision') = (kick_id IS NOT NULL))
CHECK ((decision_kind = 'candidate_decision') = (kick_content_fingerprint IS NOT NULL))
CHECK ((decision_kind = 'candidate_decision') = (kick_analysis_version IS NOT NULL))
CHECK ((decision_kind = 'candidate_decision') = (questions_digest IS NOT NULL))
CHECK ((decision_kind = 'candidate_decision') = (ranking_version IS NOT NULL))
CHECK ((decision_kind = 'candidate_decision') = (weight_table_id IS NOT NULL))
CHECK ((decision_kind = 'candidate_decision') = (baseline_ranking_version IS NOT NULL))
CHECK ((decision_kind = 'candidate_decision') = (baseline_weight_table_id IS NOT NULL))
CHECK (decision_kind <> 'candidate_decision' OR dimension IS NULL)
```

Two indexes exist: `idx_decision_cache_created` on
`(created_at, cache_key)`, the documented pruning order, and
`idx_decision_cache_candidate` on `(candidate_id)`, the explicit invalidation
lookup.

`decision_cache.candidate_id` and `decision_cache.kick_id` deliberately have
**no foreign key** to `samples`. A sample row that #71 prunes or #22 re-scans
must never cascade a cache entry away, and explicit invalidation is #26's own
operation: `PRAGMA foreign_key_check` stays empty and a cache row survives the
deletion of the `samples` row it names until `invalidate_candidate` is called.

### decision_model_versions

The current observed model version per interface identity.

| Column | Type | Keys and constraints |
| --- | --- | --- |
| `interface_name` | TEXT | NOT NULL, non-blank after trim; PRIMARY KEY part |
| `source` | TEXT | NOT NULL, `interface` or `double`; PRIMARY KEY part |
| `adapter_version` | TEXT | NOT NULL, non-blank after trim; PRIMARY KEY part |
| `prompt_version` | TEXT | NOT NULL, non-blank after trim; PRIMARY KEY part |
| `model_version` | TEXT | NOT NULL, non-blank after trim |
| `first_observed_at` | TEXT | NOT NULL, set once and kept |
| `observed_at` | TEXT | NOT NULL, moved on every observation |
| `observation_count` | INTEGER | NOT NULL, DEFAULT 1, `>= 1` |

The primary key is `(interface_name, source, adapter_version, prompt_version)`,
so a `double` pin can never answer an `interface` lookup: the two identities
differ in both `interface_name` and `source`.

## The key

`DecisionCacheKey` is one frozen record with exactly these fields, in this
order: `cache_key_version`, `decision_kind`, `source`, `interface_name`,
`adapter_version`, `prompt_version`, `model_version`, `palette_hash`,
`palette_hash_version`, `candidate_id`, `candidate_content_fingerprint`,
`candidate_analysis_version`, `dimension`, `question_id`, `kick_id`,
`kick_content_fingerprint`, `kick_analysis_version`, `questions_digest`,
`ranking_version`, `weight_table_id`, `baseline_ranking_version`,
`baseline_weight_table_id`. It carries the key, the payload records and the pin
method set the task publishes: the key, the three payload records and the pin
also have `to_json()`, `from_dict()` and `from_json()`.

```
CACHE_KEY_VERSION = decision-cache-v1
CACHE_PAYLOAD_VERSION = decision-cache-payload-v1
DECISION_KINDS = ('judgment', 'candidate_decision')
CACHE_ORIGIN = cache
CACHEABLE_OUTCOME_STATES = ('judged', 'abstained')
CACHE_MISS_REASONS = ('absent', 'version_unsupported', 'cached_response_invalid', 'cache_corrupt')
DEFAULT_CACHE_MAX_ENTRIES = 50000
MIN_CACHE_MAX_ENTRIES = 1
MAX_CACHE_MAX_ENTRIES = 1000000
DEFAULT_CACHE_MAX_BYTES = 67108864
MIN_CACHE_MAX_BYTES = 4096
MAX_CACHE_MAX_BYTES = 1073741824
MAX_PAYLOAD_BYTES = 65536
```

`DECISION_KINDS` is `('judgment', 'candidate_decision')` and
`CACHEABLE_OUTCOME_STATES` is `('judged', 'abstained')`. The palette hash
version is #24's `PALETTE_HASH_VERSION` (`palette-hash-v1`); it is never
re-derived or re-declared here.

The two identity records group the key's fields by kind. `JudgmentIdentity`
carries `source`, `interface_name`, `adapter_version`, `prompt_version`,
`model_version`, `palette_hash`, `palette_hash_version`, `candidate_id`,
`candidate_content_fingerprint` and `candidate_analysis_version`.
`CandidateDecisionIdentity` carries those ten plus `kick_id`,
`kick_content_fingerprint`, `kick_analysis_version`, `ranking_version`,
`weight_table_id`, `baseline_ranking_version` and
`baseline_weight_table_id`.

- `judgment_key(identity, question)` takes `dimension` and `question_id` from
  the asked #13 `JevQuestion` and leaves the eight candidate-decision-only
  fields NULL. A question this product refused to ask has no digest and is
  `cache_key_invalid`: there is nothing to ask again and nothing to key.
- `candidate_decision_key(identity, questions)` fills `questions_digest =
  batch.digest([[question.dimension, question.question_id or question.code] for
  question in questions]) over the asked questions in the order supplied`, plus
  the identity's kick fields and the four ranking fields, and leaves `dimension`
  and `question_id` NULL.
- `cache_key_digest(key)` is `batch.digest(key.to_dict())`: bare
  64-character lowercase hex, path-free, and equal for equal keys whatever order
  their fields were built in.
- The constructor refuses a blank text field, an unknown `decision_kind` or
  `source`, and any key whose optional-field set does not match the table's
  CHECKs, with `cache_key_invalid`.

```
DECISION_KINDS = ('judgment', 'candidate_decision')
CACHE_MISS_REASONS = ('absent', 'version_unsupported', 'cached_response_invalid', 'cache_corrupt')
CACHEABLE_OUTCOME_STATES = ('judged', 'abstained')
```

The palette hash and its version are taken verbatim from #24's
`palette_hash(record)` and `PALETTE_HASH_VERSION`. History never keys an
entry: `revision`, `item_id`, timestamps and removed items are outside
`palette_hash`, so a no-op mutation or a remove-then-re-add that restores the
same active content still hits, while a changed active sample, role, kick, bass
slot or context value misses every entry for that palette.

## The payloads

`CachedJudgment` is frozen with exactly `payload_version`, `dimension`,
`question_id`, `response_json`, `origin`, `origin_source`,
`origin_interface_name`, `adapter_version`, `attempts`, `origin_elapsed_ms`
and `created_at`. `response_json` is `response_document(question, judgment)`:
the exact eight-field #13 response object `(question_id, dimension, label,
probabilities, confidence, model_version, prompt_version, unavailable_reason)`
canonicalised so its keys are sorted and its probabilities are in contract
order. `origin` is always `cache`.

`CachedDimensionDecision` is frozen with exactly `dimension`,
`dsp_compatibility`, `dsp_unavailable_reason`, `judgment`,
`unavailable_reason` and `combined_compatibility`. `CachedCandidateDecision`
is frozen with exactly `payload_version`, `candidate_id`, `analysis_version`,
`ranking_version`, `weight_table_id`, `mode`, `jev_status`,
`compatibility`, `confidence`, `uncertain`, `dimensions` and `warnings`.
Its `dimensions` always carries the six contract dimensions in contract order,
and each carries exactly one of a `judgment` or an `unavailable_reason`.

The payload deliberately carries no `rank`, no `alternatives` and no rendered
reason string: a rank is a property of one run's candidate set and reason
strings are regenerated by #15, #28 and #33. `canonical(record.to_dict())` is
byte-identical across runs and processes and `json.dumps(..., allow_nan=False)`
succeeds.

## Entry points

Every entry point lives in `backend/library/decision_cache.py` and holds no SQL
statement of its own: every read and write is a `LibraryRepository` operation
in `backend/library/repository.py`. Every write runs in exactly one
`repository.transaction` block; a lock or a refused write is mapped through
`repository.coded_error` to `database_locked` or `write_failed`; a lookup
runs individual statements and never writes.

| Entry point | Inputs | Output | Raised codes |
| --- | --- | --- | --- |
| `judgment_key(identity, question)` | `JudgmentIdentity`, asked `JevQuestion` | `DecisionCacheKey` | `cache_key_invalid` |
| `candidate_decision_key(identity, questions)` | `CandidateDecisionIdentity`, a list or tuple of #13 questions | `DecisionCacheKey` | `cache_key_invalid` |
| `cache_key_digest(key)` | `DecisionCacheKey` | 64-character lowercase hex | `cache_key_invalid` |
| `response_document(question, judgment)` | asked `JevQuestion`, `JevJudgment` | the eight-field #13 response object | `cache_payload_invalid` |
| `pinned_model_version(connection, *, interface_name, source, adapter_version, prompt_version)` | interface identity | `ModelVersionPin` or None | `cache_key_invalid` |
| `record_model_version(connection, run)` | a #14 `JevScoringRun` | `ModelVersionPin` or None | `cache_payload_invalid`, `database_locked`, `write_failed` |
| `lookup_judgment(connection, key, *, question)` | `DecisionCacheKey`, asked `JevQuestion` | `CacheLookup` | `cache_key_invalid` |
| `store_judgment(connection, key, *, outcome, max_entries=DEFAULT_CACHE_MAX_ENTRIES, max_bytes=DEFAULT_CACHE_MAX_BYTES)` | `DecisionCacheKey`, #14 `JevOutcome` | `StoreResult` | `cache_key_invalid`, `cache_payload_invalid`, `decision_not_cacheable`, `invalid_cache_bound`, `database_locked`, `write_failed` |
| `lookup_candidate_decision(connection, key, *, questions)` | `DecisionCacheKey`, the supplied questions | `CacheLookup` | `cache_key_invalid` |
| `store_candidate_decision(connection, key, *, decision, max_entries=DEFAULT_CACHE_MAX_ENTRIES, max_bytes=DEFAULT_CACHE_MAX_BYTES)` | `DecisionCacheKey`, `CachedCandidateDecision` | `StoreResult` | `cache_key_invalid`, `cache_payload_invalid`, `invalid_cache_bound`, `database_locked`, `write_failed` |
| `invalidate_entry(connection, key)` | `DecisionCacheKey` | True when a row was deleted | `cache_key_invalid`, `database_locked`, `write_failed` |
| `invalidate_candidate(connection, *, candidate_id)` | a candidate id | the number of rows deleted | `cache_key_invalid`, `database_locked`, `write_failed` |
| `prune_cache(connection, *, max_entries=DEFAULT_CACHE_MAX_ENTRIES, max_bytes=DEFAULT_CACHE_MAX_BYTES)` | the two bounds | `PruneReport` | `invalid_cache_bound`, `database_locked`, `write_failed` |
| `cache_stats(connection)` | one connection | `CacheStats` | none |

The result records are `CacheLookup` (`status`, `reason`, `judgment`,
`decision`, `stale_found`, `entry_created_at`), `StoreResult` (`stored`,
`created_at`, `pruned`), `PruneReport` (`deleted`, `remaining_entries`,
`remaining_bytes`, `max_entries`, `max_bytes`) and `CacheStats` (`entries`,
`judgments`, `candidate_decisions`, `bytes`, `oldest_created_at`,
`newest_created_at`), and `ModelVersionPin` (`interface_name`, `source`,
`adapter_version`, `prompt_version`, `model_version`, `first_observed_at`,
`observed_at`, `observation_count`). Every one has `to_dict()`.

The four coded failures are `LibraryError` subclasses in
`backend/library/errors.py`: `cache_key_invalid`, `cache_payload_invalid`,
`decision_not_cacheable` and `invalid_cache_bound`.

## The four miss reasons

A lookup answers with `status` `hit` or `miss`. A miss carries one of
`CACHE_MISS_REASONS`, never an exception, and never deletes a row:

| Reason | Raised by | `stale_found` |
| --- | --- | --- |
| `absent` | no row for the key | False |
| `version_unsupported` | a row whose `cache_key_version`, or whose payload `payload_version`, is not the value this code writes | True |
| `cached_response_invalid` | a stored document that `validate_response` no longer accepts | True |
| `cache_corrupt` | a payload that does not parse, or is not the exact declared record shape, or contradicts the row's own key | True |

An unsupported entry is **kept**, because deleting a newer writer's row would
silently reset the cache. A `cache_key_invalid` is raised instead of a miss
when the caller's key and the question or question set disagree
(`key.question_id != question.question_id`, `key.dimension !=
question.dimension`, `key.prompt_version != question.prompt_version`, or a
`questions_digest` that does not match the supplied questions): a caller bug
must never be mistaken for a stale entry.

## The re-validation rule

The cache stores a #13 response document, never a judgment it trusts. On every
hit `lookup_judgment` calls `validate_response(question, stored_response_json)`
and returns the judgment that call produces, with `entry_created_at` from the
row; `lookup_candidate_decision` re-validates every embedded judgment against
the question supplied for its dimension. A `JevResponseError` (a changed
prompt, a tightened rule, a value that no longer validates) is a miss with
reason `cached_response_invalid`, `stale_found=True`, no exception escaping
and **no stored judgment returned, ever**. The caller's next step is a live call
and, if it wants the row gone, `invalidate_entry(connection, key)`.

## What is stored and what is refused

`store_judgment` accepts a #14 `JevOutcome` only when `outcome.state` is in
`CACHEABLE_OUTCOME_STATES` (`judged` or `abstained`), `outcome.judgment` is a
`JevJudgment` and the key's `source` is a #14 transport source. Every other
state --- `not_asked`, `not_attempted`, `invalid_result`, `service_error`,
`timed_out`, `unavailable` --- raises `decision_not_cacheable` and writes no
row. A labeled judgment and an abstention (`label` is None,
`unavailable_reason` `model_abstained`) are both storable.

`store_judgment` derives the stored document with
`response_document(question, judgment)` and refuses with
`cache_payload_invalid` unless `validate_response(question, document)`
reproduces `outcome.judgment` exactly and the key already matches
`question.question_id`, `question.dimension`,
`judgment.prompt_version == PROMPT_VERSION` and `judgment.model_version`. The
key already names the asked question, so the module rebuilds that question from
the key's three echoed fields and validates against it. A key whose
`model_version` is not the judgment's own raises `cache_key_invalid`.

`store_candidate_decision` refuses a payload that is not exactly the
`CachedCandidateDecision` shape, whose `compatibility` or `confidence` is
outside `[0, 1]`, whose `dimensions` are not the six `get_args(Dimension)`
literals in contract order, whose per-dimension `unavailable_reason` is not a
#13 `QUESTION_UNAVAILABLE_CODES` code or a #14 `TRANSPORT_ERROR_CODES`,
`NOT_ATTEMPTED_CODES` or `UNAVAILABLE_CODES` code, whose `mode` or
`jev_status` is not a `backend.palette.ranking` literal, whose `warnings` are
not `HYBRID_CODES` members, whose `judgment` and `unavailable_reason` are both
set or both absent, whose embedded `candidate_id` or `analysis_version`
disagrees with the key, or whose canonical payload exceeds `MAX_PAYLOAD_BYTES`
--- all with `cache_payload_invalid` and no row written. No ranking function is
called: the caller produced the decision.

## The model-version pin

`decision_model_versions` records the current observed version per
`(interface_name, source, adapter_version, prompt_version)`.
`record_model_version(connection, run)` reads a #14 `JevScoringRun` and, when
the run is not cancelled, its `source` is a transport source, its
`interface_name` is non-blank and `run.model_versions` holds exactly one
distinct version, upserts that version with `observed_at = utc_now()`,
`observation_count += 1` and `first_observed_at` unchanged. A run with zero or
more than one distinct version, a cancelled run and a run whose source is
`unavailable` write nothing and return None.

The lookup is two steps: resolve the pin with
`pinned_model_version(connection, *, interface_name, source, adapter_version,
prompt_version)`, then build the key with `model_version =
pin.model_version`. With no pin no key can name a model version and the caller
must call Jev. A `double` pin never satisfies an `interface` lookup, so with
no credentials only contract-double entries can ever hit. A judgment stored
under version A is a miss with reason `absent` after a live run observes
version B, a judgment stored under B hits, and A's row survives until it is
pruned or invalidated.

## Bounds and pruning

Both bounds are enforced on every store and on demand:

- `store_judgment` and `store_candidate_decision` accept `max_entries` and
  `max_bytes`; a value outside the published range, or one that is not an
  `int`, raises `invalid_cache_bound` and writes nothing.
- Inside the same transaction as the insert, the oldest rows by
  `(created_at ASC, cache_key ASC)` are deleted --- **never the row just
  written** --- until both `COUNT(*) <= max_entries` and
  `COALESCE(SUM(length(payload_json)), 0) <= max_bytes` hold. The number is
  reported as `StoreResult.pruned`.
- `prune_cache` performs the same deletion on demand and returns
  `PruneReport(deleted, remaining_entries, remaining_bytes, max_entries,
  max_bytes)`; a second identical call deletes 0 and reports the same counts.
- `cache_stats` returns `CacheStats(entries, judgments, candidate_decisions,
  bytes, oldest_created_at, newest_created_at)`.
- Only a store or `prune_cache` prunes; no lookup, stats call or pin read ever
  deletes a row.

Because the just-written row is never evicted, a payload larger than
`max_bytes` is written and the byte bound is then exceeded by exactly that one
row. `MAX_PAYLOAD_BYTES` (65536) is larger than `MIN_CACHE_MAX_BYTES` (4096),
so that state is reachable and is documented rather than hidden.

## Invalidation

Invalidation is explicit, targeted and idempotent.
`invalidate_entry(connection, key)` deletes exactly the row for that key and
returns True, or False when no row exists.
`invalidate_candidate(connection, *, candidate_id)` deletes every row for that
candidate in both kinds and in every palette in one transaction and returns the
count, returns 0 on a second call, and touches no other candidate, no
`decision_model_versions` pin and no #21, #22, #23 or #24 row. It is the
documented path after #22 re-scans or #71 prunes a sample, because no foreign
key cascades a cache entry away.

A cache write is an optimization and never the caller's correctness dependency.
A store that fails --- `database_locked` within #21's documented
`busy_timeout`, or `write_failed` from a failing trigger or a rolled-back
transaction --- leaves no partial row, keeps every existing entry unchanged and
leaves the caller's already-computed decision valid and usable. The documented
recovery is to retry the store or continue uncached; a store failure is never a
failed recommendation. Two connections cannot produce a duplicate, mixed or
half-written record: the insert is `ON CONFLICT(cache_key) DO NOTHING` inside
one transaction, so the first committed entry wins and a later store under an
unchanged key writes nothing and returns
`StoreResult(stored=False, created_at=<the existing row>)`.

## A cache hit is never a live outcome

The rule: a cache hit is reused evidence, never a live outcome.

Every stored judgment carries `origin = CACHE_ORIGIN` (`cache`) plus the
original run's `origin_source` (`interface` or `double`),
`origin_interface_name`,
`adapter_version`, `attempts` and `origin_elapsed_ms` copied verbatim from
the #14 outcome, and `created_at` from the cache write. #15 consumes the
re-derived judgment as `HybridEvidence` and has no provenance field of its
own, so the `origin` label is #26's: #28 and #33 must display it with the
entry's age, and #19 counts live evidence only from a `JevScoringRun` whose
`source` is `interface`, so a cached judgment is excluded from every live
count, from any lift claim and from
[`ranking-comparison-19.md`](ranking-comparison-19.md). No cache record, table
or entry point exposes a `JevScoringRun`, a `source` value of its own, or an
attempt count it did not copy.

## What is not stored

No audio byte, no local path, no filename, no library root, no pack name, no
credential, no endpoint, no `JevScoringRun` and no rendered reason. The
committed fixtures use synthetic ids and bare `sha256:` digests only, and the
#13 privacy tokens appear only in the assertion that they appear nowhere.

## Storage operations consumed from #21

`MIGRATIONS`, `SCHEMA_VERSION`, `open_database`, `migrate`, `verify` and
`utc_now` come from `backend/library/schema.py` and keep their landed names.
`transaction` comes from `backend/library/repository.py`. #21 exposed no
cache statement, so this task added them to #21's own file under its naming, in
the section marked `# -- decision cache (issue #26)`:

| Operation | Purpose |
| --- | --- |
| `LibraryRepository.insert_decision_cache_entry(values)` | the one `ON CONFLICT(cache_key) DO NOTHING` insert |
| `LibraryRepository.decision_cache_row(cache_key)` | one whole row |
| `LibraryRepository.decision_cache_created_at(cache_key)` | the existing row's timestamp on a no-op store |
| `LibraryRepository.decision_cache_totals()` | the entry count and stored payload bytes |
| `LibraryRepository.decision_cache_oldest_keys(exclude_key)` | keys ordered `(created_at ASC, cache_key ASC)`, minus the row just written |
| `LibraryRepository.decision_cache_stats()` | the counts, the bytes and the timestamp extremes |
| `LibraryRepository.delete_decision_cache_key(cache_key)` | one explicit deletion |
| `LibraryRepository.delete_decision_cache_candidate(candidate_id)` | one candidate's deletions |
| `LibraryRepository.decision_model_version_pin(...)` | one pin by its primary key |
| `LibraryRepository.upsert_decision_model_version_pin(values)` | the counting upsert |
| `repository.coded_error(error)` | #21's sqlite3-to-`LibraryError` mapping, made public so a caller that owns its own transaction block maps a lock or a refused write the same way |

The key components are reused, never re-declared: #24's `palette_hash` and
`PALETTE_HASH_VERSION`, #13's `PROMPT_VERSION`, `QUESTION_UNAVAILABLE_CODES`,
`JevQuestion`, `UnavailableQuestion`, `validate_response`, `JevResponseError`,
#14's `JevOutcome`, `JevScoringRun`, `OUTCOME_STATES`, `TRANSPORT_SOURCES`,
`TRANSPORT_ERROR_CODES`, `NOT_ATTEMPTED_CODES`, `UNAVAILABLE_CODES`, #15's
mode, status and warning literals, and `canonical` and `digest` from
`backend.analysis.batch`.

## Version bump rule

A change to what a key covers needs a new `CACHE_KEY_VERSION` and a change to
a stored record's shape needs a new `CACHE_PAYLOAD_VERSION`. Neither is a
schema migration: an existing entry written under an older value is a miss with
reason `version_unsupported` and is kept, because deleting a newer writer's row
would silently reset the cache.

## Resolved inconsistencies in the issue body

Five statements cannot all hold literally. They are resolved as follows, with
the exact text.

1. **The question a store validates against.** The entry-point list publishes
   "store_judgment(connection, key, *, outcome, max_entries=..., max_bytes=...)"
   with no question parameter, while the store criterion requires that the call
   "refuses with cache_payload_invalid unless
   validate_response(question, document) reproduces outcome.judgment exactly
   and the key already matches question.question_id, question.dimension".
   Resolution: the key is the caller's statement of what was asked, and the
   module rebuilds the asked question from the key's `question_id`,
   `dimension` and `prompt_version` --- exactly the three fields #13 compares
   --- so both sentences hold and no second parameter appears.
2. **The payload's per-dimension reason against #15's combined reason.** The
   store criterion forbids "whose per-dimension unavailable_reason is not a #13
   QUESTION_UNAVAILABLE_CODES code or a #14
   TRANSPORT_ERROR_CODES/NOT_ATTEMPTED_CODES/UNAVAILABLE_CODES code", while the
   round-trip criterion asserts "field-for-field equality with the source
   records for ... every dimension's
   dsp_compatibility/dsp_unavailable_reason/unavailable_reason/combined_compatibility".
   For a dimension whose #15 evidence is DSP-only, the source record's
   `HybridDimensionScore.unavailable_reason` is `no_evidence` --- a
   `HYBRID_CODES` member, which the first sentence forbids. Resolution:
   `CachedDimensionDecision.unavailable_reason` stores the Jev-side evidence
   reason, the source's `jev_unavailable_reason` or, when the source has none,
   the #13 code of the question the caller could not ask; #15's combined
   `no_evidence` is not stored. The round-trip test asserts that mapping and
   embeds only the judgments a real #13 question can reproduce.
3. **Two fields of the key matrix.** The matrix criterion asserts "one entry per
   field of the record" (twenty-two) but lists expectations for only twenty
   fields, omitting `cache_key_version` and `decision_kind`. Resolution: the
   matrix carries all twenty-two entries; changing `cache_key_version` builds a
   valid key that misses by digest, and changing `decision_kind` makes the key
   contradict the table's CHECKs, so it is `cache_key_invalid` and no digest is
   built.
4. **An identity's exact fields.** The criterion pins `DecisionCacheKey`'s
   field list but never enumerates `JudgmentIdentity` or
   `CandidateDecisionIdentity`. Resolution: each identity carries exactly the
   key fields its kind owns --- the ten shared identity fields, plus the kick
   and ranking fields for a candidate decision --- so no field exists that a key
   of that kind cannot carry.
5. **Bounds that cannot both hold.** The pruning criterion says the deletion
   continues "until both COUNT(*) <= max_entries and
   COALESCE(SUM(length(payload_json)), 0) <= max_bytes hold", together with
   "never the row just written". Because `MAX_PAYLOAD_BYTES` (65536) exceeds
   `MIN_CACHE_MAX_BYTES` (4096), a store whose own payload is larger than
   `max_bytes` cannot satisfy both. Resolution: never-evict-the-new-row wins;
   the row is written and the byte bound is exceeded by exactly that row.

Two further notes rather than contradictions: the criterion that the module
"imports only the standard library plus ... `backend.palette.ranking` and
`backend.library`" is satisfied without importing `backend.palette.model`,
because the palette hash and its version reach the key as caller-supplied
values that the test reads from #24; and "the pin also with from_dict(),
to_json() and from_json()" is honoured although no cache path ever decodes a
pin from storage.

## Tests

```
tests/test_decision_cache.py
tests/test_decision_cache_migration.py
tests/fixtures/cache/cache-cases.json
tests/fixtures/cache/key-matrix.json
```

`tests/test_decision_cache.py` covers the module surface, the key component
matrix (one entry per key field, iterated from the fixture), #24's palette hash
verbatim, the repeat request with zero sends, every #14 outcome state, the store
refusals and payload shapes, re-validation through #13, the corrupt, partial
and unsupported keep-don't-delete rules, two concurrent connections, the
recoverable write failure, both bounds and the pruning order, explicit
invalidation, the round-trip of a real `rank_hybrid` result, the
never-a-live-outcome rule, the privacy tokens and the document agreement.
`tests/test_decision_cache_migration.py` covers the fresh database, the upgrade
of a seeded version-3 database, the refusals with the file bytes unchanged, a
failing cache-column migration and the JSON functions the CHECK depends on.

## Local verification

Both commands were run from the repository root with the project interpreter.
`uv run pytest` cannot capture a subprocess in this sandbox (it fails with a
`CreatePipe` permission error), so the project interpreter is invoked directly
and `--basetemp` lives outside the repository; the focused run is:

```bash
.venv\Scripts\python.exe -m pytest tests/test_decision_cache.py tests/test_decision_cache_migration.py -q --basetemp %TEMP%\cache-focused -p no:cacheprovider
```

```
50 passed in 23.00s
```

and the full suite is:

```bash
.venv\Scripts\python.exe -m pytest -q --basetemp %TEMP%\cache-full -p no:cacheprovider
```

```
4 failed, 1991 passed, 1 skipped in 379.19s (0:06:19)
```

The baseline at the parent commit is `4 failed, 1941 passed, 1 skipped`, the
four failures being the sandbox-blocked `CreatePipe` `PermissionError` cases
(`tests/test_batch.py` twice, `tests/test_evaluation_manifest.py`,
`tests/test_evaluation_prepare.py`). This run carries exactly those four and
nothing else, so this task's own delta is zero: 1941 baseline passes plus the 50
tests it adds. `tests/test_library_scanner.py` asserted the exact eleven-name
user-table list, which migration 4 extends to thirteen, so that one expectation
was updated with the orchestrator's authorisation to name `decision_cache` and
`decision_model_versions` in sorted position, extend the `empty` tuple and
extend the comment; no assertion was weakened, deleted or reordered.

The bundled SQLite observed here is `sqlite3.sqlite_version` `3.47.1`, and
its JSON functions answer `json_valid('{}') = 1` and `json_type('{}') =
'object'`, which is what the payload CHECK depends on.

The `cache_stats` after the fixture run is recorded by replaying
`tests/fixtures/cache/cache-cases.json` through the module and is asserted by
`test_the_hand_written_cache_fixture_cases_replay`:

```bash
python -c "import importlib.util, os, sqlite3, tempfile; ..."   # see the driver in the issue comment
```

```
sqlite3.sqlite_version = 3.47.1
cache_stats after the fixture run = {"bytes": 870, "candidate_decisions": 0, "entries": 1, "judgments": 1, "newest_created_at": "2024-01-01T00:00:05Z", "oldest_created_at": "2024-01-01T00:00:05Z"}
```

The two tests that would otherwise be timing-dependent are made deterministic
rather than skipped: `TickingClock` replaces `utc_now` with one distinct
second per call, because `utc_now()` has one-second resolution and the
documented pruning order is `(created_at ASC, cache_key ASC)`; the concurrent
writer case uses two connections from `open_database` on one file and the
documented `busy_timeout`. No case is skipped, and the #14 live integration
stays UNVERIFIED by skip, exactly as #14 landed it: every judgment recorded in
this document's evidence comes from the contract double.
