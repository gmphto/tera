# Recommendation API

`POST /recommendations` answers one question for the desktop client: given this
stored palette and its selected kick, which basses should I audition next? It is
issue #28's operation on issue #27's local service: the one route that loads a
stored palette, admits candidates through #11's deterministic filters, cuts them
through #25's bounded shortlist, resolves the five weighted #13 questions through
#14 -- from #26's cache first and live only for the misses -- and ranks them
through #15's hybrid ranking.

The route is synchronous and loopback-only. It returns a schema-1
`RecommendationBatch` projection plus one closed `run` block that records every
version, count, exclusion and evidence source behind it. It publishes no path, no
audio byte, no credential and no endpoint, stores no run row of its own, and
offers no cancel route: cancellation is the client going away or the budget
expiring, and the deterministic `run_id` makes two identical requests
interchangeable.

This document is the contract of `backend/api/recommendations.py`. Where the
implementation consumes a landed name, the landed name is used here; the mapping
from the issue's grooming-time names is recorded in "Consumed operations".

## The route

| Method | Path | Request | Success |
| --- | --- | --- | --- |
| POST | `/recommendations` | `{"palette_id", "revision", "limit", "filters"}` | 200 |

```json
{"palette_id": "palette-00000000000000000000000000000001", "revision": 3,
 "limit": 10, "filters": {"tempo_lock": true, "exact_key_lock": false}}
```

The success body is exactly three members:

```json
{"api_schema": "1.0", "recommendation": {"...": "the batch projection"}, "run": {"...": "the run block"}}
```

The response carries `Content-Type: application/json; charset=utf-8`, an
accurate `Content-Length`, `Cache-Control: no-store` and
`X-Content-Type-Options: nosniff`. Every non-2xx body is #27's one envelope
`{"api_schema": "1.0", "error": {"code", "message", "details"}}` with a constant,
path-free, traceback-free sentence of at most `MAX_MESSAGE_LENGTH` = 200
characters.

### The request

| Field | Type | Required | Default | Bounds |
| --- | --- | --- | --- | --- |
| `palette_id` | text | yes | -- | #27's id rule, the `run_id` pattern `[A-Za-z0-9_-]{1,64}`, never a second pattern |
| `revision` | integer | yes | -- | a non-boolean whole number >= 0 |
| `limit` | integer | no | `DEFAULT_RESULT_LIMIT` = 10 | `[RESULT_LIMIT_MIN, RESULT_LIMIT_MAX]` = [5, 20] |
| `filters` | object | no | `{}` | exactly the keys `dataclasses.fields(FilterPolicy)` declares: `tempo_lock`, `exact_key_lock`, each a JSON boolean |

The object is closed: an unknown top-level field, and an unknown `filters`
key, are `unknown_field` with `details.field` = the field name (`filters.<key>`
inside `filters`). `limit: 4`, `limit: 21`, `limit: 1.5`, `limit: true` and
`limit: "10"` are all `invalid_limit`; `revision: -1`, `1.5`, `true`, `"3"`
and `null` are all `invalid_revision`. The filters are handed to `#11`'s
`FilterPolicy(tempo_lock=..., exact_key_lock=...)`, so an explicit boolean
policy, not this route, decides admission.

No refusal echoes a rejected value: `details` carries fixed field names, the
request's own palette id only where the response is about a well-formed palette
that does not exist or cannot be used, and the two revision numbers for a
conflict. A malformed request performs no palette read beyond what validation
needs, no ranking, no Jev call and no cache write; a valid one that the palette
refuses performs no ranking, no Jev call and no cache write either.

### The error codes of this route

