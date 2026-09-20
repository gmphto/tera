# Ranked recommendation cards (#33)

The panel that turns #28's ranked shortlist into cards. It renders, in this
order: the request identity, the state the run is in, the cards, the badges and
notes that sit beside them, and the client's own error vocabulary. Nothing here
re-ranks, re-scores or re-derives a value the response carried.

## The two requests

`recommendationsApi.ts` extends #30's one api slice with `injectEndpoints`, so
`app/api.ts` is unchanged, and declares exactly two endpoints:

| endpoint | method and path | body |
| --- | --- | --- |
| `getRecommendations` | `POST /recommendations` | `palette_id`, `revision`, `limit`, `filters` |
| `recordOutcome` | `POST /outcomes` | #29's ten keys |

`limit` is `CLIENT_RESULT_LIMIT = 10`, a client request value; `recommendationBody`
refuses anything outside #28's `[5, 20]` with a `RangeError` rather than sending
it. `filters` is `{}` — the landed palette state exposes no filter control — and
the builder copies only #32's two `FilterPolicy` booleans, so a stray key cannot
reach the wire. `getRecommendations` runs `parseBatch` in `transformResponse`, so
a 200 this client cannot read arrives as a closed client code instead of cards.

No cache tag is added: a refresh is the query's own `refetch`. The panel makes
exactly three kinds of request — #32's palette read/write, `POST /recommendations`
and `POST /outcomes` — and `recommendationsApi.test.ts` asserts that from the
request log.

## The panel's states

Exactly one renders at a time; `recommendations-degraded`, `exclusions-summary`,
`unscored-note` and `recommendations-alternatives` may appear beside one.

| state | element | attributes | when |
| --- | --- | --- | --- |
| idle | `recommendations-idle` | `data-reason="no_kick"` | no kick selected; no request is sent |
| loading | `recommendations-loading` | `data-slow`, `role="status"`, `aria-live="polite"` | a request is in flight |
| cancelled | `recommendations-cancelled` | — | the producer cancelled, or a new run superseded this one |
| stale | `recommendations-stale` | `data-run-revision`, `data-current-revision`, `role="alert"` | a conflict, or a response for another revision |
| empty | `recommendations-empty` | `data-empty-kind`, and `data-limit-reason` for `no_candidates` | a 200 with no results |
| error | `recommendations-error` | `data-error-source`, `data-error-code`, `data-retryable`, `role="alert"` | a #27 envelope, or a payload this client refused |
| unavailable | `recommendations-unavailable` | `data-service-phase` | the request never reached the service |
| list | `recommendations-list` | an `<ol>` of cards | results are present |

`data-slow` is `deriveRunNotice(elapsedMs)` — the documented boundary, 9999 →
`false`, 10000 → `true` — read from a clock the panel starts when a request
starts. The client adds no timeout: the request ends when #27's transport or
#28's budget ends it.

### The three empty kinds

A 200 with an empty `results` is never an error. `emptyKind(run)` decides which
copy is shown, from the counts the response returned:

- `no_candidates` — `eligible == 0` and `excluded == 0`, with `data-limit-reason`.
- `all_excluded` — `eligible == 0` and `excluded > 0`, with one `exclusion-row`
  per record of `run.exclusions` in the returned order, each carrying
  `data-exclusion-code` and `data-exclusion-count`. The label is the client's
  `EXCLUSION_LABELS` entry or, for a code this client does not know, the code
  verbatim.
- `all_unscored` — `eligible > 0`, `scored == 0` and `unscored > 0`.

When results *are* present and something was excluded, a single
`exclusions-summary` line carries `data-excluded-count` instead of the rows.

## A card

`data-testid="recommendation-card"` on the `<li>`, with `data-rank`,
`data-candidate-id`, `data-compatibility` and `data-confidence` (the returned
values as decimal strings), `data-uncertain`, `data-selected`, `data-jev`,
`data-state` — and nothing else. Compatibility and confidence are two separate
labeled elements, `card-compatibility` and `card-confidence`, each with
`data-value`; the visible percentages are `Math.round(value * 100)`.

The card renders **no** `similarity`. It is not in the model at all, so a result
with a known similarity and its twin with `similarity: null` produce byte-equal
markup, and a render test pins that.

