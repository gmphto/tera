# Deterministic DSP-only kick-to-bass compatibility baseline

`backend.palette.ranking.rank_candidates(kick, candidates, *, policy)` is a
pure, local, Jev-free baseline. It consumes the bass/sub-bass candidates that
`backend.palette.compatibility.filter_candidates` ([#11](https://github.com/gmphto/tera/issues/11))
already made eligible and ranks them against one selected kick using only the
kick and candidate records. It imports nothing from `backend.intelligence`,
opens no files and makes no network or credential access.

```python
from backend.palette.ranking import RankingPolicy, rank_candidates

result = rank_candidates(selected_kick, eligible_bass_samples, policy=RankingPolicy())
result.ranking_version   # "dsp-baseline-v1"
result.weight_table_id   # "dsp-baseline-weights-1"
result.ranked            # scored records with rank 1..n, best first
result.unscored          # explicit insufficient_evidence records, ID ascending
```

The returned object is immutable: `RankingResult`, `ScoredCandidate` and
`UnscoredCandidate` are frozen dataclasses. Inputs are never mutated, and the
output depends only on the supplied values: there is no clock, randomness,
locale, hash iteration or process state in the computation or the ordering.

## Entry-point contract

`rank_candidates` revalidates every supplied record the same way the filter
does: `type(value) is Sample` plus a full `Sample.from_dict(sample.to_dict())`
round trip, so a tampered or malformed object is rejected instead of ranked.
`policy` is keyword-only and must be an explicit `RankingPolicy`. Song context
is not required and is not used; the three dimensions read only the kick and
candidate records.

`RankingInputError` inherits `ValueError` and exposes a stable `.code` plus an
explanatory message. Nothing is dropped or silently ranked:

| Code | Condition |
| --- | --- |
| `invalid_policy` | Not a `RankingPolicy`, or its `WeightTable` is malformed (nonblank identifier, finite positive weights summing to 1 within `1e-9`) |
| `invalid_kick` | Selected record is not a valid `Sample`, fails contract revalidation, or does not have role `kick` |
| `invalid_candidates` | Candidates are not a non-string sequence of valid `Sample` objects, or a candidate role is not `bass` or `sub-bass` |
| `duplicate_candidate_id` | Two candidates share a sample ID |
| `selected_kick_in_candidates` | A candidate shares the selected kick ID. #11 excludes that ID from its eligible list; here it is an input error, never a ranked or dropped candidate |

An empty candidate sequence is valid and returns an empty ranked and unscored
list. Availability, file state and #11's usability rules are not re-run: the
candidates are already admitted, and the kick is assumed to have passed #11.

## Output records

Each ranked record is a `ScoredCandidate` with `candidate_id`,
`analysis_version` (the candidate's own version), `rank` (1..n), `compatibility`,
`confidence`, `dsp_dimensions`, `reasons` and `warnings`. Each unscored record
is an `UnscoredCandidate` with `candidate_id`, `analysis_version`, `code`,
`dsp_dimensions` and `reasons`.

Every record carries exactly one `DimensionScore` entry for each of the three
contract dimensions, in the fixed order `frequency`, `transient`, `tonal`.
`rhythmic`, `texture` and `arrangement` never appear, and the baseline has no
Jev field at all: `jev_judgments` is always empty when the result is mapped
into a schema-1 `RecommendationBatch`. A scored entry has a `compatibility` in
[0, 1] and no `unavailable_reason`; an unavailable entry has `compatibility`
null and a nonblank stable `unavailable_reason`. An unavailable dimension is
never scored as zero.

## Dimensions

The contract dimension literals are `frequency`, `transient` and `tonal`; they
are the plan's `frequencyFit`, `transientFit` and `tonalFit`. Each mapping below
produces a compatibility value in [0, 1] where higher means more compatible.

### frequency

Measured inputs: the six band ratios `band_sub`, `band_bass`, `band_low_mid`,
`band_mid`, `band_high_mid`, `band_high` of both sides. The low band is
`band_sub + band_bass`.

```text
kick_total      = sum of the kick's six band ratios
candidate_total = sum of the candidate's six band ratios
kick_share      = (kick.band_sub + kick.band_bass) / kick_total
candidate_share = (candidate.band_sub + candidate.band_bass) / candidate_total
shared          = min(kick_share, candidate_share)
compatibility   = 1 - shared
```

Band ratios need not sum to one (`_docs/spectral.md`), so each side is
normalized by its own total; the below-20 Hz and above-20 kHz power that stays
in the spectral denominator is therefore excluded from both. `shared` is the
low-band concentration both sides claim, limited by the side that emphasizes
the low band less. Higher means more compatible: a candidate with no low-band
energy scores exactly 1, and two sides that both concentrate in the low band
score near 0.

### transient

Measured inputs: kick `attack` (ms), kick `decay` (ms), kick
`transient_strength` (normalized) and candidate `transient_position` (ms).
`transient_position` is the candidate's observed onset time from its own clip
start; `attack` and `decay` are the measured attack time and decay length
(`_docs/transient.md`), so the kick's decay window is approximated as
`[attack, attack + decay]` ms and the kick's own `transient_position` is not
an input.

```text
window_end    = kick.attack + kick.decay
position      = 1 if window_end <= 0 else min(candidate.transient_position, window_end) / window_end
compatibility = 1 - (1 - position) * kick.transient_strength
```

A candidate that attacks inside the kick's decay window competes with it and
scores below 1; a candidate that attacks at or after `window_end` scores
exactly 1. A kick with `transient_strength` 0 has no transient to collide with,
so every candidate scores exactly 1. A zero-length window (`attack` and
`decay` both 0) also scores exactly 1.

### tonal

Measured inputs: both sides' schema-1 `MusicalKey` (`tonic`, `mode`,
`confidence`) and, as a reliability guard, both sides' `fundamental`
measurement. Keys are the only compared tonal evidence: a fundamental never
substitutes for a key on either side, mirroring the filter policy's rule that
F0 never becomes key evidence.

```text
for each side in (kick, candidate):
    key.tonic is None                        -> unavailable <side>_key_unknown
    key.confidence < 0.80                    -> unavailable <side>_key_unreliable
    fundamental known and confidence < 0.80  -> unavailable <side>_f0_unreliable
kick side is checked first; a usable key takes precedence over an unusable
fundamental, and an unknown fundamental is not an error.

distance      = |pitch_class(kick.tonic) - pitch_class(candidate.tonic)| % 12
interval      = min(distance, 12 - distance)          # interval class in semitones
compatibility = TONAL_INTERVAL_COMPATIBILITY[interval]
```

Pitch classes use the schema-1 tonic literals in the order C, C#, D, D#, E, F,
F#, G, G#, A, A#, B (C = 0). `mode` does not change the score; it is reported
in the reason text.

| Interval class (semitones) | Relationship | Compatibility |
| ---: | --- | ---: |
| 0 | unison / octave | 1.00 |
| 3 | minor third | 0.75 |
| 4 | major third | 0.85 |
| 5 | perfect fourth / fifth | 0.95 |
| 2 | major second | 0.30 |
| 1 | minor second | 0.10 |
| 6 | tritone | 0.10 |

These coefficients are a provisional, uncalibrated ordering by conventional
consonance; they are not calibrated against producer ratings, and [#16](https://github.com/gmphto/tera/issues/16)
and [#19](https://github.com/gmphto/tera/issues/19) own tuning and evaluation.

Unknown or unreliable tonal evidence on either side makes `tonal` unavailable
with a reason naming the side and the input, and it is never treated as a key
or pitch clash. Partial evidence is not combined: a reliable kick fundamental
with an unknown candidate key still returns `candidate_key_unknown`, and a
known but unreliable fundamental on either side blocks the dimension rather
than contributing a lower score.

## Weight table

| Dimension | Weight | Decimal |
| --- | ---: | ---: |
| `frequency` | 3/7 | 0.42857142857142855 |
| `transient` | 2/7 | 0.2857142857142857 |
| `tonal` | 2/7 | 0.2857142857142857 |

Identifier: `dsp-baseline-weights-1`. The values are the three DSP weights of
the product plan's example weighting (`frequencyFit` 0.3, `transientFit` 0.2,
`tonalFit` 0.2) renormalized over the dimensions this baseline scores; they are
positive and sum to 1. A `WeightTable` refuses any other table whose weights
are not finite, positive and within `1e-9` of 1, and the returned
`weight_table_id` records the table actually used.

## Missing features, renormalization and confidence

`compatibility` is the weighted mean over the *available* dimensions only,
with the covering weights renormalized:

```text
compatibility = sum(weight[d] * score[d] for available d) / sum(weight[d] for available d)
confidence    = sum(weight[d] for available d)
```

`confidence` is therefore the total weight covered by the available
dimensions, in [0, 1] (1 when all three are available, 4/7 when frequency is
unavailable, 2/7 when only one dimension is available). Compatibility and
confidence stay separate: measurement confidences at or above the threshold
are never multiplied into or added to a score, they change no compatibility
value, and they never determine order. A candidate with no available dimension
gets no compatibility value at all: it is returned in `unscored` with the
stable code `insufficient_evidence` and with all three dimension entries
marked unavailable, and it never appears in `ranked`.

## Reliability threshold

The threshold is **0.80**, the same value the #11 filter applies to its key and
tempo gates
(`backend.palette.compatibility.CONFIDENCE_THRESHOLD`, imported rather than
duplicated). It is applied to key confidence and, when a fundamental is known,
to fundamental confidence. A value just below 0.80 makes the dimension
unavailable; a value exactly at or just above 0.80 is reliable. A
below-threshold value never produces a lower score — it produces an
unavailable dimension, and the reason text shows the rejected value with full
precision.

## Tie-break

`ranked` is sorted by `compatibility` descending. Exact float equality — no
epsilon — is a tie, and a tie is broken by `candidate_id` ascending using
Python's locale-independent code-point comparison. Nothing else breaks a tie.
`unscored` is sorted by `candidate_id` ascending. Ranks are then assigned
1..n in that order.

## Reason codes and reason strings

Stable `unavailable_reason` codes, by dimension:

| Dimension | Code | Meaning |
| --- | --- | --- |
| `frequency` | `kick_band_energy_zero` | All six kick band ratios are 0 |
| `frequency` | `candidate_band_energy_zero` | All six candidate band ratios are 0 |
| `transient` | `kick_transient_strength_unknown` | Kick `transient_strength` is null |
| `transient` | `kick_attack_unknown` | Kick `attack` is null |
| `transient` | `kick_decay_unknown` | Kick `decay` is null |
| `transient` | `candidate_transient_position_unknown` | Candidate `transient_position` is null |
| `tonal` | `kick_key_unknown` / `candidate_key_unknown` | That side's key has no tonic |
| `tonal` | `kick_key_unreliable` / `candidate_key_unreliable` | That side's key confidence is below 0.80 |
| `tonal` | `kick_f0_unreliable` / `candidate_f0_unreliable` | That side's known fundamental confidence is below 0.80 |

Within `transient` the codes are reported in the order listed above (kick
strength, kick attack, kick decay, candidate position). Within `tonal` the
kick side is reported before the candidate side and, per side, the key before
the fundamental. The only record code is `insufficient_evidence`.

Each record has exactly one human-readable reason string per dimension, in the
same fixed order. A scored reason names the measured inputs and the resulting
value, and an unavailable reason names the code plus the side and input, for
example:

```text
frequency: kick low band 0.800 of 1.000 (band_sub 0.500, band_bass 0.300), share 0.800; candidate low band 0.100 of 1.000 (band_sub 0.000, band_bass 0.100), share 0.100; shared concentration 0.100; compatibility 0.900
transient: kick attack 10.000 ms, kick decay 250.000 ms, window end 260.000 ms; kick transient strength 0.800; candidate transient position 300.000 ms; onset ratio 1.000; compatibility 1.000
tonal: kick key C major confidence 0.900, kick fundamental 55.000 Hz confidence 0.900; candidate key C major confidence 0.900, candidate fundamental 110.000 Hz confidence 0.900; tonic interval class 0 semitones; compatibility 1.000
```

Scored values are rendered with three decimals; a rejected below-threshold
confidence is rendered with its shortest round-trip form so the comparison is
visible. Reason strings never contain local paths, sample identifiers,
non-finite text or Jev data.

`warnings` is empty for a candidate whose `analysis_version` equals the
kick's. A candidate with a different version is still ranked, with a warning
naming both versions instead of a silent cross-version comparison. Records are
echoed with their own `analysis_version`.

## Versioning

`RANKING_VERSION = "dsp-baseline-v1"` identifies this mapping, the default
weight-table identifier `dsp-baseline-weights-1`, the 0.80 reliability
threshold, the confidence rule and the tie-break, and the result records it.
Any change to a weight, mapping, threshold, tie-break or reason-code meaning
requires a new version value (and, for weights, a new weight-table identifier);
schema version 1.0 does not change.

## Worked example

The fixtures below are synthetic declared values (every record is built from
schema-1 fields; no audio is read). Kick: bands `0.50/0.30/0.10/0.05/0.03/0.02`
(sub/bass/low-mid/mid/high-mid/high, total 1.00), `attack` 10 ms, `decay`
250 ms, `transient_strength` 0.80, key C major confidence 0.90, fundamental
55 Hz confidence 0.90. Candidate `bass-complement`: bands
`0.00/0.10/0.30/0.40/0.15/0.05` (total 1.00), `transient_position` 300 ms, key
C major confidence 0.90, fundamental 110 Hz confidence 0.90.

- `frequency`: kick share 0.80/1.00 = 0.80, candidate share 0.10/1.00 = 0.10,
  shared 0.10, compatibility 0.90.
- `transient`: window end 260 ms, ratio min(300, 260)/260 = 1, compatibility
  1 - (1 - 1) * 0.80 = 1.00.
- `tonal`: same tonic, interval class 0, compatibility 1.00.
- `compatibility` = (0.428571... * 0.90 + 0.285714... * 1.00 + 0.285714... * 1.00) /
  (0.428571... + 0.285714... + 0.285714...) = 6.7/7 = 0.957142857142857.
- `confidence` = 1.

A conflicting candidate with bands `0.60/0.30/0.05/0.03/0.01/0.01` (low share
0.90), `transient_position` 100 ms and key F# major scores `frequency` 0.20
(shared 0.80), `transient` 1 - (1 - 100/260) * 0.80 = 0.5076923076923077 and
`tonal` 0.10 (interval class 6), giving `compatibility` ≈ 0.2593406593406593
and `confidence` 1.

```text
uv run pytest tests/test_dsp_baseline.py --basetemp .pytest_cache/dsp-baseline-focused
uv run pytest --basetemp .pytest_cache/dsp-baseline-full
```

Focused validation: 62 passed. The full suite after this work is 854 passed /
4 failed (858 collected), against the 792 passed / 4 failed baseline (796
collected). The four failures are pre-existing and sandbox-only: they spawn a
subprocess with captured pipes, which this environment forbids
(`test_batch.py`, `test_evaluation_manifest.py`, `test_evaluation_prepare.py`),
and they are unrelated to this baseline.

This is a Phase 0 feasibility baseline, not calibrated musical truth. The
weights and the tonal interval coefficients are provisional, the mappings use
only the measurements named above, and no claim is made that the ordering
matches producer preference; [#16](https://github.com/gmphto/tera/issues/16),
[#19](https://github.com/gmphto/tera/issues/19) and [#25](https://github.com/gmphto/tera/issues/25)
own tuning, comparison and retrieval.
