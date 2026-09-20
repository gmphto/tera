# Normalized feature retrieval

This document owns the normalized kick-to-bass similarity space of issue #25
(plan section 6 ranking pipeline, section 4 MVP scope, Phase 1 as authorized by
[`feasibility-decision-165.md`](feasibility-decision-165.md)). It is the
retrieval stage between issue #11's admission and the Jev stage: it turns stored
measurements into one versioned order per kick and cuts that order to a
configured 50-100 candidate shortlist.

[`library-storage.md`](library-storage.md) owns the database, the migrations and
the tables; [`filter-policy.md`](filter-policy.md) owns admission;
[`batch-analysis.md`](batch-analysis.md) owns identity, digest and analysis
versions; [`evaluation-protocol.md`](evaluation-protocol.md) owns the
evaluation protocol, its seeds and its thresholds. This document owns the
retrieval policy, the normalization, the recall measurement and their version
identifiers.

Retrieval similarity is a **plausibility order with its own version
identifiers**. It is never a compatibility score, never a confidence and never a
recommendation: it is not a recommendation and must not be presented as one by
the later work of #28 or #33. The only contract field it feeds is
`backend.contracts.RankedCandidate.similarity` (with
`similarity_unavailable_reason`), while `compatibility` and `confidence`
come only from #12/#15.

Nothing here leaves the device. No audio byte is opened, statted or decoded, no
path is probed and no network or model call is made.

## The representation

`backend/palette/retrieval.py` is pure and I/O-free: it imports the standard
library, `backend.contracts` and `backend.analysis.batch`'s `digest` only.
It never imports `sqlite3`, `backend.library`, `backend.palette.compatibility`,
`backend.palette.ranking` or `backend.intelligence`, never opens a path and
never decodes audio.

### The dimension table

`RETRIEVAL_DIMENSIONS` is exactly these fourteen stored
`backend.contracts.MEASURES` names, in this order, with the transform
`RETRIEVAL_TRANSFORMS` declares. The transform is applied before normalization,
in this dimension order, on the stored value with no unit conversion:

| # | Dimension | Unit | Transform | Accepted domain | Excluded as |
| --- | --- | --- | --- | --- | --- |
| 1 | `fundamental` | Hz | `log2_hz` | value > 0 and confidence >= `RETRIEVAL_FUNDAMENTAL_CONFIDENCE_FLOOR` (0.80) | unknown |
| 2 | `spectral_centroid` | Hz | `log2_hz` | value > 0 | unknown |
| 3 | `spectral_rolloff` | Hz | `log2_hz` | value > 0 | unknown |
| 4 | `loudness` | LUFS | `linear` | any finite stored value | unknown |
| 5 | `crest_factor` | ratio | `linear` | any finite stored value | unknown |
| 6 | `transient_strength` | normalized | `linear` | any finite stored value | unknown |
| 7 | `attack` | ms | `log1p_ms` | value >= 0 | unknown |
| 8 | `decay` | ms | `log1p_ms` | value >= 0 | unknown |
| 9 | `band_sub` | ratio | `linear` | any finite stored value | unknown |
| 10 | `band_bass` | ratio | `linear` | any finite stored value | unknown |
| 11 | `band_low_mid` | ratio | `linear` | any finite stored value | unknown |
| 12 | `band_mid` | ratio | `linear` | any finite stored value | unknown |
| 13 | `band_high_mid` | ratio | `linear` | any finite stored value | unknown |
| 14 | `band_high` | ratio | `linear` | any finite stored value | unknown |

`log2_hz` is `math.log2(value)`; `log1p_ms` is `math.log1p(value)`; `linear`
is the stored value unchanged. `MEASURES` already fixes the units, so no unit
conversion happens here and no measurement is renamed.

The retrieval-side F0 floor `RETRIEVAL_FUNDAMENTAL_CONFIDENCE_FLOOR` is 0.80 and
is this module's own constant: it is deliberately not read from
`backend.palette.compatibility.CONFIDENCE_THRESHOLD`, and a test changes that
constant and asserts retrieval is unchanged.

### Deliberate exclusions

These stored measurements are deliberately left out of the representation:

| Measurement | Why it is not a retrieval dimension | Owner |
| --- | --- | --- |
| `rms`, `peak` | Absolute level, already represented by `loudness` and `crest_factor`; level matching belongs to audition, not to the plausibility order. | this task |
| `transient_position` | No cross-sample onset alignment in v1, so its position is not comparable between two samples. | this task |
| `stereo_width`, `tempo` | Stored as `not_implemented` by the current extractors, so a dimension would be constructed out of nothing. | #44, #46 |
| `Sample.features.key` | A compatibility input owned by #12/#15, never a similarity dimension. | #12, #15 |

## Masking: unknown measurements are never imputed

A dimension is known for one sample only when its `Measurement.value is not
None`, the transform's domain accepts the value and - for `fundamental` only -
the stored `confidence` is a number at or above 0.80. A `None` value keeps its
stored `unavailable_reason` and is masked. Zero-filling, mean imputation,
dropping the sample and treating unknown as the population mean are all
forbidden: a candidate with an unknown dimension is scored on the dimensions it
does know, and the population statistics never contain a value that was not
stored.

A value outside a log transform's domain (a stored `spectral_centroid` or
`spectral_rolloff` of 0, or any negative value) is unknown for that dimension
and is never `-inf`; `mask_counts` counts it as domain-rejected per dimension
and `StoredRetrieval.domain_rejected` reports that count. A stored
`fundamental` of 0 cannot exist: `backend.contracts.Measurement` requires a
positive fundamental, while `spectral_centroid` and `spectral_rolloff` permit
0 and are the reachable domain refusal.