`data-jev` is `present` when the result carries judgments and `none` otherwise,
with the copy `No Jev judgment — measured rules only`. This task renders no Jev
label, probability, dimension or model version — that is #35 — and a result with
no judgments is never described as Jev-scored.

`data-uncertain` is exactly #28's own rule: some warning begins with
`low_coverage` or `low_jev_confidence`. The client applies no numeric threshold,
so `confidence: 0.55` with no warning is not uncertain and `0.95` with a
`low_jev_confidence` warning is.

## Staleness and cancellation

The slice holds the active request identity — `palette_id`, `revision`, `limit`,
`filters`, kick id — and a serial that moves on every start. A response is
applied only when it answers the active serial *and* its
`batch.palette.revision` equals the revision that request asked for. Anything
else is counted in `droppedResponses` and rendered as nothing, so two runs' cards
can never be on screen together, and starting a run clears the previous cards
before the new response arrives.

A `revision_conflict` produces `stale` and re-issues **once** at the revision the
service named; a second conflict renders the same state and issues nothing
further. `cancel` aborts the request through RTK Query's own abort *and* moves
the serial on, so whatever a cancelled request settles with — an abort, a
transport error — is dropped by the same guard rather than becoming an error
state.

The conflict test is the envelope code, not the HTTP status: #29 answers
`409 outcome_conflict` for a rejection it refuses, and that is a card's error,
not a palette revision this run could re-issue against.

## Select and reject

`card-select` is one action with a fixed order, and the order is the rule:

1. #32's palette write for the `bass` slot with the batch's revision;
2. then `POST /outcomes` with `event_type: "selected"`.

`card-reject` sends the outcome alone and no palette write, and is disabled on
the card whose `candidate_id` is `batch.palette.selected_bass_id` — the service
refuses a rejection while a live selection exists.

Every value is copied from the batch, the run block, the result or the palette
state. One `client_event_id` (`crypto.randomUUID()`) is created when the action
starts and reused verbatim by every retry of it; a click while the action is
pending is ignored, so a double click produces one palette write and one outcome.
A `201 created: true` and a `200 created: false` are both success.

`data-state` is the visible outcome: `pending` while the action runs, `saved` only
after a 2xx — never optimistically — `rejected` after a recorded rejection,
`unrecorded` when the palette write landed but the outcome did not, and `error`
when the write itself failed. `card-action-error` carries `data-error-code` from
the envelope and `data-retryable`, which is true only for the documented
retryable set; a non-retryable code is shown verbatim with no Retry control.

## Landed names this task had to match

The issue's text was written before the #27/#28/#29/#30/#32 work landed. Where a
name differed, the landed name is used:

| the issue says | landed | where |
| --- | --- | --- |
| `results` in rank order | `batch.results`, contiguous `rank` from 1 | `batch.ts` |
| `data-state="saved"` on a `201` or a `200 created:false` | `OutcomeEnvelope.created` | `recommendationsApi.ts` |
| "the request log" | `installStubFetch`'s `calls` / `path()` | `stubFetch.ts` |
| the palette's two filter booleans | the palette state exposes none, so `filters: {}` | `recommendationsApi.ts` |
| #28's fixture file `tests/fixtures/api/recommendation-runs.json` | not used; `fixtures.ts` transcribes the shapes from #28's projection in `backend/api/schemas.py` and the landed types | `fixtures.ts` |

Two places where the response cannot express what the copy would like, and the
honest wording that was used instead:

- `jev_partial` — the run block carries no list of missing dimensions, so the
  badge says some dimensions are missing and names none. Inventing them would be
  the client deriving evidence the response did not return.
- A conflict that repeats at the same revision — the second `stale` records the
  revision that was actually requested (`6`/`6`), because that is what happened.
  The first conflict's `4`/`6` is not kept, since the client would be showing a
  request it had already superseded.

## Not yet done

The manual scenario in the acceptance criteria — the recorded state table from a
real window against a seeded database — has **not** been run, and neither has
`npm run tauri dev`. What exists instead is the automated evidence: 273 frontend
tests, of which 34 are the render cases above and 19 drive the real RTK Query
pipeline against the recording fetch double.