| Status | Code | `details` | When |
| --- | --- | --- | --- |
| 400 | `invalid_json` | `{}` | The body is not valid JSON (#27) |
| 400 | `invalid_body` | `{}` | The body is not a JSON object |
| 400 | `missing_field` | `{"field"}` | `palette_id` or `revision` is absent |
| 400 | `invalid_field_type` | `{"field"}` | `filters` is not an object, or a lock is not a boolean |
| 400 | `unknown_field` | `{"field"}` | An unknown top-level field or filter key |
| 400 | `invalid_palette_id` | `{}` | `palette_id` is not `[A-Za-z0-9_-]{1,64}` |
| 400 | `invalid_revision` | `{}` | `revision` is not a nonnegative whole number |
| 400 | `invalid_limit` | `{}` | `limit` is not an integer in [5, 20] |
| 403 | `host_not_allowed`, `origin_not_allowed` | `{}` | #27's loopback policy, decided before any read |
| 404 | `unknown_route` | `{}` | Any other path, including `/recommendations/{run_id}` |
| 404 | `unknown_palette` | `{"palette_id"}` | No stored palette has this well-formed id |
| 405 | `method_not_allowed` | `{}` | `Allow: POST` |
| 409 | `revision_conflict` | `{"expected_revision", "current_revision", "phase"}` | The palette is not at the request's revision, or changed or vanished while the run worked |
| 409 | `palette_incomplete` | `{"palette_id"}` | The palette has no active kick, or its selected bass cannot be supplied |
| 409 | `kick_unavailable` | `{"sample_id", "reason"}` | The palette's selected kick cannot be used |
| 411 | `length_required` | `{}` | No valid integer `Content-Length` |
| 413 | `request_too_large` | `{}` | The body is larger than `MAX_REQUEST_BYTES` = 65536 |
| 415 | `unsupported_media_type` | `{}` | No `Content-Type: application/json` |
| 500 | `internal_error` | `{}` | A defect outside a validated request; no fixture case produces one |
| 503 | `database_unavailable` | `{}` | The library database is not available |
| 503 | `server_busy` | `{}` | No request slot inside `REQUEST_QUEUE_TIMEOUT_SECONDS`; `Retry-After: 1` |

`kick_unavailable`'s `reason` is exactly one of #11's eight kick-side codes
(`wrong_role`, `file_missing`, `file_unreadable`, `availability_unknown`,
`empty_audio`, `silent_audio`, `unusable_analysis`, `inconsistent_analysis`)
or #21's non-current analysis state (`stale`, `pending`, `failed`, `absent`).
No new reason literal exists. `file_unreadable` is #11's forward-compatible
state: this schema stores `present`, `missing` and `unknown`, so a stored row
cannot report it today and the mapping keeps the literal for the day one can.

## The response: the batch projection

`recommendation` is a schema-1 `backend.contracts.RecommendationBatch`,
projected field for field. Building the contract record first and projecting it
second is what makes the block valid by construction: a projection that broke a
contract rule would be refused by the record's own validation before any bytes
are written.

| Contract record | Fields | Notes |
| --- | --- | --- |
| `RecommendationBatch` | `run_id`, `palette`, `samples`, `results`, `ranking_version`, `mode`, `alternatives`, `schema_version` | exactly the contract's eight |
| `PaletteContext` | `palette_id`, `revision`, `kick_id`, `selected_bass_id`, `song`, `schema_version` | #24's stored context, absent fields carrying their reason |
| `SongContext` | `tempo`, `key`, `genre`, `genre_unavailable_reason` | passed through, never defaulted |
| `Sample` | `sample_id`, `role`, `audio`, `features`, `analysis_version`, `schema_version` | the selected kick plus every sample a result or an alternative references, and the palette's selected bass |
| `AudioMetadata` | `sample_rate_hz`, `channels`, `frame_count`, `duration_ms` | **the one deviation**: `local_path` is dropped, exactly as #27's feature route projects a sample's audio |
| `AudioFeatures` | `measurements`, `key` | every `MEASURES` name exactly once, with the contract unit |
| `Measurement` | `name`, `value`, `unit`, `unavailable_reason`, `confidence` | an unknown value keeps `value: null`, its stored reason and `confidence: null`; nothing is imputed, normalised, rounded, renamed or derived |
| `MusicalKey` | `tonic`, `mode`, `confidence`, `unavailable_reason` | passed through with its own reliability and reason |
| `RankedCandidate` | `candidate_id`, `analysis_version`, `rank`, `compatibility`, `confidence`, `similarity`, `similarity_unavailable_reason`, `dsp_dimensions`, `jev_judgments`, `reasons`, `warnings` | `rank` contiguous from 1; `similarity` is #25's own value |
| `DimensionScore` | `dimension`, `compatibility`, `unavailable_reason` | #12's three DSP dimensions, unchanged |
| `JevJudgment` | `dimension`, `label`, `confidence`, `probabilities`, `model_version`, `prompt_version`, `unavailable_reason` | one per judged dimension; an unavailable dimension contributes no record at all |
| `LabelProbability` | `label`, `probability` | the five labels in contract order |

`tests/test_api_recommendations.py` walks the projected block by path and
compares every record's key set with `dataclasses.fields` of the eleven
contract records, so any contract change fails the test; it also asserts that no
`local_path` or `original_path` string appears anywhere in the block.
`backend/contracts.py` is not edited and `SCHEMA_VERSION` stays `"1.0"`.

Facts and judgments stay separate: `dsp_dimensions` is #12's/#15's own
`DimensionScore` tuple, `jev_judgments` holds only validated #13/#14 judgments
with a label, a #13 `model_abstained` abstention is carried with its null label
and empty probabilities when #15 recorded it, and an unavailable or unasked
dimension is represented by #15's reason and warning strings alone -- never by an
invented label, confidence, probability or model version.

The client's uncertainty signal is exactly #15's: a returned result is uncertain
precisely when one of its warnings begins with `low_coverage` or
`low_jev_confidence`. The wire contract has no `uncertain` field and this
route adds none.

## The response: the run block

`run` is this task's own closed block. Every value is copied from a landed or
prepared source name; this route coins no version string, no reason code, no
dimension name, no label and no weight.

| Member | Fields | Source |
| --- | --- | --- |
| `run_id` | the 64-character digest below | this route |
| `palette_hash` | 64 lowercase hex | #24's `palette_hash(record)` |
| `analysis_version` | the current version | #9's `digest(analysis_descriptor())` |
| `filters` | `policy_version`, `tempo_lock`, `exact_key_lock` | #11's `FilterResult.policy_version` and the request's policy |
| `retrieval` | `policy_version`, `representation_version`, `normalization_id`, `shortlist_size_requested`, `shortlist_size_returned`, `limit_reason`, `duplicate_candidates`, `skipped_stale_analysis`, `skipped_absent_analysis` | #25's `RetrievalResult` and `StoredRetrieval` counts |
| `ranking` | `ranking_version`, `weight_table_id`, `jev_status` | #15's `HybridResult` only |
| `jev` | `prompt_version`, `adapter_version`, `model_versions` | #13's and #14's constants, and the sorted unique model versions of the returned judgments |
| `counts` | the seven counts below | this route |
| `exclusions` | one `{"code", "count"}` per excluded code, sorted by code | #11's `ExclusionReason` codes |
| `evidence` | `interface`, `double`, `unavailable`, `cache`, `cache_errors` | this route's tally over #14's outcomes and #26's lookups |

`run.jev.model_versions` contains every `model_version` that appears in the
returned `jev_judgments` and nothing else, sorted and unique; it is empty when
no judgment was used. The reported `ranking_version` and `weight_table_id` are
always the values the ranking result itself produced, so a DSP-only fallback
reports #12's own `dsp-baseline-v1` and `dsp-baseline-weights-1` rather than a
hybrid name.

### Counts arithmetic

```
counts.eligible    + counts.excluded == the candidate ids #11 was given
counts.shortlisted                    == retrieval.shortlist_size_returned
counts.scored                         == len(hybrid.ranked)
counts.unscored                       == len(hybrid.unscored)
counts.requested                      == request.limit
counts.returned                       == len(recommendation.results)
counts.returned                       == min(counts.requested, counts.scored)
```

A 12-eligible run with `limit: 10` returns 10 results and `scored: 12`; a
three-candidate run returns three with no error. `shortlist_size_requested` is
#25's `DEFAULT_SHORTLIST_SIZE` = 100, chosen by the service and never by the
client, and is always inside #25's `[SHORTLIST_MIN, SHORTLIST_MAX]` = [50, 100].

### The exclusion tally

`run.exclusions` counts #11's codes over the excluded candidates, one record per
code, sorted by code, counts only: an excluded candidate contributes one to every
code it carries, and no per-candidate exclusion record is returned. A candidate
with a silent peak carries `silent_audio` and, because its RMS exceeds that
zero peak, `inconsistent_analysis`; both appear.

### Evidence accounting

`run.evidence` counts, for every weighted dimension of every scored candidate,
exactly one of four sources:

```
evidence.interface + evidence.double + evidence.unavailable + evidence.cache == 5 x counts.scored
```

- `interface` -- a judgment obtained in this run from a #14 outcome whose
  transport carries `source = "interface"`;
- `double` -- the same for `source = "double"`;
- `unavailable` -- a dimension with no usable judgment: a question #13 refused to
  ask, or a #14 outcome whose state is `not_attempted`, `invalid_result`,
  `timed_out`, `service_error` or `unavailable`. An abstention is a judged
  response and is counted under its source, never under `unavailable`;
- `cache` -- a judgment served by #26's cache. A hit is counted only here, even
  though its stored origin was a live interface call.

`evidence.cache_errors` counts cache reads that raised and were treated as
misses. Only scored candidates' dimensions are counted, so a candidate that
cannot be scored contributes nothing to the invariant and nothing to the
response.

## `run_id`

`recommendation.run_id` and `run.run_id` are the same bare 64-character
lowercase hex digest, produced by #9's `backend.analysis.batch.digest` (no
`sha256:` prefix, which belongs to a sample id). The digest covers exactly:

- the palette id, `palette_hash` and `revision`;
- the kick's sample id and analysis version;
- the request's `limit`, `tempo_lock` and `exact_key_lock`;
- the analysis version;
- #25's `RETRIEVAL_POLICY_VERSION`, `REPRESENTATION_VERSION` and
  `normalization_id`, the ordered shortlist ids and
  `shortlist_size_requested`, `shortlist_size_returned` and `limit_reason`;
- #15's `HYBRID_RANKING_VERSION` and `HYBRID_WEIGHT_TABLE_ID`;
- #13's `PROMPT_VERSION` and #14's `ADAPTER_VERSION`.

Nothing else enters it: no clock, timestamp, pid, path, file name, credential,
endpoint, transport, cache state or observed judgment. Two runs over the same
palette and library with the same versions therefore return the same `run_id`
and a byte-identical `recommendation` block; a cold-cache and a warm-cache run
differ only in `run.evidence`. A changed palette revision or hash, limit,
filter, kick, analysis version, normalization id, shortlist or version constant
changes `run_id`.

**One limitation.** A run cut short by the budget shares its `run_id` with a
complete run of the same request, because the identity covers the evidence set
the run was built on, not how much of it was answered; `run.counts`,
`run.evidence` and the per-dimension reasons are the record of what it used.
The other limitation is that no run is stored: the plan's storage model lists no
run table, the response is reproducible for the same evidence, and a stored run
header needed to replay a past ranking from storage alone is a new storage task,
not part of this one.