## The population

`fit_normalization(samples, *, analysis_version)` fits one population. For one
run the population is: **every row in the opened database whose role is in
`RETRIEVAL_CANDIDATE_ROLES` - the tuple `"bass", "sub-bass"` - and which has a
stored analysis at exactly the run's analysis version, after the duplicate
identity collapse.** It is the whole local library at that version and nothing
else:

- it is never restricted to the current query's eligible set,
- never to available files,
- never to a peak/onset/role-filtered subset,
- never to the current query's return set,
- and the query kick is never part of it.

The population is fitted in ascending `sample_id` order, so the floating-point
sums do not depend on the caller's row order. `retrieve_shortlist` computes it
from storage, so the same `normalization_id` is produced for every query and can
be reused by #26's cache and #28.

`fit_normalization` raises `invalid_candidates` when any supplied sample's role
is not in `RETRIEVAL_CANDIDATE_ROLES` (a kick sample is refused),
`candidate_analysis_version_mismatch` when a sample carries another analysis
version and `duplicate_candidate_id` when one sample id appears twice.

### Rejected alternatives

| Alternative | Consequence |
| --- | --- |
| A per-query eligible-set population | One candidate's similarity would depend on the other candidates of that query; it would change when an unrelated sample is imported or its file goes missing; no shortlist could be reproducible or cacheable. |
| A whole-library population that mixed kick and bass rows | It would z-score the candidate space against a distribution the bass candidates do not come from. |

## The normalization record

`NormalizationRecord` is frozen with exactly `representation_version`,
`analysis_version`, `candidate_roles`, `sample_count`, `population_digest` and
`dimensions`. `DimensionNormalization` is frozen with exactly `name`,
`transform`, `mean`, `std`, `known_count` and `inactive_reason`.

Per dimension over the known transformed population values, in this order in
Python floats:

- `mean = sum(values) / count`
- `std = sqrt(sum((value - mean) ** 2) / count)` (population standard
  deviation, `ddof=0`)
- `z = (value - mean) / std` for active dimensions.

`population_digest` is `backend.analysis.batch.digest` over the representation
version, the analysis version, the candidate role tuple and the sorted
`(sample_id, analysis_version)` pairs. `normalization_id(record)` is
`digest(record.to_dict())`. Both are bare 64-character lowercase hex, matching
#9's `analysis_digest`, and neither contains a local path, pack name, filename
or timestamp - two machines that hold the same content analysed by the same
runtime versions derive the same `normalization_id` at different library roots.
The representative path used for the duplicate collapse is reported only in the
local in-memory records; this task adds no table, column, index or migration,
no `CREATE TABLE`, `CREATE INDEX` or SQL string exists in either new module,
and `backend/library/schema.py` is unchanged (`SCHEMA_VERSION` stays 3 and the
eleven-table set is exactly what `library-storage.md` documents).

## Degenerate populations and extreme values

`DIMENSION_INACTIVE_REASONS` is the closed tuple
`("insufficient_population", "zero_variance")`. A dimension is inactive when its
known population count is below `MIN_NORMALIZATION_POPULATION` = 2
(`inactive_reason = "insufficient_population"`) or its `std` is below
`NORMALIZATION_EPSILON` = 1e-9 (`inactive_reason = "zero_variance"`). An
inactive dimension carries `mean = null`, `std = null`, is excluded from every
distance, is never divided by zero, is never imputed and is not counted in a
candidate's coverage.

Every returned `mean`, `std`, `similarity` and `coverage` is a finite double, with one theoretical exception: a population whose scaled standard deviation lands within an ulp of the largest finite double can still overflow the final multiplication. No stored extractor value can produce that (see Limitations), and the guard is documented rather than claimed to be absolute.
A population whose sum or squared deviations would overflow a double - a stored
`loudness` population of `0.0` and `1e155`, or of `1e308` and `1.5e308` - is
re-scaled by its largest magnitude inside the same formula, so
`backend.analysis.batch.canonical` never sees a NaN or an infinity. The
squared deviation is magnitude-checked against `sqrt(sys.float_info.max)`
before it is squared, because `deviation ** 2` raises `OverflowError` above
that bound instead of returning an infinity the finiteness test could see; the
re-scaling branch is therefore reached and the exception cannot escape
`fit_normalization`. A population member's `|z|` stays bounded by
`sqrt(n - 1)` even in the re-scaled space, so a re-scaled population is never
itself the extreme case; `extreme_dimension_value` is for the kick or a
caller-supplied candidate outside the population, exactly as the next
paragraph states.

A candidate whose `|z|` exceeds `MAX_ABSOLUTE_Z` = 1e9 on any common dimension
- or whose z-score with the kick is not finite - gets `similarity = null` with
reason `extreme_dimension_value` instead of a squared term that can overflow.
Such a candidate still ranks (last, by the ordering rule) because #11, not
retrieval, owns removal. The guard covers a population member (bounded by
`sqrt(n - 1)`), the kick (which is not a population member and can be extreme)
and a caller-supplied candidate outside the population.

## Distance, similarity and coverage

Let `active` be the dimensions the normalization marks active and `D` the
subset of `active` known for both the kick and the candidate.
`coverage = len(D) / len(active)`, and `coverage = 0.0` when nothing is active.
`len(active) == 0` is the only case where a coverage denominator is zero.

