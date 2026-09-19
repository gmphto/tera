# Hybrid DSP and Jev kick-to-bass ranking

`backend.palette.ranking.rank_hybrid(baseline, evidence, *, policy)` combines the
deterministic DSP-only baseline of [#12](https://github.com/gmphto/tera/issues/12) with validated
Jev judgments ([#13](https://github.com/gmphto/tera/issues/13), [#14](https://github.com/gmphto/tera/issues/14))
under product-owned, versioned rules. It consumes the `RankingResult` #12 already returns and a
mapping of candidate ID to that candidate's `HybridEvidence` entries, and returns one
`HybridResult`.

The module is pure and local: it imports only the standard library, `backend.contracts` and
`backend.palette.compatibility`, opens no file, reads no audio, touches no network, credential,
clock or randomness, and imports nothing from the intelligence package. It issues no Jev call and
builds no question — the loop that asks, transports and records outcomes belongs to
[#28](https://github.com/gmphto/tera/issues/28). Jev reaches this module only as
`backend.contracts.JevJudgment` values the caller supplies, and this module never reconciles a
judgment with a measured fact.

```python
from backend.palette.ranking import (HybridEvidence, HybridPolicy, rank_candidates, rank_hybrid)

baseline = rank_candidates(selected_kick, eligible_bass_samples, policy=RankingPolicy())
evidence = {
    "bass-001": (HybridEvidence("frequency", judgment=frequency_judgment),),
    "bass-002": (HybridEvidence("texture", unavailable_reason="song_context_absent"),),
}
result = rank_hybrid(baseline, evidence, policy=HybridPolicy())
result.ranking_version   # "hybrid-ranking-v1"
result.weight_table_id   # "hybrid-weights-1"
result.mode              # "hybrid" or "dsp-only"
result.jev_status        # "jev_present", "jev_partial" or "jev_absent"
result.ranked            # scored records with rank 1..n, best first
result.unscored          # explicit insufficient_evidence records, ID ascending
result.alternatives      # up to three ranked IDs after rank 1, or empty
```

Every returned object is an immutable frozen dataclass. Inputs are never mutated, and the output
depends only on the supplied values: there is no clock, randomness, locale, hash iteration or
process state in the computation, the ordering or the reason strings.

## Entry-point contract

`rank_hybrid` revalidates everything it is given, the way `rank_candidates` does: the baseline
must be a `RankingResult` with contiguous ranks, unique nonblank candidate IDs, finite scores in
[0, 1] and valid `DimensionScore` entries; each judgment must pass a full
`JevJudgment.from_dict(judgment.to_dict())` round trip. `evidence` and `policy` are
keyword/explicit values. Nothing is dropped, defaulted or silently ranked.

`RankingInputError` inherits `ValueError` and exposes a stable `.code`:

| Code | Condition |
| --- | --- |
| `invalid_policy` | Not an explicit `HybridPolicy`, or its `HybridWeightTable` is malformed |
| `invalid_baseline` | Not an `RankingResult`, or a baseline field, rank, score, ID or `DimensionScore` entry is malformed |
| `invalid_evidence` | Evidence is not a mapping of tuples of `HybridEvidence`; a key is blank; an entry carries both or neither of a judgment and an unavailable reason; a blank unavailable reason; an unknown dimension literal; a judgment that is not a valid `JevJudgment` or that names another dimension |
| `duplicate_evidence` | Two entries for one candidate and dimension |
| `unknown_candidate` | An evidence key outside the baseline's candidates |

Validation order is policy, then baseline, then evidence. Evidence entries are validated candidate
by candidate in the baseline's own order (ranked in rank order, then unscored by ID), so a second
problem in an unordered mapping can never change which code is raised. An empty evidence mapping, an
empty baseline (no ranked and no unscored records), a single candidate, a candidate with no evidence
at all and a candidate whose only evidence is one Jev dimension are all valid.

## Output records

\`HybridResult\` is a frozen dataclass with:

| Field | Meaning |
| --- | --- |
| \`ranking_version\` | \`HYBRID_RANKING_VERSION\` |
| \`weight_table_id\` | The identifier of the table this run used |
| \`mode\` | \`hybrid\` or \`dsp-only\` |
| \`jev_status\` | \`jev_present\`, \`jev_partial\` or \`jev_absent\` |
| \`kick_id\` | The selected kick's ID, echoed from the baseline |
| \`kick_analysis_version\` | The kick's analysis version, echoed from the baseline |
| \`ranked\` | Scored \`HybridCandidate\` records with ranks 1..n, best first |
| \`unscored\` | \`HybridUnscored\` records, candidate ID ascending |
| \`alternatives\` | Up to three ranked IDs after rank 1, or empty |

\`HybridCandidate\` is a frozen dataclass with \`candidate_id\`, \`analysis_version\`, \`rank\`
(1..n), the combined \`compatibility\`, the coverage \`confidence\`, the \`uncertain\` flag, the
baseline's own \`dsp_dimensions\` (the three #12 \`DimensionScore\` records, verbatim), the six
\`hybrid_dimensions\` entries in the contract order, \`jev_judgments\` (the labeled judgments that
produced a \`jev_score\`, in the contract order), the baseline's own \`reasons\` and the
\`warnings\`. \`HybridUnscored\` carries \`candidate_id\`, \`analysis_version\`, the baseline
\`code\` (\`insufficient_evidence\`), \`dsp_dimensions\`, \`hybrid_dimensions\`, \`reasons\` and
\`warnings\`; it has no compatibility, confidence or \`uncertain\` flag because it is never
recommended.

\`reasons\` is the #12 baseline's own reason tuple, preserved verbatim so that a \`dsp-only\` run
is provably the baseline; the hybrid explanation of each dimension is the \`reason\` field of its
\`HybridDimensionScore\`.

## Constants

| Constant | Value | Meaning |
| --- | --- | --- |
| `HYBRID_RANKING_VERSION` | `"hybrid-ranking-v1"` | Identifies this combination, label map, thresholds, tie-break and code meanings |
| `HYBRID_WEIGHT_TABLE_ID` | `"hybrid-weights-1"` | The default five-dimension table identifier |
| `HYBRID_WEIGHTS` | frequency 0.30, transient 0.20, tonal 0.20, texture 0.10, arrangement 0.20 | The plan's five-dimension example table |
| `UNWEIGHTED_DIMENSIONS` | `("rhythmic",)` | Contract dimensions with weight 0 |
| `SOURCE_SHARE` | 0.5 | Equal share of each source when both exist |
| `JEV_LABEL_SCORES` | very-poor 0.0, poor 0.25, neutral 0.5, good 0.75, excellent 1.0 | The label-to-score map |
| `LOW_CONFIDENCE_THRESHOLD` | 0.60 | Coverage below which a candidate is uncertain |
| `JEV_CONFIDENCE_THRESHOLD` | 0.80 | Imported from `backend.palette.compatibility.CONFIDENCE_THRESHOLD` and never re-declared |
| `DISAGREEMENT_DELTA` | 0.50 | Absolute source disagreement above which a dimension is reported |
| `ALTERNATIVES_LIMIT` | 3 | The most alternative IDs a result carries |
| `WEIGHT_SUM_TOLERANCE` | 1e-9 | The existing #12 tolerance a hybrid weight table must satisfy |
| `HYBRID_DIMENSIONS` | frequency, transient, tonal, rhythmic, texture, arrangement | The six contract dimension literals in the contract's own order |
| `HYBRID_WEIGHTED_DIMENSIONS` | frequency, transient, tonal, texture, arrangement | The five dimensions `HYBRID_WEIGHTS` weights |

`LOW_CONFIDENCE_THRESHOLD` and `DISAGREEMENT_DELTA` are provisional constants owned by this
task; [#19](https://github.com/gmphto/tera/issues/19) owns tuning them against producer ratings.

## Weight tables and the interaction with #12

| Table | Dimension | Weight | Decimal |
| --- | --- | ---: | ---: |
| `dsp-baseline-weights-1` | `frequency` | 3/7 | 0.42857142857142855 |
| `dsp-baseline-weights-1` | `transient` | 2/7 | 0.2857142857142857 |
| `dsp-baseline-weights-1` | `tonal` | 2/7 | 0.2857142857142857 |
| `hybrid-weights-1` | `frequency` | 0.30 | 0.3 |
| `hybrid-weights-1` | `transient` | 0.20 | 0.2 |
| `hybrid-weights-1` | `tonal` | 0.20 | 0.2 |
| `hybrid-weights-1` | `texture` | 0.10 | 0.1 |
| `hybrid-weights-1` | `arrangement` | 0.20 | 0.2 |

`dsp-baseline-weights-1` is the product plan's `frequencyFit` 0.3, `transientFit` 0.2 and
`tonalFit` 0.2 renormalized over the three dimensions #12 scores. `hybrid-weights-1` is the
plan's five-dimension example table (`frequencyFit` 0.3, `transientFit` 0.2, `tonalFit` 0.2,
`textureFit` 0.1, `arrangementFit` 0.2, `_docs/plan.md` section 6), so its three DSP
dimensions carry 0.30 + 0.20 + 0.20 = 0.70 of the total. The two tables therefore agree exactly on
the three shared dimensions after renormalization, within 1e-9:

```text
0.30 / 0.70 == 3 / 7
0.20 / 0.70 == 2 / 7
0.10 + 0.20 == 0.30        # the two Jev-only dimensions' weight
```

A `HybridWeightTable` refuses any other table whose identifier is blank or whose five weights are
not finite, positive and within 1e-9 of 1; the returned `weight_table_id` records the table
actually used, and a custom table is accepted and recorded in full.

## The six dimensions and their sources

| Dimension | #12 DSP dimension score | #13 Jev question | Weight in `hybrid-weights-1` | Rule |
| --- | --- | --- | ---: | --- |
| `frequency` | yes | yes | 0.30 | Both sources; combined as their mean when both exist |
| `transient` | yes | yes | 0.20 | Both sources; combined as their mean when both exist |
| `tonal` | yes | yes | 0.20 | Both sources; combined as their mean when both exist |
| `rhythmic` | no | yes | 0.0 | Unweighted: recorded, never scored |
| `texture` | no | yes | 0.10 | Jev only; scored from the judgment alone and counted in coverage |
| `arrangement` | no | yes | 0.20 | Jev only; scored from the judgment alone and counted in coverage |

A dimension present in one source only is **single-source evidence, not a missing-evidence case**:
`texture` and `arrangement` are scored from the judgment alone and their weight counts in
coverage, and a DSP-only dimension is scored from the baseline score alone. Neither fabricates an
absent counterpart, no dimension is dropped for lacking one source, and every record carries all six
entries in the order above.

A dimension is *missing evidence* — `unavailable_reason` `no_evidence` — only when neither a DSP
dimension score nor a usable judgment exists for that candidate. `rhythmic` has no DSP dimension
and weight 0.0: a supplied rhythmic judgment is carried in the record with weight 0.0 and
`unavailable_reason` `unweighted_dimension` (never `no_evidence`), and it contributes nothing
to compatibility, confidence, ordering, tie-breaking, alternatives or mode. An unsupplied
`rhythmic` entry is `no_evidence` like any other dimension with no source.

## Per-dimension combination

For a weighted dimension with at least one source:

```text
dimension_score = (dsp_score + jev_score) / 2      when both exist   (SOURCE_SHARE = 0.5 each)
dimension_score = dsp_score                        when only the DSP baseline scores it
dimension_score = jev_score = JEV_LABEL_SCORES[label]   when only the judgment scores it
```

The overall scores are the #12 renormalization applied to this table:

```text
compatibility = sum(weight[d] * dimension_score[d] for covered d) / sum(weight[d] for covered d)
confidence    = sum(weight[d] for covered d)
```

`confidence` is the total weight covered by the dimensions that have a `compatibility` value, in
[0, 1], and nothing else — not a count of dimensions and not a calibration of them. Each Judgment's
probabilities are recorded unchanged as provenance for
[#19](https://github.com/gmphto/tera/issues/19); version 1 derives no score from them.

The difference from #12 is the table: #12's confidence is a fraction of
`dsp-baseline-weights-1`, whose three weights sum to 1, so a fully covered DSP candidate has
confidence 1.00. Recommendation confidence is the covered weight of *this run's* table, so in a
hybrid run a candidate whose only evidence is those same three DSP dimensions has
`confidence` 0.70 (0.30 + 0.20 + 0.20 of `hybrid-weights-1`), not #12's 1.00. A whole-run
DSP-only fallback is the exception and still reports #12's own values (see the fallback section).

## Evidence rules

`HybridEvidence(dimension, judgment=None, unavailable_reason=None)` carries exactly one of a
judgment and an upstream reason. Every dimension's entry keeps the two sources separate and
records:

| Field | Meaning |
| --- | --- |
| `dimension` | The contract dimension literal |
| `weight` | This run's table weight for the dimension; 0.0 for `rhythmic` |
| `dsp_compatibility` | The #12 dimension score, or null when #12 does not score the dimension or returned it unavailable |
| `dsp_unavailable_reason` | The baseline's own upstream reason verbatim; null when #12 scores the dimension and when #12 has no such dimension |
| `jev_label` | The judgment's label, or null |
| `jev_score` | `JEV_LABEL_SCORES[label]` for a labeled judgment in a weighted dimension, otherwise null |
| `jev_confidence` | The judgment's own confidence, unchanged, or null |
| `jev_probabilities` | The judgment's five `LabelProbability` entries, unchanged, or empty |
| `jev_model_version` / `jev_prompt_version` | The judgment's own versions, or null |
| `jev_unavailable_reason` | The upstream #13/#14 code verbatim, or `jev_abstained` for an abstention, or null |
| `compatibility` | The combined value, or null |
| `unavailable_reason` | The combined reason when `compatibility` is null: `no_evidence` or `unweighted_dimension` |
| `reason` | One human-readable string naming both inputs and the result |

A null judgment with a nonblank `unavailable_reason` records that upstream code verbatim (a #13
`UnavailableQuestion` code such as `song_context_absent` or `kick_key_unknown`, or a #14
outcome code), scores the dimension from the DSP baseline score when one exists, and adds a warning
naming the dimension and the code; when no DSP score exists either, the dimension is
`no_evidence`. An entry carrying both a judgment and a reason, or neither, is
`invalid_evidence` rather than a fabricated judgment.

A judgment with a null label — #13's `model_abstained` abstention — is recorded with provenance
`jev_abstained` in `jev_unavailable_reason` and contributes no `jev_score`. An
`UnavailableQuestion` code or a #14 transport outcome is recorded in the same field; a
`JevJudgment` with a label is the only thing that produces a `jev_score`. Neither an abstention
nor an unavailable reason ever produces a scored Jev dimension, a Jev contribution to
compatibility, or a fabricated label, confidence, probability or model version. An abstention adds
no warning; its provenance is the `jev_abstained` code. The probabilities of a labeled judgment
are recorded unchanged, but no version-1 score is derived from them.

All four states of one dimension are therefore distinguishable, for `frequency`:

| State | `dsp_compatibility` | `jev_score` | `compatibility` | `unavailable_reason` |
| --- | --- | --- | --- | --- |
| Judgment only | null | label score | the `jev_score` | null |
| Both sources | the #12 value | label score | their mean | null |
| DSP only with an explicit unavailable entry | the #12 value | null | the `dsp_compatibility` | null |
| Neither | null | null | null | `no_evidence` |

## Disagreement

When a weighted dimension has both sources and `abs(dsp_score - jev_score) > 0.50`
(`DISAGREEMENT_DELTA`, strictly greater), both values are kept unchanged, the combined score is
still the documented mean, and the record carries the warning `dimension_disagreement` naming the
dimension and both values. Nothing is clamped, substituted, averaged away, reordered or rejected,
and no Jev value is ever written into a DSP field or the reverse.

## Confidence, compatibility and ordering stay separate

No judgment confidence is multiplied into, added to, compared with or used to order a compatibility
score. Changing only a judgment's `confidence` changes no dimension score, no compatibility value,
no rank and no tie-break. Confidence determines exactly two things: the candidate's `uncertain`
flag and, through it, whether the result offers alternatives.

The two tables are proportional on the three dimensions #12 scores, so a candidate with no Jev
evidence at all keeps the same compatibility in a hybrid run as in the baseline — the weighted means
renormalize to the same value — and differs only in `confidence`, 0.70 instead of 1.00. That is
the whole point of separating compatibility from confidence: the same ordering, a different
statement about how much evidence supports it.

## Uncertainty and alternatives

`HybridCandidate.uncertain` is true when the record's `confidence` is strictly below
`LOW_CONFIDENCE_THRESHOLD` (0.60), or when any judgment that contributed a `jev_score` has a
`confidence` strictly below `JEV_CONFIDENCE_THRESHOLD` (0.80). Its warnings then carry
`low_coverage` and/or `low_jev_confidence`, each naming the triggering value, and a candidate
with no usable judgment in any weighted dimension carries `candidate_no_jev_evidence`.

`HybridResult.alternatives` is empty when no ranked candidate is uncertain and otherwise lists up
to `ALTERNATIVES_LIMIT` (3) ranked candidate IDs in rank order taken after rank 1 — unique, all
present in `ranked`, matching the schema-1 `alternatives` rule. Overlap with the returned results
is allowed. Alternatives never change `ranked`, `unscored` or any score.

## Tie-break

`ranked` is sorted by `compatibility` descending. Exact float equality — no epsilon — is a tie,
broken by `candidate_id` ascending using Python's locale-independent code-point comparison and
nothing else. `unscored` is sorted by `candidate_id` ascending. Ranks are assigned 1..n after
ordering. This is #12's own rule, reused rather than reinvented: two candidates whose combined
compatibility is exactly equal, whether both are DSP-only, both Jev-only or one of each, are ordered
by ID alone.

## Mode, status and the DSP-only fallback

| Value | Appears as | Meaning |
| --- | --- | --- |
| `hybrid` | `HybridResult.mode` | At least one candidate has a usable judgment in a weighted dimension |
| `dsp-only` | `HybridResult.mode` | No candidate has a usable judgment in any weighted dimension |
| `jev_present` | `HybridResult.jev_status` | Every scored candidate has a usable judgment in every weighted dimension |
| `jev_partial` | `HybridResult.jev_status` | At least one usable judgment exists, but some scored candidate lacks one in a weighted dimension |
| `jev_absent` | `HybridResult.jev_status` | No candidate has a usable judgment in any weighted dimension |

`mode` uses exactly the schema-1 `RecommendationBatch` literals `"hybrid"` and `"dsp-only"`;
`"jev-only"` is not produced here — [#19](https://github.com/gmphto/tera/issues/19) owns that arm.
An empty baseline has no usable judgment and is therefore `dsp-only` / `jev_absent` with empty
`ranked`, `unscored` and `alternatives`. A rhythmic judgment never changes `mode` or
`jev_status`, because it is unweighted.

When the run is `dsp-only`, every ranked and unscored record reproduces the #12 record's own rank
order, `compatibility`, `confidence`, `dsp_dimensions`, `reasons` and `warnings` exactly, so
the fallback cannot drift from `rank_candidates`; the hybrid-only warning strings listed below are
therefore not added in `dsp-only` mode, where `mode` and `jev_status` already name the run's
state for every candidate. The result also reports the baseline's own `ranking_version`
(`RANKING_VERSION` = `"dsp-baseline-v1"`) and `weight_table_id` (`DEFAULT_WEIGHT_TABLE_ID` =
`"dsp-baseline-weights-1"`), because those are the mapping and the table that produced the record
values; a `hybrid` run reports `HYBRID_RANKING_VERSION` and its policy table's identifier.
`hybrid_dimensions`, `uncertain` and `alternatives` are still computed in both modes from the
rules above; `uncertain` in a fallback therefore follows the record's #12 confidence. This is the one place where a record can be `uncertain` without carrying
the matching `low_coverage` warning string, and it is required by the fallback equality.

A candidate with no Jev judgment at all in a **hybrid** run is still ranked and labelled: its
dimensions are the DSP baseline's dimensions alone, it carries `candidate_no_jev_evidence`, its
confidence is its DSP coverage (0.70 when all three DSP dimensions exist), and it is never dropped,
never scored 0, never marked `no_evidence` and never given an invented judgment.

## Codes

Every value of `HYBRID_CODES` with its meaning and where it appears:

| Code | Where | Meaning |
| --- | --- | --- |
| `no_evidence` | `HybridDimensionScore.unavailable_reason` | Neither a DSP score nor a usable judgment exists for the dimension |
| `unweighted_dimension` | `HybridDimensionScore.unavailable_reason` | A supplied judgment on a weight-0 dimension; it is recorded and never scored |
| `jev_abstained` | `HybridDimensionScore.jev_unavailable_reason` | A null-label `model_abstained` judgment; no Jev score |
| `jev_evidence_unavailable` | warning prefix | A weighted dimension's entry carried an upstream code and no usable judgment |
| `dimension_disagreement` | warning prefix | `abs(dsp - jev) > DISAGREEMENT_DELTA`; both values kept |
| `candidate_no_jev_evidence` | warning prefix | A scored candidate has no usable judgment in any weighted dimension |
| `low_coverage` | warning prefix | The candidate's confidence is below `LOW_CONFIDENCE_THRESHOLD` |
| `low_jev_confidence` | warning prefix | A contributing judgment's confidence is below `JEV_CONFIDENCE_THRESHOLD` |

Warning strings are `"<code>: <human-readable detail>"`; the leading code is the stable part and
the detail names the dimension, the upstream code or the triggering value. A record's warnings are
the baseline's own warnings first, then the per-dimension warnings in contract dimension order, then
`candidate_no_jev_evidence`, `low_coverage` and one `low_jev_confidence` per triggering
dimension. `candidate_no_jev_evidence` is added to every scored candidate that has no usable
judgment in any weighted dimension — including one whose evidence is an explicit unavailable reason
or an abstention, because neither is a usable judgment. The #12 baseline warning
about mixed analysis versions is reproduced unchanged as the first warning of a record that has it.
Reason and warning strings contain no local path, no sample ID and no non-finite text.

## The #14 outcome-state table

#14's `OUTCOME_STATES` are the eight states one adapter outcome can carry
(`_docs/jev-adapter.md`). This is the evidence #15 records for each; the mapping is implemented by
the caller ([#28](https://github.com/gmphto/tera/issues/28)), proved against #14's contract double
in `tests/test_hybrid_flow.py`, and no product code here issues a Jev call.

| `OUTCOME_STATES` value | `HybridEvidence` for the dimension | Recorded as | Jev score |
| --- | --- | --- | --- |
| `judged` | `judgment=outcome.judgment` | label, confidence, probabilities and model/prompt versions | `JEV_LABEL_SCORES[label]` |
| `abstained` | `judgment=outcome.judgment` (null label) | `jev_unavailable_reason` `jev_abstained` | none |
| `not_asked` | `unavailable_reason=<#13 question code>` | the code verbatim and a `jev_evidence_unavailable` warning | none |
| `not_attempted` | `unavailable_reason=cancelled` or `batch_deadline_exceeded` | the code verbatim and a `jev_evidence_unavailable` warning | none |
| `invalid_result` | `unavailable_reason=<#13 response code>` | the code verbatim and a `jev_evidence_unavailable` warning | none |
| `service_error` | `unavailable_reason=<transport code>` | the code verbatim and a `jev_evidence_unavailable` warning | none |
| `timed_out` | `unavailable_reason=timeout` | the code verbatim and a `jev_evidence_unavailable` warning | none |
| `unavailable` | `unavailable_reason=credentials_absent` | the code verbatim; a whole-run absence gives the `dsp-only` fallback | none |

The code spaces are #13's `QUESTION_UNAVAILABLE_CODES` (for example `song_context_absent`,
`kick_key_unknown`), #14's `RESPONSE_ERROR_CODES` (for example `invalid_response`), #14's
`TRANSPORT_ERROR_CODES` (`connection_failed`, `service_unavailable`, `service_error`,
`rate_limited`, `timeout`, `invalid_credentials`), #14's `NOT_ATTEMPTED_CODES`
(`cancelled`, `batch_deadline_exceeded`) and #14's `UNAVAILABLE_CODES`
(`credentials_absent`). `model_abstained` is #13's abstention token; #15 records it as the
`jev_abstained` provenance instead of copying it, so an abstention can never be mistaken for a
transport code.

No failed, abstained, invalid, cancelled, timed-out, not-asked or unavailable outcome ever becomes a
judgment or a score: the only state that produces a `jev_score` is `judged`, and the only state
that carries a judgment without a score is `abstained`.

## What #19 gets

A consumer can assemble a schema-1 `RecommendationBatch` directly from a `HybridResult`: the
run's `mode` and `jev_status`, `ranking_version`, `weight_table_id`, each record's
`dsp_dimensions` and `jev_judgments`, the combined `compatibility`, the coverage
`confidence`, the `uncertain` flag, the per-dimension reason strings and the six-dimension
evidence with the DSP and Jev values kept separate, each judgment's label, confidence,
probabilities, model version and prompt version. `backend.contracts` stays at schema 1.0: no field
is added and the wire representation of an unscored candidate is
[#62](https://github.com/gmphto/tera/issues/62). Retrieval similarity
([#25](https://github.com/gmphto/tera/issues/25)) is never an input here and is never used in
compatibility, confidence, ordering, tie-breaking or alternatives; the caller supplies the
`similarity_unavailable_reason` it needs.

## Versioning and the local-only rule

Any change to a weight, the label map, a threshold, the tie-break or a code meaning requires a new
`HYBRID_RANKING_VERSION` value, and a new weight-table identifier for any weight change.
`RANKING_VERSION`, `DEFAULT_WEIGHT_TABLE_ID` and `rank_candidates` are unchanged by this task,
and schema version 1.0 does not change.

Audio and local library paths stay on the device. No path, sample ID, credential or third-party
audio appears in a hybrid record, a reason or a warning; the sample mapping stays with the caller.

## Worked example

The fixtures are synthetic declared values — every record is built from schema-1 fields and no audio
is read. Kick: bands `0.50/0.30/0.10/0.05/0.03/0.02`, `attack` 10 ms, `decay` 250 ms,
`transient_strength` 0.80, key C major confidence 0.90, fundamental 55 Hz confidence 0.90.
Candidate `bass-complement`: bands `0.00/0.10/0.30/0.40/0.15/0.05`, `transient_position` 300 ms,
key C major confidence 0.90, fundamental 110 Hz confidence 0.90, so #12 scores it
`frequency` 0.90, `transient` 1.00 and `tonal` 1.00.

With one `excellent` `frequency` judgment at confidence 0.90:

```text
frequency    dsp 0.900, jev 1.000 -> (0.900 + 1.000) / 2 = 0.950
transient    dsp 1.000, no judgment -> 1.000
tonal        dsp 1.000, no judgment -> 1.000
texture      no source -> no_evidence
arrangement  no source -> no_evidence
rhythmic     no source -> no_evidence, weight 0.000
covered        = 0.30 + 0.20 + 0.20 = 0.70
compatibility = (0.30 * 0.950 + 0.20 * 1.000 + 0.20 * 1.000) / 0.70 = 0.9785714285714288
confidence    = 0.70
uncertain     = false        (0.70 >= 0.60 and 0.90 >= 0.80)
mode          = hybrid, jev_status = jev_partial
```

A conflicting candidate with bands `0.60/0.30/0.05/0.03/0.01/0.01`, `transient_position` 100 ms
and key F# major is scored by #12 as `frequency` 0.20, `transient` 0.5076923076923077 and
`tonal` 0.10. With one `very-poor` `frequency` judgment its `frequency` combination is
`(0.20 + 0.00) / 2 = 0.10`, its covered weight is 0.70 and its compatibility is
0.21648351648351646. With an `excellent` judgment instead, the dimension is reported as
`dimension_disagreement` (0.800 apart, above the 0.500 delta), the combined value is still the
mean 0.60, and the record's compatibility is 0.4307692307692308.

## Local verification

```text
uv run pytest tests/test_hybrid_ranking.py tests/test_hybrid_flow.py --basetemp .pytest_cache/hybrid-focused
uv run pytest --basetemp .pytest_cache/hybrid-full
```

Focused validation: 77 passed (54 in `tests/test_hybrid_ranking.py`, 23 in
`tests/test_hybrid_flow.py`). The full suite after this work is 1474 passed / 4 failed / 1 skipped
(1479 collected), against the 1397 passed / 4 failed / 1 skipped baseline (1402 collected). The four
failures are pre-existing and sandbox-only: they spawn a subprocess with captured pipes, which this
environment forbids (`tests/test_batch.py`, `tests/test_evaluation_manifest.py`,
`tests/test_evaluation_prepare.py`), and they are unrelated to hybrid ranking. Both runs used a
temporary base directory outside the repository so no test artifact is left in it.

All evidence for this task is local and synthetic, and the live TypeSafe Jev integration stays
UNVERIFIED exactly as [#14](https://github.com/gmphto/tera/issues/14) reports it: no credential,
sample, participant or host access is required, so none is claimed. This is a Phase 0 feasibility
combination, not calibrated musical truth; the weights, the label map and both thresholds are
provisional, and [#16](https://github.com/gmphto/tera/issues/16),
[#19](https://github.com/gmphto/tera/issues/19) and [#25](https://github.com/gmphto/tera/issues/25)
own evaluation, comparison and retrieval.