## Ordering and bounds

- `recommendation.results` is the first
  `min(request.limit, len(hybrid.ranked))` records of #15's own rank order,
  projected field for field. A candidate is never invented, padded or dropped to
  fill the count: fewer results are returned only when fewer candidates were
  scored.
- `recommendation.alternatives` is #15's `HybridResult.alternatives` unchanged.
  Because `RESULT_LIMIT_MIN` = 5 exceeds #15's `ALTERNATIVES_LIMIT` = 3, every
  alternative is inside the returned results; the route asserts
  `set(alternatives) <= set(result ids)` for every fixture case.
- The Jev stage is sent at most `5 x shortlisted` questions, in shortlist order,
  in batches of at most #14's `MAX_BATCH_SIZE` = 100, so a 100-candidate
  shortlist sends at most five batches. No question is built for a candidate
  outside the shortlist, the unweighted `rhythmic` dimension is never asked and
  never supplied, and no Jev call happens for an empty or all-excluded shortlist.
- Library reads are bounded and do not grow with the shortlist. One 100-candidate
  run executes the same 26 reads as a 12-candidate run: this route's palette read
  before the run and after ranking (seven statements each, because #24's record
  resolves every item), its kick row (two), #25's own kick and population reads
  (eight) and the single keyed shortlist sample read (two). #26's own per-decision
  reads are excluded from that bound.
- `RECOMMENDATION_BUDGET_SECONDS` = 25.0 is strictly below #27's
  `REQUEST_TIMEOUT_SECONDS` = 30, so the route always gets to write a body. The
  budget is measured with the injectable `collaborators.monotonic` and no other
  clock. The handler passes one `cancelled` callable to #14's
  `score_questions` and to its own batch boundaries.

## Cache first, then live

For every built question the runner asks #26's cache before #14, through the two
landed operations: `pinned_model_version` resolves the model version a stored
judgment could carry (with no pin no key can name one, so the question is a miss
and no read happens), `judgment_key` builds the canonical key, and
`lookup_judgment` returns the stored decision with its entry timestamp and the
explicit `hit`/`miss` status. On a hit the stored document has already been
re-validated through #13's `validate_response`, so the returned judgment is the
same value a live call would have produced -- and it is recorded as served from
the cache. On a miss the question is sent to #14 and the validated outcome is
stored through `store_judgment` with the source it was obtained with; the key is
rebuilt from the judgment's own model version, so a version the pin did not name
is stored under the version it actually was.

Two details matter:

- A #26 read failure is a miss and is counted in `run.evidence.cache_errors`; a
  #26 write failure leaves the response and its status unchanged. A stored
  document that #13 rejects is never used or re-served: the dimension is a miss
  and then `unavailable` if the live path also fails.
- Decisions are written only after the palette is confirmed unchanged, so a run
  refused for a palette that moved during the run stores nothing.

## Behaviours, each with one worked example

Requests below use the synthetic ids of the fixtures; the responses are
abridged. Every case is one entry of
`tests/fixtures/api/recommendation-runs.json` and is replayed by
`tests/test_api_recommendation_flow.py` against a real service.

### Empty library

`{"palette_id": "palette-...", "revision": 1, "limit": 10}` against a kick-only
library:

```
200 {"recommendation": {"mode": "dsp-only", "results": [], "alternatives": [],
                        "samples": [the kick]},
     "run": {"counts": {"eligible": 0, "excluded": 0, "shortlisted": 0, "scored": 0,
                        "unscored": 0, "requested": 10, "returned": 0},
             "retrieval": {"limit_reason": "no_candidates"},
             "ranking": {"jev_status": "jev_absent"},
             "evidence": {"interface": 0, "double": 0, "unavailable": 0, "cache": 0,
                          "cache_errors": 0}}}
```

Zero Jev sends, zero questions built, `run_id` still computed, never a 4xx or
5xx. A kick-only library is the same shape.

### All-excluded library