Then, in this order:

1. `len(active) == 0` -> every candidate gets `similarity = null` with reason
   `no_active_dimensions`.
2. else the kick's own coverage below `MIN_RETRIEVAL_COVERAGE` = 0.50 -> every
   candidate gets `similarity = null` with reason
   `kick_dimensions_insufficient`.
3. else a candidate's coverage below 0.50 -> `similarity = null` with reason
   `insufficient_common_dimensions`.
4. else `d = sqrt(sum((z_kick[i] - z_candidate[i]) ** 2 for i in D) / len(D))`
   and `similarity = 1 / (1 + d)`, which is in (0, 1].

The sums iterate the declared dimension order, never `set` order.
`SIMILARITY_UNAVAILABLE_REASONS` is the closed tuple
`("no_active_dimensions", "kick_dimensions_insufficient",
"insufficient_common_dimensions", "extreme_dimension_value")`: an unknown
similarity always carries exactly one of them and a known similarity carries
none, the same rule `backend.contracts.RankedCandidate` validates.

## Ordering, ties and the cutoff

```text
candidates with a known similarity first, by similarity descending
equal similarities by sample_id ascending (exact == on the computed double)
then the unscored candidates by sample_id ascending
```

The order is total and deterministic: input order, `dict` or `set` iteration
order and database page order cannot move it. `CutoffEvidence` is frozen with
exactly `similarity`, `included`, `excluded` and `tied_ids`, where

- `tied_ids` is every ranked candidate whose similarity is exactly equal to the
  last included candidate's (all of them, not only those at the boundary);
  an unavailable similarity is not a value that can tie, so the set is that
  candidate itself,
- `included` is the last shortlist id,
- `excluded` is the first id left out, or null when the shortlist was not cut.

## Shortlist size and limit reasons

`RetrievalPolicy` has exactly one field, `shortlist_size: int =
DEFAULT_SHORTLIST_SIZE` (100). A value that is not an `int` (a `bool`
included) or that lies outside [`SHORTLIST_MIN`, `SHORTLIST_MAX`] = [50, 100]
raises `invalid_shortlist_size` from the constructor and again from
`select_shortlist`. No caller may request a shortlist below 50 or above 100,
and the diagnostic curve below is computed by truncating `ranked`, never by
running a smaller policy.

`limit_reason` is exactly one of `"shortlist_size"` (the cut removed at least one
ranked candidate), `"eligible_exhausted"` (every eligible candidate was
returned, which includes `len(ranked) == shortlist_size`) or
`"no_candidates"` (`ranked` is empty), and `shortlist_size_returned` equals
`len(shortlist)`.

## The result records

`RetrievalResult` is frozen with exactly `policy_version`,
`representation_version`, `normalization_id`, `analysis_version`, `policy`,
`shortlist_size_requested`, `shortlist_size_returned`, `limit_reason`,
`ranked`, `shortlist` and `cutoff`. `ranked` is every eligible candidate,
ordered; `shortlist` is the first `shortlist_size_returned` sample ids of
`ranked`.

`RetrievedCandidate` is frozen with exactly `sample_id`, `position` (1-based
within `ranked`), `similarity`, `similarity_unavailable_reason`, `coverage`
and `common_dimensions` (the dimensions of `D`, in declared order). Neither
record has a compatibility, confidence, dimension-score, weight, Jev or
reason-for-recommendation field, and neither new module imports
`backend.palette.ranking` or `backend.intelligence`.

`select_shortlist(kick, candidates, *, policy, normalization)` orders exactly
the sequence it receives and cannot re-admit anything, because the pure module
imports nothing from `backend.palette.compatibility`; #11 owns admission. The
kick and every candidate must carry exactly `normalization.analysis_version`:
a kick at another version raises `stale_kick_analysis`, a candidate at another
version `candidate_analysis_version_mismatch` and a mismatched
`representation_version` `invalid_normalization`.

## Storage reads

`backend/library/retrieval.py` contains no SQL string: every read goes through
#21's `backend.library.repository`. `retrieve_shortlist` performs two library
reads, each one statement, with no query inside a per-candidate or per-dimension
loop:

| Read | #21's landed operation | What it returns |
| --- | --- | --- |
| the query kick's current-version sample | `LibraryRepository.get_sample(sample_id, analysis_version)` | the validated kick `Sample` and its stored `file_status` |
| the candidate-role population at that version | `LibraryRepository.list_retrieval_rows(analysis_version, roles=RETRIEVAL_CANDIDATE_ROLES)` | one `RetrievalRow` per stored row: `sample_id`, `role`, `path`, `path_key`, `file_status`, `content_sha256`, `analysis_state` and the validated `Sample` of that version |
| the same read for the reference ids | `LibraryRepository.list_retrieval_rows(analysis_version, sample_ids=...)` | the same rows named by id, whatever their role |

`list_retrieval_rows` is the read this issue added to #21's file: it left no
operation returning a row's analysis state (`current`, `stale`, `absent`) and
its stored availability together with the validated sample, and the recall
report needs exactly that for a set of sample ids. It is one statement with
correlated `EXISTS` lookups on the primary-key prefix, so the statement count
does not grow with the number of rows; a test installs
`sqlite3.Connection.set_trace_callback` and asserts the count is identical for
60 and for 120 candidate rows. `tests/test_retrieval_integration.py` records
the `EXPLAIN QUERY PLAN` output for it against a temporary database in the
issue comment.

