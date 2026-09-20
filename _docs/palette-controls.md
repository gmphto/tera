# Palette controls (issue #32)

The palette panel: the stored palette, one kick select or replace, the optional
song context, and the revision discipline that keeps a stale answer off the
screen. Everything storage-shaped belongs to
[#24](https://github.com/gmphto/tera/issues/24) and everything transport-shaped to
[#27](https://github.com/gmphto/tera/issues/27); this task adds four routes, one
projection and one client contract.

## The four routes

| Method and path | Request | Success | Refusals |
| --- | --- | --- | --- |
| `GET /palette` | optional `project_id` | 200 `{"api_schema", "palette"}` | 400 `invalid_project_id`, 400 `unknown_query_parameter`, 404 `unknown_project` (`{project_id}`), 404 `unknown_palette` (`{palette_id}`), 503 |
| `POST /projects` | `{"name", "palette_name"?}` | 201 `{"api_schema", "palette"}` | 400 `invalid_body`/`missing_field`/`invalid_field_type`/`unknown_field`, 409 `project_exists` (`{project_id}`), 503 |
| `PUT /palette/items/{slot}` | `{"palette_id", "sample_id", "expected_revision"}` | 200 `{"api_schema", "changed", "palette"}` | 400 `invalid_palette_id`/`invalid_sample_id`/`invalid_revision`/`unknown_slot` (`{slot}`), 404 `unknown_sample` (`{sample_id}`)/`unknown_palette`, 409 `role_mismatch` (`{slot, accepted_roles, stored_role, sample_id}`)/`revision_conflict` (`{expected_revision, current_revision}`), 503 |
| `PUT /palette/context` | `{"palette_id", "expected_revision", "tempo", "key", "genre"}` | 200 `{"api_schema", "changed", "palette"}` | 400 `invalid_palette_id`/`invalid_revision`/`invalid_context` (`{field}`)/`unknown_field`/`missing_field`/`invalid_field_type`, 404 `unknown_palette`, 409 `revision_conflict`, 503 |

There is no removal route: removing an item is
[#36](https://github.com/gmphto/tera/issues/36). `POST /palette`, `GET /projects`
and `DELETE /palette/items/kick` are 405 with an `Allow` header naming the one
accepted method; `/palettes` and `/palette/items` are 404 `unknown_route`.

Each route calls #24's operations and nothing else: `handle_read` calls
`list_projects` / `get_project` and `load_palette`; `handle_create_project` calls
`list_projects` and `create_project`; the two writes call `set_palette_item` and
`set_palette_context`. `backend/api/palette.py` holds no SQL string, no weight, no
threshold, no dimension, no label and no Jev import.

## The palette projection

```json
{"api_schema":"1.0",
 "palette":{"palette_id":"palette-001",
   "project":{"project_id":"project-001","name":"Track A"},
   "name":"Main","revision":2,
   "context":{"tempo":{"state":"known","bpm":140.0},
              "key":{"state":"unknown","reason":"producer_marked_unknown"},
              "genre":{"state":"unset"}},
   "items":{"kick":{"slot":"kick","sample_id":"sha256:aaaa…","role":"kick",
                    "added_revision":1,"sample_state":"present",
                    "sample_error_code":null,"slot_role_mismatch":false},
            "bass":null}}}
```

`palette` has exactly `palette_id`, `project`, `name`, `revision`, `context` and
`items`. `project` has exactly `project_id` and `name`. `context` has exactly
`tempo`, `key` and `genre`, each one of `{"state":"known", …value keys…}`,
`{"state":"unknown","reason"}` or `{"state":"unset"}`, where the value keys are
`bpm`, then `tonic` and `mode`, then `genre`. `items` has exactly the `MVP_SLOTS`
keys; a slot is `null` or the seven item fields above, copied from #24's
`PaletteItemRecord`. No field is a timestamp, a path, a directory component, an
audio byte, a credential or a judgment.

## The three context states, and how they reach #24

A declared value is the producer's own statement about their song. It is written
with `MANUAL_CONTEXT_CONFIDENCE = 1.0`, which is above the `CONFIDENCE_THRESHOLD`
the compatibility and question layers apply to song tempo and key, so a value
entered here is actually used by the locks it feeds. It is not a measurement, and
the panel labels it "declared by you, not measured".

| Client state | `SongContext` written | Stored columns | Reads back as |
| --- | --- | --- | --- |
| `known` | the value with `confidence = 1.0` | the value and its confidence | `{"state":"known", …}` |
| `unknown` | a null value with the reason verbatim | NULL value, the reason | `{"state":"unknown","reason"}` |
| `unset` | a null value with `SONG_CONTEXT_ABSENT_REASON` | **all columns NULL** | `{"state":"unset"}` |

The read maps each field from #24's `context_state` (never from the reason
string), so an explicitly unknown tempo and an unset one produce different stored
rows and different wire bodies. `POST /projects` leaves all three `unset`, and no
route or client code invents a default tempo, key or genre.

## Revision, compare-and-set and conflicts

Both writes are compare-and-set through #24's `expected_revision`. A mismatch is
409 `revision_conflict` with both revisions and writes nothing, so a client
re-reads and retries. Re-selecting the sample already active in the slot is a
success with `changed: false` and no revision change, which is why two identical
submissions cannot produce two active kicks; the client replaces the slot's entry
in place rather than appending. `palette.revision` in every success body is the
stored counter after the mutation, never a client guess.

**The staleness rule #33 consumes.** A recommendation request carries the loaded
`palette_id` and `revision`, and a response for a lower revision is discarded. The
gate is `frontend/src/features/palette/state/revisionGuard.ts`:
`acceptsPaletteResponse(state, {paletteId, revision, issuedEpoch})` requires the
palette this client loaded, an epoch equal to the newest user action, and a
revision not older than the applied one. `revisionGuard.test.ts` proves the
recommendation-shaped case (a response for revision 6 after 7 was loaded) is
refused here rather than assumed downstream.

## The client

`paletteSlice.ts` holds `status` (`idle`/`loading`/`ready`/`no-project`/`error`),
`project`, `paletteId`, `revision` (-1 before the first response), `epoch`,
`items`, `context`, `draft`, `pending`, `conflict`, `lastError` and
`droppedResponses`. Every user action increments `epoch` and records
`{epoch, action}` as `pending`; the mutation endpoints capture the epoch when the
request is issued and dispatch the answer, which the reducer accepts or drops
through the guard. A second submission while `pending` is set is ignored, and the
control is disabled.

`MAX_CONFLICT_RETRIES = 1`: a conflict re-reads `GET /palette` and, when the
failed action is still the newest (`shouldRetryConflict`), retries the same
mutation once with the freshly read revision. A second conflict sets `conflict`
and issues nothing further.

`contextDraft.ts` holds `CONTEXT_FIELDS`, `CLIENT_UNKNOWN_REASON`,
`MAX_CONTEXT_TEXT_LENGTH = 64`, the contract's tonic and mode literals, the three
parsers and `toContextRequest` / `fromPaletteContext`. A blank genre is the
producer clearing the field, so it serializes as `unset`.

## The checkable surface

| Element | Attributes | Shown when |
| --- | --- | --- |
| `palette-panel` | `data-palette-state`, `data-revision` | always; `service-unavailable` while #30's origin is null, and no request is issued |
| `palette-kick` | `data-kick-state`, `data-sample-id` | the palette is loaded |
| `palette-kick-choose` | — | always; "Choose kick" in `none`, "Replace kick" otherwise |
| `palette-kick-reselect` | — | every state except `present` and `none` |
| `kick-picker`, `kick-picker-option`, `kick-picker-empty`, `kick-picker-error`, `kick-picker-close` | `data-sample-id` per option | the picker is open; one `GET /library/samples?role=kick` page per interaction |
| `palette-context` | `data-draft` | always |
| `palette-context-tempo` / `-key` / `-genre` | `data-context-state` | always |
| `palette-context-error` | `data-error-field` | a save failed |
| `palette-context-save`, `palette-context-save-retry` | — | always / after a failure |
| `palette-conflict`, `palette-conflict-retry` | — | a conflict is unresolved |
| `palette-project-create`, `palette-project-create-submit` | — | no project exists |

`data-kick-state` follows the projection: `none`, `role-mismatch` when
`slot_role_mismatch` is true, otherwise the item's `sample_state` (`present`,
`missing`, `unknown`, `removed`). In every non-`present` state the item stays
displayed with its slot, sample id and state, is never auto-removed or hidden,
and offers the reselect control; the panel says plainly that recommendations
cannot use it until it is available or replaced. Editing a sample's role is
[#72](https://github.com/gmphto/tera/issues/72) and pruning is
[#71](https://github.com/gmphto/tera/issues/71); this task adds no repair write.

## Recorded mappings from the landed code

1. **A cleared field needed storage support.** #32 was groomed with the mapping
   "`unset` becomes a null value whose reason is `SONG_CONTEXT_ABSENT_REASON` so
   the stored state is #24's `unset`". The landed `_context_field_state` reads
   *no value with a reason* as `unknown`, so that mapping stores an explicit
   unknown and a producer could never clear a field. `set_palette_context` gained
   an `unset` parameter that writes the field's columns NULL, which is the only
   way back to the state; a caller passing nothing keeps the previous behaviour.
2. **`failures`-style coercion does not apply here.** `#31`'s `toLibraryError`
   closes its union to the six library routes and coerces anything else to
   `internal_error`, so `revision_conflict` and `unknown_project` would be lost.
   `paletteApi.ts` reads the code and details straight from #27's envelope
   (`refusal(error)`), which both tasks share.
3. **A palette read grows with its items.** `_item_record` enriches one item at a
   time, so a read costs two more statements per item. `#172` owns batching it;
   `tests/test_api_palette.py` pins today's behaviour so a change there is
   visible.

## Boundaries

No measurement, similarity, compatibility, confidence, score or ranking is
computed or rendered. No path, no audio byte and no credential reaches the
client: the kick's file name comes from #27's sample route, which the panel calls
for the selected kick only, and a 404 there is the pruned-row case. No second
store, route table, code table, id pattern, envelope or version string is
created, and no request leaves the loopback interface.

## Reproducing the checks

```sh
cd frontend
npm ci
npx vitest run --reporter=verbose          # 12 files, 164 tests
npm run build                              # tsc --noEmit, then dist/
cd .. && uv run pytest tests/test_api_palette.py tests/test_api_contracts.py tests/test_api_policy.py
```

`npm test -- --reporter=verbose` is the documented form, but npm 12 parses
`--reporter` as one of its own flags and exits `EUNKNOWNCONFIG`; `npx vitest run
--reporter=verbose` is the identical run.