Two candidates, one silent (zero peak) and one missing file: 200 with
`results: []`, `counts.eligible: 0`, `counts.excluded: 2`,
`retrieval.limit_reason: "no_candidates"` (which is #25's own value for an empty
admitted set) and
`exclusions: [{"code": "file_missing", "count": 1}, {"code": "inconsistent_analysis", "count": 1}, {"code": "silent_audio", "count": 1}]`.
The palette's selected bass is still supplied in `samples`, because the contract
requires it whenever `selected_bass_id` is set.

### A stale, missing, deleted or unusable kick

One code, `409 kick_unavailable`, with `details: {"sample_id", "reason"}` per
stored fact:

| Stored fact | `reason` |
| --- | --- |
| the sample row's role no longer matches the kick slot (#72) | `wrong_role` |
| the sample row is gone (#71 pruning) | `absent` |
| `file_status` is `missing` | `file_missing` |
| `file_status` is `unknown` | `availability_unknown` |
| zero frames | `empty_audio` |
| a zero peak | `silent_audio` |
| an unknown peak | `unusable_analysis` |
| RMS above the peak beyond #11's roundoff | `inconsistent_analysis` |
| another analysis version stored | `stale` |
| nothing stored, a queued item | `pending` |
| nothing stored, a failed item | `failed` |
| nothing stored, nothing queued | `absent` |

The specific code is derived from the same stored facts in #11's documented check
order, because #11's `filter_candidates` reports one generic `invalid_kick`
for all of them (recorded below). No palette, queue, run or cache row is written.

### Revision conflict

The request's `revision` is compared with the palette read in the same
transaction as the palette. A mismatch before ranking:

```
409 {"error": {"code": "revision_conflict",
               "details": {"expected_revision": 1, "current_revision": 2,
                           "phase": "before_ranking"}}}
```

The palette is read again after #15 has ranked. A palette that changed or was
deleted while the run worked is refused with the same code and
`phase: "after_ranking"`, `current_revision` = the new revision, or `null`
when the palette is gone. The run publishes no result, writes no cache entry and
can never be mistaken for the newer palette's. Ranking and the Jev call have
necessarily already happened when the change is first observable there; see the
recorded conflicts.

### Palette incomplete

A palette with no active kick, or whose selected bass has no current analysis or
is no longer a bass, is
`409 palette_incomplete` with `details: {"palette_id"}`. #24's `to_context()`
raises it for the kick; the bass case is the same code because the palette cannot
be assembled into a contract batch, and no other code exists for it.

### Unavailable Jev and the DSP-only fallback

With credentials absent, #14 records `credentials_absent` for every question
with zero sends, and the run is an honest DSP-only 200:

```
200 {"recommendation": {"mode": "dsp-only", "results": [three records with
                        "jev_judgments": []]},
     "run": {"ranking": {"jev_status": "jev_absent"},
             "evidence": {"interface": 0, "double": 0, "unavailable": 15, "cache": 0,
                          "cache_errors": 0}}}
```

Every result is field for field the projection of `rank_candidates(kick,
shortlist_samples, policy=RankingPolicy())` for the same inputs, asserted
against the #12 entry point at runtime rather than against a stored expectation.
A timeout and a service error produce the same shape with `unavailable` for
their dimensions; an unreachable endpoint, an invalid result, a question that was
never asked and a #13 `UnavailableQuestion` code such as `song_context_absent`
or `kick_key_unknown` are all supplied to #15 with that upstream code copied
verbatim as `unavailable_reason`, and each is visible through #15's
`jev_evidence_unavailable` or `no_evidence` reason and warning strings.

When some dimensions or candidates have judgments, `mode` is `"hybrid"` and
`jev_status` is `"jev_partial"` or `"jev_present"` exactly as #15 defines; a
candidate with DSP evidence only is ranked with the `candidate_no_jev_evidence`
warning.

### A candidate that cannot be scored

A fixture library containing one candidate with no usable weighted dimension and
one fully scored candidate returns 200 with `counts.unscored: 1`,
`counts.scored: 1`, exactly one result, and the unscorable candidate's id
neither in `results` nor in `samples`. There is no per-candidate record, no
`compatibility` of 0.0 and no placeholder: the per-candidate wire form for an
unscorable candidate is [#62](https://github.com/gmphto/tera/issues/62)'s
contract change, which needs the user's approval first.

### Cache hit on the second request

Two identical requests against one unchanged palette:

| | first request | second request |
| --- | --- | --- |
| `evidence.interface` | 0 | 0 |
| `evidence.double` | 60 | 0 |
| `evidence.unavailable` | 0 | 0 |
| `evidence.cache` | 0 | 60 |
| `evidence.cache_errors` | 0 | 0 |
| sends | 60 | 0 |
| `run_id` | the same | the same |
| `recommendation` | the same bytes | the same bytes |

12 candidates x 5 weighted dimensions = 60. The second response differs from the
first only in `run.evidence`: a decision obtained before the request is reused
evidence, never a live Jev outcome. With credentials absent and a warm cache the
run is still `mode: "hybrid"`, `cache == 5 x scored`, and it sends and writes
nothing: `decision_cache.cache_stats` is unchanged.

### Cancelled: the budget and the client going away

The budget is checked before each Jev batch. When it has expired the Jev stage
stops at that batch boundary, the evidence obtained so far is kept, the remaining
dimensions stay `unavailable` with #14's own code (`cancelled`), and the route
returns 200 with the honest `mode`, `jev_status`, `counts` and `evidence` --
never a 5xx, never a fabricated judgment, never a partially written body. A
scripted transport that blocks inside a batch returns within the budget plus two
seconds, `/health` answers within one second throughout, and the following
request succeeds.

When the requesting client disappears, the handler stops at the same stage
boundary, publishes nothing, releases its concurrency slot, leaves the stored
palette, the #23 rows and every library row unchanged, and records no outcome;
the transport receives no batch after the abort, and the follow-up identical
request succeeds, returns the same `run_id` and reports the decisions obtained
before the abort under `cache`.

## Consumed operations

| This route consumes | Landed name |
| --- | --- |
| #9 identity and version | `backend.analysis.batch.digest`, `analysis_descriptor` |
| #11 admission | `FilterPolicy`, `FilterResult`, `ExclusionReason`, `Availability`, `filter_candidates` (through #25) |
| #12 DSP baseline | `rank_candidates`, `RankingPolicy`, `RankingResult` |
| #13 questions | `build_question`, `JevQuestion`, `UnavailableQuestion`, `PROMPT_VERSION` |
| #13 validation | `validate_response` (through #26) |
| #14 adapter | `score_questions`, `JevAdapterConfig`, `JevCredentials`, `JevScoringRequest`, `JevScoringRun`, `JevOutcome`, `MAX_BATCH_SIZE`, `ADAPTER_VERSION`, `HttpJevTransport` |
| #15 hybrid ranking | `rank_hybrid`, `HybridPolicy`, `HybridEvidence`, `HybridResult`, `HYBRID_WEIGHTED_DIMENSIONS`, `HYBRID_RANKING_VERSION`, `HYBRID_WEIGHT_TABLE_ID`, `LOW_CONFIDENCE_THRESHOLD` |
| #21 storage | `LibraryRepository.load_palette`, `list_retrieval_rows`, `sample_detail`, `open_database`, `LibraryRepository.delete_sample` (tests only) |
| #23 queue | `job_items` state through #21's read; the fixtures use `queue.enqueue` |
| #24 palette | `load_palette`, `PaletteRecord.to_context()`, `palette_hash`, `PALETTE_HASH_VERSION`, `PaletteIncomplete` |
| #25 retrieval | `backend.library.retrieval.retrieve_shortlist`, `StoredRetrieval`, `RetrievalPolicy`, `DEFAULT_SHORTLIST_SIZE`, `RETRIEVAL_POLICY_VERSION`, `REPRESENTATION_VERSION`, `SIMILARITY_UNAVAILABLE_REASONS` |
| #26 cache | `pinned_model_version`, `record_model_version`, `judgment_key`, `JudgmentIdentity`, `lookup_judgment`, `store_judgment` |
| #27 service | `ApiError`, the one `ERROR_CODES` table, `ROUTES`, `API_SCHEMA_VERSION`, `MAX_REQUEST_BYTES`, `REQUEST_TIMEOUT_SECONDS` and the Host, Origin, media-type, length and loopback rules |

Recorded mappings from the issue's grooming-time names to the landed ones:

- `retrieve_shortlist` lives in `backend.library.retrieval` (the storage-facing
  module), not in `backend.palette.retrieval` (the pure one); it is called with
  the palette's kick id, not with a `Sample`.
- #26's cache operations are `lookup_judgment` and `store_judgment` keyed by
  `judgment_key(identity, question)`; the two-operation shape the issue's
  Constraints section describes is met by `pinned_model_version` plus those
  three names, never by a second key or table.
- #11's kick-side gate reports one generic `invalid_kick`; the response's
  specific `reason` is named from the same stored facts (recorded below).
- #21 exposes every read this task needs, so
  `backend/library/repository.py` is unmodified.

## Credential, path and audio policy

No response body, header, error message or access-log line contains a
`TERA_JEV_*` value, the configured endpoint or API key, an absolute or
root-relative path, a directory component, a file name, a pack name or an audio
byte; every response is `application/json` only. The projection carries the
sample ids, roles, measurements and judgments the client needs and no
`local_path`, so the client never receives a filesystem path from this route.
This module reads no credential value itself -- #14's `JevCredentials.from_env`
does -- imports no outbound client, and opens no socket: the transport #14 owns
does the sending. With credentials absent, an outbound-socket patch that makes
`socket.create_connection` and `socket.socket.connect` raise observes zero
connection attempts, at both a cold and a warm cache.

The route runs on the handler's own thread with one short-lived connection of its
own, opened through #21's `open_database` and closed in the same request,
exactly as #23's runner thread holds its own connection. #27's single
handler-side database thread runs one statement sequence at a time, so running a
whole recommendation -- including the Jev stage, which waits on a transport --
through it serialized even `GET /health` behind the run and broke the one-second
health bound the budget criterion requires: measured before the change, `GET
/health` returned after `30204` ms while the transport was blocked, and
`app.health()` waited `29.806` s behind a blocking `app.database.run` (probe
4). After the change, with the transport blocked inside the run, `GET /health`
answers in under one second -- the budget case asserts it -- and the follow-up
request still succeeds. No other route changes.

## Recorded conflicts and deviations

1. **#12 raised on an admitted candidate with an unknown band (owner #12).**
   The criterion "a candidate admitted with unknown F0, key, loudness, bands or
   envelope (which #11 allows) therefore appears with its nulls and reasons and is
   never re-measured, re-read or filled in" could not hold for bands:
   `backend/palette/ranking.py`'s `_frequency_evidence` computed
   `sum(candidate_bands.values())` with no unknown guard, so
   `rank_candidates(kick, (candidate,), policy=RankingPolicy())` raised
   `TypeError: unsupported operand type(s) for +: 'int' and 'NoneType'` at
   `ranking.py:162`, which this route can only turn into a 500 -- forbidden by
   the same issue. **Resolved under the orchestrator's authorization**: the
   minimal guard landed in `_frequency_evidence` (issue #28's recorded
   conflict), returning #12's own unavailable score with
   `kick_band_unknown` or `candidate_band_unknown` for an incomplete band set;
   #12 owns that semantics, and `tests/test_dsp_baseline.py` carries the two
   focused regression rows. The unknown F0, key and loudness paths already
   worked: `_transient_evidence` and `_tonal_evidence` return #12's own
   unavailable scores, and an unknown loudness only makes #25's similarity
   unavailable. The route's fixture case keeps a candidate with unknown bands,
   an unknown key and an unknown transient position and asserts that it is
   counted as unscorable with its nulls and reasons intact
   (`test_an_unknown_band_takes_the_unavailable_path`). The two new codes are
   recorded here because `_docs/dsp-baseline.md` is outside this task's file
   set; #12's own document is the place they belong.

2. **#26's judgment key carries no ranking version (owner #26).** The criterion
   "a decision stored by #26 under the old key is not reused under the new one"
   cannot hold for a `HYBRID_RANKING_VERSION` or `HYBRID_WEIGHT_TABLE_ID` bump:
   #26's landed `DecisionCacheKey` built by `judgment_key` carries the palette
   hash, candidate identity and content, analysis, prompt, adapter and model
   versions and the question id, and no ranking or weight-table version (only
   `CandidateDecisionIdentity` carries those). Rebuilding the same question's
   judgment key with the ranking constant replaced yields an identical
   `cache_key_digest` (probe below). The response and `run_id` do change on
   every one of those bumps, which is what the mandated test asserts.

3. **"In both cases no ranking, no Jev call and no cache write happen for the
   refused run" (the body's own sentence).** The conflict is detected by reading
   the palette again *after* ranking, which is what the `phase: "after_ranking"`
   literal means; ranking and the Jev call have necessarily already happened by
   then. The part that can hold does: no cache entry is written for a refused
   run, because decisions are stored only after the palette is confirmed
   unchanged.

4. **The ordered shortlist ids enter `run_id` (the body's own sentence "the
   ordered shortlist ids") while "a permuted shortlist produces a byte-identical
   recommendation block" cannot**: a permuted shortlist order is a different
   digest input. #25 orders the shortlist deterministically from content, so a
   permuted candidate population or insertion order produces the same shortlist,
   the same `run_id` and a byte-identical block; the same request repeated gives
   the same bytes. `test_a_permuted_candidate_population_leaves_the_block_identical`
   asserts it.

5. **`backend/api/__init__.py`'s package claim.** Its docstring said "Nothing
   in this package imports `backend.intelligence.*`, reads a credential, opens
   an outbound socket or returns an audio byte, a filesystem path or a file",
   which this issue itself makes false for `backend/api/recommendations.py`.
   **Resolved under the orchestrator's authorization**: the claim paragraph now
   names the one module that may import `backend.intelligence.jev` and
   `backend.intelligence.questions` and states that no module here opens an
   outbound socket of its own; nothing else in the docstring changed, and #27's
   two policy rules are restated in `tests/test_api_policy.py` exactly as the
   issue documents them.

## Reproduction commands

```powershell
uv run pytest tests/test_api_recommendations.py tests/test_api_recommendation_flow.py `
  tests/test_api_contracts.py tests/test_api_policy.py tests/test_api_library.py `
  tests/test_api_imports.py
uv run pytest
```

A skipped subprocess or signal case is a skip with its reason, never passing
evidence; the credentialed live Jev check stays
[#68](https://github.com/gmphto/tera/issues/68)'s and is reported UNVERIFIED
here, exactly as #14 reports it.

## Local verification

Recorded on this machine at commit (see the issue comment; the tree was clean
apart from the commit). Commands were run from the repository root with the
project interpreter and a `%TEMP%` `sitecustomize.py` on `PYTHONPATH`
coercing directory modes, `-p no:cacheprovider` and `--basetemp` outside the
repository.

```powershell
$env:PYTHONPATH="$env:TEMP\dsh-Mh1fXE"
.venv\Scripts\python.exe -m pytest tests/test_api_recommendations.py `
  tests/test_api_recommendation_flow.py tests/test_api_contracts.py `
  tests/test_api_policy.py tests/test_api_library.py tests/test_api_imports.py `
  -q -p no:cacheprovider --basetemp="$env:TEMP\dsh-Mh1fXE\ptrecord"
```

Observed summary line: `221 passed in 315.43s (0:05:15)`.

```powershell
.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider `
  --basetemp="$env:TEMP\dsh-Mh1fXE\ptall2"
```

Observed full-suite summary line: `4 failed, 2214 passed, 1 skipped in 724.12s
(0:12:04)`. Against this repository's baseline at the parent commit
(`4 failed, 2120 passed, 1 skipped`) the change adds 94 passing tests and
introduces no failure; the four failures are the same sandbox-blocked
`CreatePipe` `PermissionError` cases as the baseline, named there as
`tests/test_batch.py::test_cli_empty_and_invalid_inputs`,
`tests/test_batch.py::test_cli_fresh_and_resume`,
`tests/test_evaluation_manifest.py::test_cli_build_validate_and_synthetic_shortfall`
and
`tests/test_evaluation_prepare.py::test_preparation_idempotence_source_preservation_and_collisions`.
One case is skipped, exactly as at the baseline; no case of this task is skipped.

### Probe 1: the library reads do not grow with the shortlist

A direct `run_recommendation` call on a 12-candidate and a 100-candidate library,
with a #26 handle stubbed to perform no SQL and `sqlite3.Connection.set_trace_callback`
counting the reads:

```
== size 12 scored 12 reads 26
== size 100 scored 100 reads 26
```

The 26 are this route's palette read before the run and after ranking (7 + 7),
its kick row (2), #25's kick and population reads (8) and the single keyed
shortlist read (2). The shortlist read is one statement whatever its size.

### Probe 2: #26's judgment key carries no ranking version

```
== probe 1: #26's judgment key carries no ranking version
cache_key_digest before: ad25832580c6111504c001932d2350a56d82a479c485c2ccc2a6b5264e53ed54
cache_key_digest after : ad25832580c6111504c001932d2350a56d82a479c485c2ccc2a6b5264e53ed54
identical: True
```

### Probe 3: #12 raises on an unknown band

```
TypeError: unsupported operand type(s) for +: 'int' and 'NoneType'
  backend/palette/ranking.py:162 in _frequency_evidence
    candidate_total = sum(candidate_bands.values())
```

### Probe 4: #27's single database thread serializes the health route

With a blocking task on `app.database.run`, `app.health()` waited for it:

```
== probe 2: #27's one database thread serializes /health behind app.database.run
health state: ok blocked for seconds: 29.806
```

This is why the recommendation route opens its own connection (above).

### Probe 5: the health route during a real blocked run, before and after

Before the connection change, `GET /health` issued while a recommendation run was
blocked inside its transport returned after `30204` ms (the service's own access
line: `{"duration_ms":30204,...,"route":"GET /health","status":200}`).

After the change, three `GET /health` calls issued while the run was still
blocked:

```
health replies while the run was blocked: [(200, 0.0036), (200, 0.003), (200, 0.0022)]
run status: 200
```

and the service's access lines for them read `"duration_ms":1` each. The budget
criterion's one-second bound holds with two orders of magnitude to spare.

### Machine limitations

- The confined sandbox refuses piped stdio to child processes, so no part of this
  work spawns a service child or reads a child pipe; every API case runs the
  server in process on port 0, as #27's own tests do.
- `uv run` cannot capture a child on this machine, so the recorded commands use
  `.venv\Scripts\python.exe -m pytest` directly.
- The credentialed live Jev integration is UNVERIFIED here: no real credential,
  endpoint or model version was used, and every judgment in the fixtures comes
  from a synthetic scripted transport with source `double` or `interface`.