The analysis version is derived as
`backend.analysis.batch.digest(backend.analysis.batch.analysis_descriptor())`
and never accepted from a caller. Only rows whose stored analysis is exactly
that version are scored: rows whose only analysis is an older version are
counted as `skipped_stale_analysis` and rows with no stored analysis at all as
`skipped_absent_analysis` (both are subsets of the population #22's
`pending_analysis` returns). Retrieval never falls back to a superseded
analysis and never mixes two versions in one normalization.

### Availability

The mapping passed to #11 is read from #21's stored per-sample availability and
no path is probed, so an unscanned row is unverified rather than assumed
available:

| Stored `samples.file_status` | `backend.palette.compatibility.Availability` | #11's reason when it excludes |
| --- | --- | --- |
| `present` | `AVAILABLE` | - |
| `missing` | `MISSING` | `file_missing` |
| `unknown`, or a supplied id with no row | `UNKNOWN` | `availability_unknown` |
| `unreadable` (not stored today) | `UNREADABLE` | `file_unreadable` |

An explicit `availability` argument is accepted only as a complete mapping over
the supplied ids - the kick plus every current candidate - and is validated by
#11's own rules, so a test can inject a state without touching the database; an
incomplete or unknown-id mapping raises `invalid_availability`.

### Admission stays with #11

`retrieve_shortlist` calls `backend.palette.compatibility.filter_candidates`
with the caller's `filter_policy`, the derived availability and the optional
context **before** it fits the normalization, and the returned record carries
that `FilterResult` unchanged (`policy_version`, `eligible_ids`,
`excluded`). Every id in `ranked` and `shortlist` is in
`filter_result.eligible_ids`, no excluded candidate appears anywhere in the
result, and when the cut is not binding `set(shortlist) == set(eligible_ids)`.
A silent or unavailable candidate is reported with #11's `silent_audio` or
`file_missing` reason and is never scored by retrieval. Retrieval never changes
a sample's role, never deletes or writes a library row, and never opens, stats
or decodes an audio file.

### Duplicate identity collapse

`retrieve_shortlist` collapses rows that share an id within
`RETRIEVAL_CANDIDATE_ROLES` to one candidate - the representative with the
lexicographically smallest normalised path, the same rule #22's
`pending_analysis` uses - records the collapse count as `duplicate_candidates`
and the chosen representative paths in the local-only
`StoredRetrieval.representative_paths` field, and calls #11 with unique ids, so
a scanned duplicate cannot produce a `duplicate_candidate_id` refusal or two
identical entries in the shortlist. Rows that share content but differ in role
are resolved by the role filter, not by the collapse. The pure
`select_shortlist` still raises `duplicate_candidate_id` for a repeated id in
its input.

**Recorded conflict.** Issue #25's duplicate criterion is not reachable through
the landed storage contract. The criterion says "**#21/#22 store one row per
(root, path, role) while the sample id is a content identity that duplicate
paths share**", but `library-storage.md` stores `content_sha256` as
"NOT NULL, UNIQUE, 64 lowercase hex", and #22's scanner records a duplicate
path as `DUPLICATE` ("The content identity is already held by a library row at
another path; the scan neither merges nor reindexes it") instead of writing a
second row. A probe importing the same bytes under a second path and sample id
raises `duplicate_content` and leaves one row. The collapse is implemented and
tested against a stubbed population read (two rows sharing one id), so a future
per-path row model needs no retrieval change, but through the repository
`duplicate_candidates` is always 0. The owner is #167 (per-path sample rows,
wide channel layouts); #166 (binding a stored analysis to the content identity
it was measured from) is adjacent.
## The recall report

`backend/library/retrieval.py` defines `RETRIEVAL_SCHEMA = "1.0"`,
`RECALL_AT_SIZES = (5, 10, 20, 50, 100)`, `RECALL_POSITIVE_LABELS = ("good",
"excellent")`, `REPORT_STATES = ("measured", "insufficient_evidence")`,
`REPORT_BLOCKED_REASONS = ("pair_list_absent", "no_sessions",
"no_positive_references")`, `NOT_EVALUABLE_REASONS = ("query_kick_missing",
"no_available_candidates", "no_positive_references")`,
`MISS_CLASSIFICATIONS = ("cut", "filter_excluded", "missing_from_library")` and
`class RetrievalReportError(ValueError)` with a stable `.code`.

### The reference

The pair list is read with `backend.evaluation.rating.load_pair_list`'s own
parts and vocabulary - `manifest.read_json` plus
`rating.check_pair_list_document`, `rating.SCHEMA_VERSION` and
`rating.PAIR_LIST_FIELDS` - and the exports are the `export.json` documents of
`backend.evaluation.rating` (`rating.EXPORT_FIELDS`, `rating.RATING_FIELDS`,
`rating.SCHEMA_VERSION`, `rating.LABELS`). This task defines no second
pair-list or export reader, schema or label vocabulary. The landed
`load_pair_list` prints its own problem table, which the CLI's one-code output
forbids, so the report calls the two functions that function is made of and
records the mapping here.

`reference_for(pair_list, exports)` builds the reference from the pair list and
the export records **only**, is called before any retrieval runs, and for each
query kick ascending by `kick_sample_id` over the pair list's distinct kicks
produces a `RecallQuery`:

- `declared` = the distinct `bass_sample_id` values for that kick, ascending;
- `positive` = the declared references whose pair aggregate is positive, where
  the aggregate reuses `backend.evaluation.comparison`'s published
  `ratings_by_pair`, `pair_aggregates` (at least `MIN_RATINGS_PER_PAIR` = 2
  distinct evaluators, lower-middle median) and `LABEL_VALUES`, and is positive
  when it equals `LABEL_VALUES["good"]` (2.0) or `LABEL_VALUES["excellent"]`
  (3.0) - exactly the labels `RECALL_POSITIVE_LABELS` names. A skipped
  presentation or a record whose `rating` is not in `rating.LABELS` never
  contributes, and a record for a pair the list does not hold is ignored.

`reference_for` has no database access, so `positive` is the declared positive
set; the `available` intersection below happens in `recall_metrics`.

### The metrics

`recall_metrics(reference, retrieval)` consumes one `RecallReference` plus the
retrieval runs made for it (one `StoredRetrieval`, a sequence of them or a
mapping keyed by query kick) and returns `RecallMetrics` - frozen with
`queries` (a tuple of `RecallQueryRow`, each with `misses` as `RecallMiss`
records), `curve` (a tuple of `RecallCurvePoint`) and `totals` (a
`RecallTotals`). Per query:

- `available` = the declared ids present in that query's current-version
  candidate population (the same read `retrieve_shortlist` made);
- `positive` = the declared positives inside `available`;
- `retrieved_declared` = `|shortlist ∩ declared|` and
  `retrieved_positive` = `|shortlist ∩ positive|`;
- `recall` = `retrieved_positive / positive_count`, or `null`;
- a query is measurable only with `positive_count >= 1`; a query with no
  positive reference is counted in `queries_not_evaluable` with reason
  `no_positive_references`, is excluded from both numerator and denominator and
  never contributes a recall of 1;
- each row carries `query_kick_id`, `declared_count`, `available_count`,
  `positive_count`, `retrieved_declared`, `retrieved_positive`, `recall`,
  `not_evaluable_reason` and `misses`;
