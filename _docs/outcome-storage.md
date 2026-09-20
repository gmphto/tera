# Local recommendation outcomes

Issue #29 records explicit producer actions in the same local SQLite database as
projects and palettes. It does not infer an action from a view, hover, impression,
ignored card or palette state. The history contains identifiers and timestamps,
never an audio byte, path, file name, score, confidence, compatibility,
similarity, Jev payload or credential. It is not uploaded.

## One table and one order

Migration 5 adds only `recommendation_outcomes`. Its columns are `event_id`,
`client_event_id`, `event_type`, `project_id`, `palette_id`,
`palette_revision`, `run_id`, `candidate_id`, `ranking_version`, `mode`,
`candidate_analysis_version`, `removes_event_id` and `recorded_at`.
`event_id` is an `INTEGER PRIMARY KEY AUTOINCREMENT`. It is the insertion order
and the exclusive paging position; deleted ids are never reused.
`recorded_at` comes from `schema.utc_now()` in UTC. Equal timestamps are valid
and do not change event order. No client timestamp is accepted.

The table checks nonblank ids and timestamps, a nonnegative palette revision,
the four event types, the three `RecommendationBatch.mode` values, and the
equivalence between `removed` and a non-null `removes_event_id`. A removal's
link must name an earlier `event_id`. `project_id` references `projects` and
`palette_id` references `palettes`, both with `ON DELETE CASCADE`.
`removes_event_id` references `recommendation_outcomes(event_id) ON DELETE
CASCADE`. There is no foreign key to `samples` or to a run table. A pruned
sample or run therefore does not erase history; reads resolve neither.
Deleting a project cascades through its palettes and outcomes while preserving
the ids of other projects' rows.

The unique `ux_outcomes_client_event` constraint gives `client_event_id` its
idempotency meaning. The partial unique `ux_outcomes_removes` index on
`removes_event_id` (where non-null) permits at most one closure per selection.
`idx_outcomes_project(project_id,event_id)`,
`idx_outcomes_palette_candidate(palette_id,candidate_id,event_id)` and
`idx_outcomes_run(run_id,event_id)` support bounded history reads.
No other user table or column changes.

## Events and validity

`OUTCOME_EVENT_TYPES` is exactly `auditioned`, `selected`, `rejected` and
`removed`. `OUTCOME_SLOT` is the `bass` member of `MVP_SLOTS`.
`MAX_ID_LENGTH` is 128. An id cannot be blank, exceed this length, or contain
whitespace, a control character or a directory separator. `mode` must be a
`RecommendationBatch.mode` literal. The API accepts no unused field.

| Event | Meaning and palette relationship |
| --- | --- |
| `auditioned` | One explicit play or audition. Every occurrence is appended. No palette mutation. |
| `selected` | Opens one selection window after the candidate is active in the bass slot. Repeated selection while it stays active returns the existing event. |
| `rejected` | One explicit negative action. It changes no palette item and closes no selection. A live selection cannot be rejected. |
| `removed` | Closes the named `selected` event after the candidate is no longer active in the bass slot. The run, project, palette, candidate, revision, ranking mode and analysis version are copied from that selection. |

`auditioned`, `selected` and `rejected` submit `client_event_id`, `event_type`,
`project_id`, `palette_id`, `candidate_id`, `run_id`,
`palette_revision`, `ranking_version`, `mode` and
`candidate_analysis_version`. `removed` submits only `client_event_id`,
`event_type` and `removes_event_id`. The latter can be recorded arbitrarily
late. No sample row, file stat or audio decoding is required to write or read.

| Refusal | Code |
| --- | --- |
| Missing, blank, overlong, whitespace/control-containing or mistyped identity; invalid mode, revision or unused field | `invalid_outcome` |
| No project row | `unknown_project` |
| No palette row | `unknown_palette` |
| Palette belongs to another project | `cross_project_reference` |
| Submitted revision exceeds the palette's revision | `unknown_palette_revision` |
| Selection is not the active bass item | `selection_not_in_palette` |
| Removal link is absent or names a non-selection | `unknown_selection` |
| Removal link is already closed, or rejection targets a live selection | `outcome_conflict` |
| Removal's candidate is still active in the bass slot | `removal_not_reflected` |
| Same client event id with different content | `idempotency_conflict` |
| SQLite refuses the write or cannot acquire its busy lock | `write_failed`, `database_locked` |