- every miss (a declared id not in the shortlist) is classified as `cut`,
  `filter_excluded` (with #11's reason codes) or `missing_from_library`, so a
  filtered or unanalysed reference is never reported as a retrieval failure.

`recall_at_size = (sum over measurable queries of |shortlist ∩ positive|) /
(sum of |positive|)` and `coverage_at_size` is the same shape over
`available`. The curve is diagnostic only - a `RecallCurvePoint` per size in
`RECALL_AT_SIZES` at or below the policy size, computed from `ranked[:k]` - and
the product shortlist is only ever the configured 50-100 size.

### Non-circularity

The reference set may never be derived from the retrieval's own output or order,
from #11's `FilterResult`, from the DSP-only baseline (#12,
`backend.palette.ranking`), from hybrid results (#15), from Jev judgments
(#13/#14) or from any compatibility score. The pair list is the plan's
feasibility evaluation set drawn by #65 from the #10 pool, and it is
arm-independent. With `select_shortlist` monkeypatched to raise,
`reference_for` still returns the full reference. The metric helpers take no
ranking, Jev or filter argument: the admission evidence they read is #11's
result, which `StoredRetrieval` carries unchanged.

This report is a retrieval-stage measurement. It sets no quality threshold, adds
no gate to `_docs/evaluation-protocol.md`, and does not modify #16's protocol
or #19's metrics, seeds or artifacts; quality gates stay with #16, #19 and #20.

### Report schema

`recall_report(connection, *, pair_list_path, export_paths, size)` returns - and
the CLI writes - exactly these fields and nothing else:

```text
retrieval_schema           "1.0" (RETRIEVAL_SCHEMA)
retrieval_version          RETRIEVAL_POLICY_VERSION
representation_version     REPRESENTATION_VERSION
normalization_id           normalization_id of the whole-library fit
analysis_version           digest(analysis_descriptor())
shortlist_size             the requested size
policy                     {"shortlist_size": size}
population_count           current candidate-role rows after the collapse
duplicate_candidates       rows the identity collapse removed
skipped_stale_analysis     candidate rows holding only another version
skipped_absent_analysis    candidate rows holding no analysis
reference                  pair_list_digest ("sha256:" plus the SHA-256 of the
                           pair-list bytes), dataset_version,
                           split_manifest_digest, sampler_seed,
                           assignment_seed, sorted export_digests,
                           positive_labels, min_ratings_per_pair
queries                    the RecallQueryRow objects above
curve                      the RecallCurvePoint objects above
totals                     queries, measurable_queries, queries_not_evaluable,
                           declared_total, available_total, positive_total,
                           retrieved_declared_total, retrieved_positive_total,
                           recall_at_size, coverage_at_size
state                      "measured" or "insufficient_evidence"
blocked_reason             null, "pair_list_absent", "no_sessions" or
                           "no_positive_references"
```

`state` is `measured` only when at least one query is measurable and
`totals.positive_total >= 1`; otherwise it is `insufficient_evidence` with
`blocked_reason` exactly `pair_list_absent` (no pair list was named or the path
does not exist), `no_sessions` (no exports) or `no_positive_references`
(exports exist, no pair is positive). In the insufficient-evidence state the
report is still written, `totals.recall_at_size` is `null` and the counts and
denominators are reported: a missing reference is a documented blocker, never a
fabricated recall and never an exit 2.

The report is written only under the caller's `--output` path, through a
flushed, fsynced temporary file in the same directory and an atomic replace, and
never into a source root. `backend.evaluation.manifest.write_private` is not
called unchanged because it refuses to replace an existing file that does not
carry a `dataset_version` field, which this report does not carry and may not
carry, so a second run could not replace its own report under the same
`--output`; the mechanism is the landed one (same-directory `mkstemp`,
`flush`, `os.fsync`, `os.replace`, a regular unlinked target and an
aliased-output refusal). The report is private local data: it contains no
timestamp, path, hostname, pid or duration. Only versions, aggregate counts,
denominators, state, blocked reason and command lines may be quoted in an issue
comment, and no sample id, path, pair id, evaluator id, per-query row or hash of
private audio may be.

## The command line

```text
uv run python -m backend.library.retrieval recall --database DB.sqlite3 --pair-list PAIRS.json [--export EXPORT.json]... [--size 100] --output REPORT.json
```

`--output` must end in `.json`. Exit codes:

| Exit | Meaning |
| --- | --- |
| exit 0 | a report was written, including the insufficient-evidence report |
| exit 1 | a report was written and at least one pair-list query could not be evaluated from the library (`query_kick_missing` or `no_available_candidates`) |
| exit 2 | invalid command, unreadable/unsupported database, malformed pair list or export, or unwritable/aliased output |
| exit 130 | interrupted before the atomic replace |

Failures print one code plus one path-free message, never a traceback or a table
dump. An argument-level refusal - no command, a missing required option, an
unknown option - is `invalid_arguments`: the parser's `error` hook raises
instead of printing argparse's usage table, so `main(argv)` returns 2 for it
too, and the only `SystemExit` argparse raises is the `--help` one it prints
for the user. `main(argv)` returns the exit code;
`python -m backend.library.retrieval` raises `SystemExit` with it.

## Input and report errors

`RETRIEVAL_ERROR_CODES` is the closed tuple of `RetrievalInputError` codes:

| Code | Raised by |
| --- | --- |
| `invalid_policy` | `select_shortlist` without a `RetrievalPolicy` |
| `invalid_shortlist_size` | `RetrievalPolicy`, `select_shortlist` and the CLI's `--size` for a non-int or out-of-bound size |
| `invalid_normalization` | a non-`NormalizationRecord`, another representation version, other candidate roles, a duplicate dimension name, a dimension list that is not the declared one in order, a transform or statistic that is not the declared one |
| `unknown_dimension` | a normalization naming a dimension outside `RETRIEVAL_DIMENSIONS` |
| `invalid_candidates` | `fit_normalization` for a sample whose role is not in `RETRIEVAL_CANDIDATE_ROLES`, or a non-sequence input |
| `invalid_kick` | `select_shortlist` without a contract `Sample` kick |
| `invalid_analysis_version` | `fit_normalization` or `mask_counts` with a blank analysis version |
| `stale_kick_analysis` | `select_shortlist` with a kick at another analysis version |
| `candidate_analysis_version_mismatch` | `fit_normalization` or `select_shortlist` with a sample at another analysis version |
| `duplicate_candidate_id` | `fit_normalization` or `select_shortlist` with a repeated sample id |

`REPORT_ERROR_CODES` is the closed tuple of `RetrievalReportError` codes:
`invalid_arguments` (`--output` does not end in `.json`, or an argument-level
refusal: no command, a missing required option or an unknown option),
`invalid_retrieval` (the metric helpers were handed something that is not a
`StoredRetrieval` or a `RecallReference`), `malformed_pair_list`,
`malformed_export`, `unwritable_output` and `aliased_output`. A database that
cannot be opened keeps #21's own code (`not_a_database`, `database_corrupt`,
`invalid_database_path`, `schema_version_newer`, `migration_failed`).

## The version table

| Constant | Covers | A change to which field forces a bump |
| --- | --- | --- |
| `RETRIEVAL_POLICY_VERSION` = `feature-retrieval-v1` | the size bounds, the coverage floor, `NORMALIZATION_EPSILON`, the population minimum, the extreme-value guard, the F0 floor, masking, the distance, the ordering and the tie-break | `shortlist_size`, `MIN_RETRIEVAL_COVERAGE`, `NORMALIZATION_EPSILON`, `MIN_NORMALIZATION_POPULATION`, `MAX_ABSOLUTE_Z`, `RETRIEVAL_FUNDAMENTAL_CONFIDENCE_FLOOR` |
| `REPRESENTATION_VERSION` = `kick-bass-feature-space-v1` | the dimension list, a transform, the population rule and the collapse rule | `RETRIEVAL_DIMENSIONS`, `RETRIEVAL_TRANSFORMS`, the population definition, the representative-path rule |

A bump invalidates every stored recall report, requires a re-run under the new
version, and may be called improved retrieval only with a fresh measurement,
never with a rerun of the old one.

## Determinism

Identical stored values, policy and normalization produce byte-identical
`backend.analysis.batch.canonical(result.to_dict())`, independent of candidate
input order, database row-insertion order, `VACUUM`/page order, `set` or
`dict` iteration order, `os.scandir` order, locale, wall-clock time,
randomness and the process hash seed: the distance sums iterate the declared
dimension order, the population is fitted in `sample_id` order and every record
is built from lists, never from iterated sets. Across machines the ids a caller
must compare are `normalization_id`, `representation_version` and
`analysis_version`; because `analysis_version` is #9's descriptor digest and
includes the Python/NumPy/SciPy/SoundFile/libsndfile versions, a different
runtime yields a different `analysis_version` and its shortlist is explicitly
not comparable.

## Resolved inconsistencies in the issue body

Five statements in issue #25 cannot all hold literally. They are resolved as
follows, with the exact text:

1. **The returned record.** "the returned record carries that `FilterResult`
   unchanged (`policy_version`, `eligible_ids`, `excluded`)" assigns the
   `FilterResult` to "the returned record", while "`RetrievalResult` carries
   exactly `policy_version`, `representation_version`, `normalization_id`,
   `analysis_version`, `policy`, `shortlist_size_requested`,
   `shortlist_size_returned`, `limit_reason`, `ranked` (every eligible
   candidate, ordered), `shortlist` ... and `cutoff`" forbids that field.
   Resolution: `retrieve_shortlist` returns a `StoredRetrieval` carrying the
   pure `RetrievalResult` **plus** `filter_result`, the population counts, the
   domain-rejection counts and the local-only representative paths; the pure
   record keeps its exact field list.
2. **The domain-rejected count.** "the result records the count of
   domain-rejected values per dimension" has no home in a record that "is frozen
   with exactly `representation_version`, `analysis_version`,
   `candidate_roles`, `sample_count`, `population_digest` and
   `dimensions`" whose entries "has exactly `name`, `transform`, `mean`,
   `std`, `known_count` and `inactive_reason`". Resolution: the count is
   carried by `StoredRetrieval.domain_rejected` (and by `mask_counts`), the
   records keep their exact fields.
3. **What `recall_metrics` consumes.** "`recall_metrics(reference, retrieval)`
   then consumes the reference plus one `RetrievalResult`" cannot classify a
   miss as "`filter_excluded` (with #11's codes)" from a `RetrievalResult`
   alone. Resolution: `recall_metrics` consumes the `StoredRetrieval` (or
   several, keyed by kick), which carries the `RetrievalResult`, and still
   takes exactly two arguments with no ranking, Jev or filter parameter.
4. **The cut at equality.** "`eligible_exhausted` (`len(ranked) <
   shortlist_size`, every eligible candidate returned)" is silent about
   `len(ranked) == shortlist_size`. Resolution: equality is
   `eligible_exhausted`, because the cut removed nothing and every eligible
   candidate was returned.
5. **The tie when the cut lands on an unscored candidate.** "`tied_ids` is
   every ranked candidate whose similarity is exactly equal to the last
   included candidate's (all of them, not only those at the boundary)"
   has no referent when the last included candidate's similarity is `null`:
   with 55 ranked candidates, 10 scored and 45 unscored at size 50, a literal
   reading would name all 45 unscored ids. Resolution: an unavailable
   similarity is not a value that can tie, so `tied_ids` is the boundary
   candidate itself, exactly as the ordering section states.

Two further notes rather than contradictions: the body's example of a
domain-refused value includes "a stored `spectral_centroid`, `spectral_rolloff`
or `fundamental` of 0", but a stored `fundamental` of 0 is refused earlier by
`backend.contracts.Measurement` ("`fundamental`: must be positive"), so the
reachable case is a frequency measurement of 0. And the body's reference rule
"positive = the available ids whose pair aggregate is positive" cannot be
computed by "`reference_for(pair_list, exports)` ... called before any retrieval
runs" without a database; the reference carries the declared positives and the
report intersects them with the available rows.

## Tests

```bash
uv run pytest tests/test_feature_retrieval.py tests/test_retrieval_integration.py --basetemp .pytest_cache/retrieval-focused
uv run pytest --basetemp .pytest_cache/retrieval-final
```

`tests/fixtures/retrieval/retrieval-cases.json` holds ten named cases whose
expectations are hand-written (stored values, not z-scores): identical vectors,
a candidate with unknown `loudness`, a stored `spectral_centroid` of 0, a
constant `transient_strength` population, a tie at the cut, a single-sample
population, a candidate with two of fourteen active dimensions, an all-unknown
kick, an extreme `crest_factor` and a size-requested-below-50 refusal. The
size-50 tie case is built from the fixture's declared formula and asserts only
the hand-stated rule (`tied_ids` equals the named three ids, the first is
included).

`tests/test_feature_retrieval.py` covers the pure module (dimension list,
transforms, masking, F0 floor, population rule, record and id reproducibility,
degenerate and extreme values, distance and coverage, ordering, ties and cutoff,
size bounds, empty/one/small sets, error codes, record fields, imports,
documentation agreement). `tests/test_retrieval_integration.py` builds
temporary databases through #21's `open_database` and repository, registers
`batch.analysis_descriptor()` and derives every stored sample from the synthetic
`tests/fixtures/contracts/hybrid.json` samples with `dataclasses.replace`, and
covers filter-first admission, availability mapping and injection, stale/absent
counts, duplicate collapse, population composition, the statement-count plan
check, the no-decode monkeypatches, the recall report over a synthetic pair list
and synthetic export documents with hand-computed recall, the curve, every miss
classification, the insufficient-evidence paths, the non-circularity
monkeypatches, the CLI exit codes and the report bytes.

## Local verification

Both commands were run from the repository root with the project interpreter.
`uv run pytest` cannot capture a subprocess in this sandbox (it fails with
`PermissionError [WinError 5]` at `_winapi.CreatePipe`), and a directory this
sandbox creates with mode 0o700 cannot then be enumerated, so each run put a
`sitecustomize.py` under the session temporary directory on `PYTHONPATH` that
forces every `os.mkdir` mode to 0o777, passed `-p no:cacheprovider` and a
`--basetemp` outside the repository, and ran the project interpreter directly
instead of `uv run`. Every database was a temporary file and every id, path and
hash in the fixtures is synthetic.

```text
$env:PYTHONPATH=<session-temp>; .venv\Scripts\python.exe -m pytest tests/test_feature_retrieval.py tests/test_retrieval_integration.py -q -p no:cacheprovider --basetemp <session-temp>\bt-focused2 -rf
  -> 44 passed in 10.20s

$env:PYTHONPATH=<session-temp>; .venv\Scripts\python.exe -m pytest -q -p no:cacheprovider --basetemp <session-temp>\bt-full -rf
  -> 4 failed, 1941 passed, 1 skipped in 322.69s (0:05:22)
```

The baseline measured in this workspace under the same sandbox at commit
`b9b5df3`, before round two's fixes, was `4 failed, 1940 passed, 1 skipped`.
The four failures are the known sandbox-only ones, all `PermissionError
[WinError 5]` at `_winapi.CreatePipe`:
`tests/test_batch.py::test_cli_empty_and_invalid_inputs`,
`tests/test_batch.py::test_cli_fresh_and_resume`,
`tests/test_evaluation_manifest.py::test_cli_build_validate_and_synthetic_shortfall`
and
`tests/test_evaluation_prepare.py::test_preparation_idempotence_source_preservation_and_collisions`.
Round two adds one pure-module test
(`test_an_overflowing_population_stays_finite_and_classifies_outside_values_extreme`)
and three argparse-level refusal assertions inside the existing CLI test, so the
two files collect 44 instead of 43 (`1941 - 1897` overall for the whole issue),
with no new failure and no new skip.

Round two re-probed the three QA overflow populations through the fixed
`_statistics`: the `loudness` pairs `[1e308, 1.5e308]`, `[1e308, 1.0]` and
`[1e155, 0.0]` all now return finite statistics instead of raising
`OverflowError` - `mean`/`std` `1.2499999999999998e+308`/`2.5000000000000005e+307`,
`5e+307`/`5e+307` and `5e+154`/`5e+154` respectively, each dimension active with
no `inactive_reason`. The three argparse-level refusals (`python -m
backend.library.retrieval` with no command, `recall` alone, and `recall` with an
unknown option) each print exactly one `invalid_arguments:` line on stderr, exit
2, and print no usage table.

Four probe results, printed by a scratch script under the session temporary
directory that built a temporary synthetic library (one kick and sixty basses)
through `open_database` and the repository. No repository file changed.

1. `EXPLAIN QUERY PLAN` for the candidate-role population read
   (`list_retrieval_rows` with the role filter), against the temporary database
   after `VACUUM` and `ANALYZE`:

```text
SCAN s USING INDEX sqlite_autoindex_samples_1
SEARCH f USING INDEX sqlite_autoindex_sample_features_1 (sample_id=? AND analysis_version=?) LEFT-JOIN
BLOOM FILTER ON k (sample_id=? AND analysis_version=?)
SEARCH k USING INDEX sqlite_autoindex_sample_keys_1 (sample_id=? AND analysis_version=?) LEFT-JOIN
CORRELATED SCALAR SUBQUERY 1
SEARCH f2 USING COVERING INDEX sqlite_autoindex_sample_features_1 (sample_id=?)
CORRELATED SCALAR SUBQUERY 2
SEARCH k2 USING COVERING INDEX sqlite_autoindex_sample_keys_1 (sample_id=?)
USE TEMP B-TREE FOR LAST TERM OF ORDER BY
```

2. The same statement with `s.sample_id IN (?, ?)` for the reference ids
   searches `samples` by primary key instead of scanning it
   (`SEARCH s USING INDEX sqlite_autoindex_samples_1 (sample_id=?)`); every
   other line is identical. Neither read scans `sample_features` or
   `sample_keys` and neither uses a per-row correlated subquery outside the
   primary-key prefix, so no defect is reported against #21's index list.

3. One `retrieve_shortlist` call for the sixty-row library executes eight
   statements (`sqlite3.Connection.set_trace_callback`), the same count for 120
   rows: `population_count: 60 shortlist: 50 limit_reason: shortlist_size`.

4. The duplicate-identity probe: importing the same content under a second path
   and sample id is refused with `duplicate_content` and one row holds that
   content - the storage-level evidence for the recorded conflict above.

One manual determinism check, run through the CLI in two fresh processes with
different hash seeds. Both wrote the same bytes:

```text
PYTHONHASHSEED=1: python -m backend.library.retrieval recall --database library.sqlite3 --pair-list pairs.json --export export-01.json --export export-02.json --size 50 --output report-1.json
  -> exit 0
PYTHONHASHSEED=2: python -m backend.library.retrieval recall ... --output report-2.json
  -> exit 0
FE0144F6E1F6C2D9D3F103F230894D50E5451957013E52C2E336682508EB2DF4  report-1.json
FE0144F6E1F6C2D9D3F103F230894D50E5451957013E52C2E336682508EB2DF4  report-2.json
state: measured   population_count: 60   shortlist_size: 50
declared: 2   available: 2   positive: 2   retrieved_positive: 1   recall: 0.5
recall_at_size: 0.5   coverage_at_size: 0.5
misses: bass-055=cut
```

The report contains no timestamp, path, hostname, pid or duration, so the check
is meaningful, and the recall numbers are the hand-checked ones: of two declared
and available positive references, the one at rank 55 of 60 is cut by the
size-50 policy and the one at rank 1 is retrieved.