Every validity read and insert in `record_outcome` runs inside one
`transaction(connection)`. Refusals and failed writes leave no partial event.
A retry with identical content and `client_event_id` returns the original
`OutcomeEvent` and `created: false`; a changed payload raises
`idempotency_conflict` with the stored `event_id`. A second selection with a
new client id while the candidate remains selected returns the live selection
with `created: false`. Auditions remain one row per occurrence. Rejections
deduplicate only on the client event id. After removal, a new selection starts
a new window. A rejection followed by selection and an audition after a
rejection or removal are valid in insertion order.

The palette write and event write are separate transactions, with the palette
first. A crash between them leaves a palette item with no logged selection;
a client retry can record it. No event is backfilled or inferred. An item
selected before this log has no `selected` event, so an attempted removal
event is `unknown_selection`. A deleted or pruned candidate can still be
recorded when the palette and item predicates hold, because no sample row is
validated here.

## Counting units for the retention report

One row is one occurrence. One `selected` row is one selection window starting
at its `recorded_at`. `ux_outcomes_removes` closes a window at most once, so it
is counted once as retained or removed. A repeated selection adds no row; a
re-selection after a removal opens a new window. An audition count groups
occurrence rows by `(palette_id, candidate_id)`. A rejection count groups rows
by `(palette_id, candidate_id, run_id)` and counts each such group once.
Issue #36 owns the report cutoff, censoring, denominator, retention window and
rates. This table computes and stores none of them; a removal 15 days after
selection still retains its original link and both timestamps.

## Repository and HTTP surface

`OutcomeSubmission` carries the submitted fields, `OutcomeEvent` all 13 stored
fields, and `OutcomeWrite(created, event)` the result.
`validate_submission(submission)` is pure and raises `invalid_outcome`.
`LibraryRepository.record_outcome(submission) -> OutcomeWrite` raises the
named `LibraryError` code above and commits before reporting success.
`LibraryRepository.list_outcomes(*, project_id=None, palette_id=None,
run_id=None, candidate_id=None, event_types=None, after_event_id=None,
limit=50) -> tuple[OutcomeEvent, ...]` reads only the outcome table in
ascending `event_id` order. `after_event_id` is exclusive, even if that id
has since been deleted.

`POST /outcomes` accepts the two closed bodies described above and returns
201 for a new row, or 200 for an idempotent or repeated-selection result.
Its body is `{"api_schema":"1.0","created":true|false,"outcome":{...}}`;
`outcome` has exactly the 13 table fields. Bad JSON uses `invalid_json`;
missing, mistyped and extra fields use `missing_field`, `invalid_field_type`
and `unknown_field`. The storage codes map one-to-one to the HTTP envelope.

`GET /outcomes` accepts optional `project_id`, `palette_id`, `run_id`,
`candidate_id`, repeated `event_type`, `limit` and `cursor`. Event types are
OR-ed. The page size uses the service's `DEFAULT_PAGE_SIZE = 50` and
`PAGE_SIZE_MIN = 1`, `PAGE_SIZE_MAX = 200`. The cursor uses the existing
`CURSOR_VERSION` and base64url canonical JSON `{"v":1,"after":event_id}`.
The result is `{"api_schema":"1.0","items":[...],"page":{"limit":50,
"count":0,"next_cursor":null,"has_more":false},"query":{...}}`.
`next_cursor` is non-null exactly when another row follows. Invalid event
types use `invalid_outcome`, malformed cursors `invalid_cursor`, invalid
limits `invalid_page_size` and unknown keys `unknown_query_parameter`.

`invalid_outcome` is HTTP 400. `unknown_project`, `unknown_palette` and
`unknown_selection` are 404. `unknown_palette_revision`,
`cross_project_reference`, `selection_not_in_palette`,
`removal_not_reflected`, `outcome_conflict` and
`idempotency_conflict` are 409. `write_failed` is 500.
`database_locked` is 503 with `Retry-After: 1`.
The service's Host, Origin, media-type, length, body-size, no-store and
one-line path-free access-log rules apply unchanged.

From the repository root, `uv sync` installs the declared dependencies.
The issue's focused verification command is `uv run pytest
tests/test_outcome_repository.py tests/test_outcome_migration.py
tests/test_outcome_recovery.py tests/test_api_outcomes.py`; `uv run pytest`
is its integration check. These commands are recorded here for later
verification, not as a claim that they have run.
