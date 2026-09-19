# Narrow Jev questions and typed judgments

`backend/intelligence/questions.py` turns supplied schema-1 facts about one kick and one
bass/sub-bass candidate into at most one narrow question per contract `Dimension`, and
`backend/intelligence/decisions.py` validates one model response into an existing
`backend.contracts.JevJudgment`. This is Phase 0 product code: DSP owns measured facts, Jev owns
fuzzy judgment, and application code owns every weight, threshold and ordering
([#15](https://github.com/gmphto/tera/issues/15)). The modules are pure and local. They do not open
files, read audio, touch the network, measure anything or call Jev; the bounded transport and
adapter are [#14](https://github.com/gmphto/tera/issues/14).

```python
from backend.intelligence.questions import build_question
from backend.intelligence.decisions import validate_response

question = build_question(dimension, kick, candidate, song=song_context)  # or UnavailableQuestion
judgment = validate_response(question, response)  # a schema-1 JevJudgment
```

One question is one dimension. There is no combined or overall question; a question never asks for a
compatibility score, a rank or an ordering, and a response carrying an overall field
(`compatibility`, `score`, `rank`, `overall`) is rejected with `unexpected_field`.

## Question payload

`JevQuestion.to_dict()` and `to_json()` (sorted keys, no indentation) contain exactly six fields and
nothing else:

| Field | Meaning |
| --- | --- |
| `question_id` | Opaque deterministic digest of the dimension, the prompt version and the question's own facts |
| `dimension` | One contract `Dimension` literal |
| `prompt_version` | The `jev-questions-v1` value that owns the instruction wording |
| `instruction` | This dimension's instruction text, recorded verbatim below |
| `evidence` | Presented contract facts, each `side`, `name`, `value` and `unit` |
| `withheld` | Facts the question does not present, each `side`, `name` and `reason` |

```json
{
  "dimension": "transient",
  "evidence": [
    {"name": "attack", "side": "kick", "unit": "ms", "value": 12.0},
    {"name": "decay", "side": "kick", "unit": "ms", "value": 240.0},
    {"name": "transient_strength", "side": "kick", "unit": "normalized", "value": 0.72},
    {"name": "transient_position", "side": "candidate", "unit": "ms", "value": 95.0}
  ],
  "instruction": "...",
  "prompt_version": "jev-questions-v1",
  "question_id": "q-<64 hex digits>",
  "withheld": []
}
```

The payload carries no sample ID, no local path, no file name, no frame count, no audio, no audio
bytes and no free product reason text. Every question is built from records the caller already
holds, so the caller keeps the sample mapping; the question itself is anonymous.

## Answer states

A consumer can always tell three states apart:

1. **Not asked** - `build_question` returned an `UnavailableQuestion` with a stable `code`; no response is accepted for it.
2. **Abstained** - the model returned a null `label` with the `model_abstained` reason; the validated `JevJudgment` has a null label, a
   null confidence and no probabilities.
3. **Judged** - `JevJudgment` with a contract label, five probabilities and a
   confidence.

`neutral` and `poor` are labels, never abstentions: no unavailable code is a label literal, and an
abstention never carries a label. A question that was not asked is a record, an abstention is a
judgment with a null label, and a judged answer carries a label.

An `UnavailableQuestion` records its `dimension`, the first failure `code` and the `withheld` facts;
it carries no prompt version and no judgment, because nothing was asked.

## Question input errors

`build_question` revalidates every supplied record the way the filter and the DSP baseline do: exact
type plus a full `from_dict(to_dict())` round trip. A failure raises `QuestionInputError`, a
`ValueError` with a stable `.code` in `INPUT_ERROR_CODES`:

| Code | Condition |
| --- | --- |
| `invalid_dimension` | The dimension is not one of the six contract literals |
| `invalid_kick` | The selected record is not a valid `Sample`, fails revalidation, or does not have role `kick` |
| `invalid_candidate` | The candidate is not a valid `Sample`, its role is not `bass` or `sub-bass`, or it shares the kick's sample ID |
| `invalid_context` | The supplied song context is not a valid `SongContext` |

## Evidence by dimension

Every fact is a schema-1 contract fact: a `MEASURES` name or one of the documented non-measurement
names `key`, `role` and `genre`. Measurement facts carry the contract unit and the declared value.
`key` is presented as the documented string form, tonic and mode only, with no confidence. `role` is
the contract role literal and `genre` is the declared text. No fact is recomputed, rescaled or
invented from another fact, and a fundamental never substitutes for a key.

| Dimension | Side | Kind | Fact |
| --- | --- | --- | --- |
| `frequency` | `kick` | required | `band_sub` |
| `frequency` | `kick` | required | `band_bass` |
| `frequency` | `kick` | required | `band_low_mid` |
| `frequency` | `kick` | required | `band_mid` |
| `frequency` | `kick` | required | `band_high_mid` |
| `frequency` | `kick` | required | `band_high` |
| `frequency` | `candidate` | required | `band_sub` |
| `frequency` | `candidate` | required | `band_bass` |
| `frequency` | `candidate` | required | `band_low_mid` |
| `frequency` | `candidate` | required | `band_mid` |
| `frequency` | `candidate` | required | `band_high_mid` |
| `frequency` | `candidate` | required | `band_high` |
| `frequency` | `kick` | optional | `fundamental` |
| `frequency` | `candidate` | optional | `fundamental` |
| `frequency` | `kick` | optional | `loudness` |
| `frequency` | `candidate` | optional | `loudness` |
| `transient` | `kick` | required | `attack` |
| `transient` | `kick` | required | `decay` |
| `transient` | `kick` | required | `transient_strength` |
| `transient` | `candidate` | required | `transient_position` |
| `transient` | `kick` | optional | `crest_factor` |
| `transient` | `candidate` | optional | `crest_factor` |
| `transient` | `kick` | optional | `loudness` |
| `transient` | `candidate` | optional | `loudness` |
| `tonal` | `kick` | required | `key` |
| `tonal` | `kick` | required | `fundamental` |
| `tonal` | `candidate` | required | `key` |
| `tonal` | `candidate` | required | `fundamental` |
| `tonal` | `song` | optional | `key` |
| `tonal` | `song` | optional | `genre` |
| `rhythmic` | `candidate` | required | `tempo` |
| `rhythmic` | `song` | required | `tempo` |
| `rhythmic` | `song` | optional | `genre` |
| `rhythmic` | `kick` | optional | `transient_position` |
| `texture` | `kick` | required | `spectral_centroid` |
| `texture` | `kick` | required | `spectral_rolloff` |
| `texture` | `candidate` | required | `spectral_centroid` |
| `texture` | `candidate` | required | `spectral_rolloff` |
| `texture` | `kick` | optional | `loudness` |
| `texture` | `candidate` | optional | `loudness` |
| `texture` | `kick` | optional | `stereo_width` |
| `texture` | `candidate` | optional | `stereo_width` |
| `arrangement` | `kick` | required | `role` |
| `arrangement` | `candidate` | required | `role` |
| `arrangement` | `song` | optional | `genre` |
| `arrangement` | `song` | optional | `key` |

### Required order and the first failure

For each dimension the table above fixes the order: every required fact of the kick side, then the
candidate side, then the song side where a dimension needs it, followed by the optional facts in
their documented order. `UnavailableQuestion.code` is the first required failure in that order and
`withheld` lists every unusable fact of the dimension in the same order. The six band facts are
checked in `band_sub`, `band_bass`, `band_low_mid`, `band_mid`, `band_high_mid`, `band_high` order,
the kick side before the candidate side. Rhythmic checks the candidate tempo before the song tempo.

An `UnavailableQuestion` never carries a judgment and never invents one. A band-energy guard follows
each side's six band facts: when all six are usable and sum to 0, the dimension is unavailable with
`kick_band_energy_zero` or `candidate_band_energy_zero`, exactly the condition the DSP baseline
marks unavailable.

### Optional evidence never blocks

Optional facts are presented when they are actually known and usable. An unknown or unreliable
optional fact is withheld and recorded, and it never blocks the question. Arrangement uses the
declared kick and candidate roles plus the optional genre; the producer-specified intended role is
not implemented here ([#63](https://github.com/gmphto/tera/issues/63)).

### Withheld reasons

| Reason | Meaning |
| --- | --- |
| `unknown` | The fact is null: the contract has no value for it |
| `below_reliability_threshold` | The fact declares a confidence below 0.80 |
| `not_supplied` | The fact belongs to a song context the caller did not supply (`song is None`) |

A withheld fact never appears in `evidence`, is never rendered as a number and never becomes a
default: an unknown value is not 0, not 1 and not `neutral`.

### Reliability threshold

The threshold is **0.80**, imported as `backend.palette.compatibility.CONFIDENCE_THRESHOLD` rather
than re-declared, and it is the same value the #11 filter and the DSP baseline use. A value just
below 0.80 makes a required fact unusable and the dimension unavailable; a value exactly at or just
above 0.80 is reliable and is presented unchanged. The threshold is never folded into a presented
value.

The contract's optional-confidence measurements are usable with no declared confidence.
**An undeclared confidence is not a reliability claim about the value.**
An undeclared confidence neither lowers nor raises anything. `fundamental` and `tempo` always carry
a confidence when they are known, because the contract requires it. Within tonal, the fundamental
entries are reliability guards: an unknown fundamental is withheld as `unknown` and does not block,
while a known fundamental below 0.80 blocks with `kick_f0_unreliable` or `candidate_f0_unreliable`.
Keys are the only compared tonal evidence and both keys are required.

### Insufficient context is dimension specific

A rhythmic question needs a usable candidate tempo and a usable song tempo. `song is None` gives
`song_context_absent`, an unknown song tempo `song_tempo_unknown`, an unreliable one
`song_tempo_unreliable`, and an unknown or unreliable candidate tempo the matching
`candidate_tempo_unknown` or `candidate_tempo_unreliable` code. A tonal question needs both keys and
never falls back to a fundamental. A texture question needs both sides' spectral centroid and
roll-off. Rhythmic is the only dimension that requires song context: the other five are asked with
`song is None` as well, and the song facts they could have used are withheld as `not_supplied`.
Tempo measurement does not exist yet ([#46](https://github.com/gmphto/tera/issues/46)), so every
tempo in these fixtures is a declared synthetic value; this module consumes a reliable tempo and
never produces one.

## Instructions and prompt version

`PROMPT_VERSION` is `jev-questions-v1`. Every accepted question records it, and a response must echo
it exactly (`prompt_version_mismatch` otherwise). The instruction texts below are owned by this
version: any wording change requires a new `PROMPT_VERSION`.

`frequency`:

> Judge how well the kick and the bass candidate share the frequency spectrum and where they compete for the same band. Answer with exactly one of the five labels very-poor, poor, neutral, good or excellent, then the probability of each of those five labels as five numbers that sum to 1, then your own confidence between 0 and 1. Use only the facts in this payload: do not measure audio or estimate any measurement. Nothing else is requested.

`transient`:

> Judge how well the bass candidate's onset fits inside the kick's transient and decay envelope. Answer with exactly one of the five labels very-poor, poor, neutral, good or excellent, then the probability of each of those five labels as five numbers that sum to 1, then your own confidence between 0 and 1. Use only the facts in this payload: do not measure audio or estimate any measurement. Nothing else is requested.

`tonal`:

> Judge how well the bass candidate's key fits the kick's key and, when supplied, the song's key. Answer with exactly one of the five labels very-poor, poor, neutral, good or excellent, then the probability of each of those five labels as five numbers that sum to 1, then your own confidence between 0 and 1. Use only the facts in this payload: do not measure audio or estimate any measurement. Nothing else is requested.

`rhythmic`:

> Judge how well the bass candidate's tempo fits the song's tempo. Answer with exactly one of the five labels very-poor, poor, neutral, good or excellent, then the probability of each of those five labels as five numbers that sum to 1, then your own confidence between 0 and 1. Use only the facts in this payload: do not measure audio or estimate any measurement. Nothing else is requested.

`texture`:

> Judge how well the bass candidate's spectral brightness and roll-off complement the kick's. Answer with exactly one of the five labels very-poor, poor, neutral, good or excellent, then the probability of each of those five labels as five numbers that sum to 1, then your own confidence between 0 and 1. Use only the facts in this payload: do not measure audio or estimate any measurement. Nothing else is requested.

`arrangement`:

> Judge how well the bass candidate fills the arrangement role left open by the kick, given the declared roles and the optional genre. Answer with exactly one of the five labels very-poor, poor, neutral, good or excellent, then the probability of each of those five labels as five numbers that sum to 1, then your own confidence between 0 and 1. Use only the facts in this payload: do not measure audio or estimate any measurement. Nothing else is requested.

Each instruction asks only for the five-label judgment, its five probabilities and the model's own
confidence, and tells the model to use only the facts in the payload and not to measure or estimate
anything. No instruction asks for a measured value, a measurement estimate, an overall score or a
ranking.

## Question identity, determinism and idempotence

`build_question` is pure and deterministic. Identical inputs return equal `JevQuestion` values, an
equal `question_id` and byte-identical `to_json()` output with sorted keys; the order in which the
facts of a sample were supplied does not matter; repeated calls mutate nothing.

`question_id` is `q-` followed by the SHA-256 hex digest of the canonical JSON of the dimension,
`PROMPT_VERSION` and the question's own sorted facts and withheld entries. It contains no path, file
extension, sample ID, file name, sample metadata or user text, and it is opaque. A change to any
supplied or withheld fact, or to `PROMPT_VERSION`, changes the ID, so two candidates with identical
facts legitimately share an ID and the caller keeps the candidate mapping, because the payload
carries no sample ID. Storing, deduplicating or caching a repeated question is
[#26](https://github.com/gmphto/tera/issues/26).

## Unavailable codes

`QUESTION_UNAVAILABLE_CODES` is the exact frozenset of codes an `UnavailableQuestion` can carry.

| Code | Dimension | Meaning |
| --- | --- | --- |
| `candidate_band_bass_unknown` | `frequency` | Candidate band_bass is null, so the required frequency evidence is incomplete |
| `candidate_band_bass_unreliable` | `frequency` | Candidate band_bass declares a confidence below 0.80, so the unreliable value is withheld and the question is not asked |
| `candidate_band_energy_zero` | `frequency` | All six candidate band ratios are 0, so the candidate side of the frequency evidence carries no energy to compare |
| `candidate_band_high_mid_unknown` | `frequency` | Candidate band_high_mid is null, so the required frequency evidence is incomplete |
| `candidate_band_high_mid_unreliable` | `frequency` | Candidate band_high_mid declares a confidence below 0.80, so the unreliable value is withheld and the question is not asked |
| `candidate_band_high_unknown` | `frequency` | Candidate band_high is null, so the required frequency evidence is incomplete |
| `candidate_band_high_unreliable` | `frequency` | Candidate band_high declares a confidence below 0.80, so the unreliable value is withheld and the question is not asked |
| `candidate_band_low_mid_unknown` | `frequency` | Candidate band_low_mid is null, so the required frequency evidence is incomplete |
| `candidate_band_low_mid_unreliable` | `frequency` | Candidate band_low_mid declares a confidence below 0.80, so the unreliable value is withheld and the question is not asked |
| `candidate_band_mid_unknown` | `frequency` | Candidate band_mid is null, so the required frequency evidence is incomplete |
| `candidate_band_mid_unreliable` | `frequency` | Candidate band_mid declares a confidence below 0.80, so the unreliable value is withheld and the question is not asked |
| `candidate_band_sub_unknown` | `frequency` | Candidate band_sub is null, so the required frequency evidence is incomplete |
| `candidate_band_sub_unreliable` | `frequency` | Candidate band_sub declares a confidence below 0.80, so the unreliable value is withheld and the question is not asked |
| `candidate_f0_unreliable` | `tonal` | The candidate fundamental is known but declares a confidence below 0.80, so the tonal reliability guard blocks the question |
| `candidate_key_unknown` | `tonal` | Candidate key is null, so the required tonal evidence is incomplete |
| `candidate_key_unreliable` | `tonal` | Candidate key declares a confidence below 0.80, so the unreliable value is withheld and the question is not asked |
| `candidate_spectral_centroid_unknown` | `texture` | Candidate spectral centroid is null, so the required texture evidence is incomplete |
| `candidate_spectral_centroid_unreliable` | `texture` | Candidate spectral centroid declares a confidence below 0.80, so the unreliable value is withheld and the question is not asked |
| `candidate_spectral_rolloff_unknown` | `texture` | Candidate spectral roll-off is null, so the required texture evidence is incomplete |
| `candidate_spectral_rolloff_unreliable` | `texture` | Candidate spectral roll-off declares a confidence below 0.80, so the unreliable value is withheld and the question is not asked |
| `candidate_tempo_unknown` | `rhythmic` | Candidate tempo is null, so the required rhythmic evidence is incomplete |
| `candidate_tempo_unreliable` | `rhythmic` | Candidate tempo declares a confidence below 0.80, so the unreliable value is withheld and the question is not asked |
| `candidate_transient_position_unknown` | `transient` | Candidate transient position is null, so the required transient evidence is incomplete |
| `candidate_transient_position_unreliable` | `transient` | Candidate transient position declares a confidence below 0.80, so the unreliable value is withheld and the question is not asked |
| `kick_attack_unknown` | `transient` | Kick attack is null, so the required transient evidence is incomplete |
| `kick_attack_unreliable` | `transient` | Kick attack declares a confidence below 0.80, so the unreliable value is withheld and the question is not asked |
| `kick_band_bass_unknown` | `frequency` | Kick band_bass is null, so the required frequency evidence is incomplete |
| `kick_band_bass_unreliable` | `frequency` | Kick band_bass declares a confidence below 0.80, so the unreliable value is withheld and the question is not asked |
| `kick_band_energy_zero` | `frequency` | All six kick band ratios are 0, so the kick side of the frequency evidence carries no energy to compare |
| `kick_band_high_mid_unknown` | `frequency` | Kick band_high_mid is null, so the required frequency evidence is incomplete |
| `kick_band_high_mid_unreliable` | `frequency` | Kick band_high_mid declares a confidence below 0.80, so the unreliable value is withheld and the question is not asked |
| `kick_band_high_unknown` | `frequency` | Kick band_high is null, so the required frequency evidence is incomplete |
| `kick_band_high_unreliable` | `frequency` | Kick band_high declares a confidence below 0.80, so the unreliable value is withheld and the question is not asked |
| `kick_band_low_mid_unknown` | `frequency` | Kick band_low_mid is null, so the required frequency evidence is incomplete |
| `kick_band_low_mid_unreliable` | `frequency` | Kick band_low_mid declares a confidence below 0.80, so the unreliable value is withheld and the question is not asked |
| `kick_band_mid_unknown` | `frequency` | Kick band_mid is null, so the required frequency evidence is incomplete |
| `kick_band_mid_unreliable` | `frequency` | Kick band_mid declares a confidence below 0.80, so the unreliable value is withheld and the question is not asked |
| `kick_band_sub_unknown` | `frequency` | Kick band_sub is null, so the required frequency evidence is incomplete |
| `kick_band_sub_unreliable` | `frequency` | Kick band_sub declares a confidence below 0.80, so the unreliable value is withheld and the question is not asked |
| `kick_decay_unknown` | `transient` | Kick decay is null, so the required transient evidence is incomplete |
| `kick_decay_unreliable` | `transient` | Kick decay declares a confidence below 0.80, so the unreliable value is withheld and the question is not asked |
| `kick_f0_unreliable` | `tonal` | The kick fundamental is known but declares a confidence below 0.80, so the tonal reliability guard blocks the question |
| `kick_key_unknown` | `tonal` | Kick key is null, so the required tonal evidence is incomplete |
| `kick_key_unreliable` | `tonal` | Kick key declares a confidence below 0.80, so the unreliable value is withheld and the question is not asked |
| `kick_spectral_centroid_unknown` | `texture` | Kick spectral centroid is null, so the required texture evidence is incomplete |
| `kick_spectral_centroid_unreliable` | `texture` | Kick spectral centroid declares a confidence below 0.80, so the unreliable value is withheld and the question is not asked |
| `kick_spectral_rolloff_unknown` | `texture` | Kick spectral roll-off is null, so the required texture evidence is incomplete |
| `kick_spectral_rolloff_unreliable` | `texture` | Kick spectral roll-off declares a confidence below 0.80, so the unreliable value is withheld and the question is not asked |
| `kick_transient_strength_unknown` | `transient` | Kick transient strength is null, so the required transient evidence is incomplete |
| `kick_transient_strength_unreliable` | `transient` | Kick transient strength declares a confidence below 0.80, so the unreliable value is withheld and the question is not asked |
| `song_context_absent` | `rhythmic` | No song context was supplied at all, so a dimension that needs the song tempo is not asked |
| `song_tempo_unknown` | `rhythmic` | Song tempo is null, so the required rhythmic evidence is incomplete |
| `song_tempo_unreliable` | `rhythmic` | Song tempo declares a confidence below 0.80, so the unreliable value is withheld and the question is not asked |

## Response format

`validate_response(question, response)` accepts JSON text or an already-parsed mapping whose object
has exactly these eight fields:

| Field | Meaning |
| --- | --- |
| `question_id` | Must echo the asked question |
| `dimension` | Must be a contract literal and must echo the asked dimension |
| `label` | A contract `Label` literal, or null for an abstention |
| `probabilities` | The five label probabilities for a judgment; null or an empty list for an abstention |
| `confidence` | The model's own confidence in [0, 1], or null for an abstention |
| `model_version` | Nonblank text naming the model; recorded unchanged |
| `prompt_version` | Must echo the asked prompt version |
| `unavailable_reason` | `model_abstained` for an abstention, null for a judgment |

An unknown field is rejected with `unexpected_field` and a missing field with `missing_field`,
mirroring the schema-1 contract's reject-unknown-fields rule. JSON text is parsed with the same
rules the contract uses: not valid JSON, a duplicate key, a non-finite number (`NaN`, `Infinity`),
or a value that is not an object is `invalid_response`.

### Validation order

The checks run in a fixed order so each malformed response has one stable code: the question type,
the response shape (`unexpected_field`, then `missing_field`), the echoes (`question_id_mismatch`,
`unsupported_dimension`, `dimension_mismatch`, `prompt_version_mismatch`), `invalid_version`, then
either the abstention rule (`invalid_abstention`) or `unsupported_label`, a labeled response's
`invalid_abstention`, `invalid_confidence`, the probabilities (`invalid_probabilities`,
`probability_out_of_range`, `probability_sum`, `label_probability_mismatch`).

### Labels, probabilities and confidence

`label` is one of the `backend.contracts.Label` literals exactly. `probabilities` must name each of
the five labels exactly once with a finite number in [0, 1]; the five must sum to 1 within the
contract's absolute tolerance of 1e-6; and `label` must be one of the labels attaining the highest
probability, with ties accepted. The validated probabilities are reordered into the contract `Label`
literal order (`very-poor`, `poor`, `neutral`, `good`, `excellent`) whatever order the response
used, so two responses listing the same five values differently validate to equal judgments.

`confidence` is a finite number in [0, 1] and is a self-report. It is recorded unchanged and is
never derived from, compared with or corrected by the probabilities: a confidence above or below the
chosen label's probability validates identically.

An abstention is a null `label` with `unavailable_reason` exactly `model_abstained`, a null
`confidence` and no probabilities. A labeled response carrying a reason, or an abstention carrying
confidence or probabilities, is rejected with `invalid_abstention`, and no model-authored free text
is ever stored or returned.

## Response error codes

`RESPONSE_ERROR_CODES` is the exact frozenset of codes `JevResponseError` can carry.

| Code | Meaning |
| --- | --- |
| `dimension_mismatch` | The response echoes a different contract dimension than the asked one |
| `invalid_abstention` | A null `label` does not carry exactly the `model_abstained` reason with no confidence and no probabilities, or a labeled response carries a reason |
| `invalid_confidence` | `confidence` is not a finite number in [0, 1] |
| `invalid_probabilities` | `probabilities` is not a five-entry list naming each contract label exactly once, or an entry is not an object with exactly `label` and `probability`, or a probability is not a number (booleans included) |
| `invalid_question` | The value passed as the asked question is not a `JevQuestion` |
| `invalid_response` | The response is not JSON text or a mapping, is not valid JSON, is not a JSON object, repeats a field name, or carries a non-finite JSON number |
| `invalid_version` | `model_version` is blank or is not text |
| `label_probability_mismatch` | `label` is not one of the labels attaining the highest probability; a tie is accepted |
| `missing_field` | The response object is missing one of the documented eight fields |
| `probability_out_of_range` | A probability is not a finite number in [0, 1] |
| `probability_sum` | The five probabilities do not sum to 1 within the contract's absolute tolerance of 1e-6 |
| `prompt_version_mismatch` | The response does not echo the asked `prompt_version` |
| `question_id_mismatch` | The response does not echo the asked `question_id` |
| `question_unavailable` | A response was offered for an `UnavailableQuestion`, so a question this product refused can never receive an accepted judgment |
| `unexpected_field` | The response object contains a field outside the documented eight; an overall `compatibility`, `score`, `rank` or `overall` field is rejected here |
| `unsupported_dimension` | The response `dimension` is outside the six contract literals, for example the plan-era alias `frequencyFit` |
| `unsupported_label` | The response `label` is not one of the five contract `Label` literals, for example `GOOD`, `very_poor` or `rating-good` |

## Derived judgment and versions

`validate_response` returns a `backend.contracts.JevJudgment` built from the accepted values with no
clamping, rounding to fit or substitution, carrying the echoed `model_version` and `prompt_version`.
Only `prompt_version` must match the asked question; any nonblank `model_version` is accepted and
recorded. The returned judgment round-trips through `to_json()` and `from_json()`.
`backend/contracts.py` stays unchanged at schema 1.0 and no field is added.

`invalid_question` rejects a value that is not a `JevQuestion`, and `question_unavailable` rejects
any response for an `UnavailableQuestion`, so a refused question can never receive an accepted
judgment.

## Contradiction rule

A judgment that disagrees with the supplied measured facts is surfaced, never reconciled. A response
may answer `excellent` on a frequency question whose declared band ratios show both sides
concentrating in the low band; the judgment is accepted and returned unchanged. These modules never
substitute, clamp, average, reorder or reject a Jev value for disagreeing with a measured fact, and
never write a Jev value into a DSP field or the reverse.
[#15](https://github.com/gmphto/tera/issues/15) owns any weighting or thresholding that would weigh
a judgment against measured evidence.

## Fixtures

`tests/fixtures/jev/question-cases.json` is an array of named cases, each with `name`, `dimension`,
`kick`, `candidate`, `song` (or null) and `expects`.

* A side object has `sample_id`, `role`, `analysis_version`, `key` (or `{"tonic": null}`) and a `measurements` array of `{"name", "value", "confidence"}` entries. A
  missing measurement entry or a missing `value` means the contract value is null;
  a missing `confidence` means the contract carries no declared confidence. `local_path` and `frame_count` may be declared for the payload-privacy
  case.
* A song object has `tempo` (`value`, `confidence`), `key` and `genre`.
* `expects` declares `outcome` (`question` or `unavailable`); for an unavailable case the `code` and which fact `blocked` it (`[side, name, unknown|unreliable|zero_energy]`); and
  optionally the exact `evidence` and `withheld` lists.
* Every code in `QUESTION_UNAVAILABLE_CODES` is exercised by at least one case.

`tests/fixtures/jev/response-cases.json` is an array of accepted cases with `name`, `question_case`,
either `response` or `response_text`, and `expects` (`outcome` `judgment` or `abstention`, then the
exact label, probabilities, confidence, model version and reason).

`tests/fixtures/jev/malformed-responses.json` is an array of rejected cases with `name`, either
`question_case` or `question_value` (`none`, `mapping` or `text`), either `response`,
`response_text` or `response_value` (`none`, `list` or `number`), and `expects.code`. Every code in
`RESPONSE_ERROR_CODES` is exercised by at least one case.

In both response fixture files the literals `<question_id>`, `<dimension>` and `<prompt_version>`
are replaced by the built question's own values before validation. Every declared value is
synthetic: no WAV file, sample library, real path or producer data is used or represented.

## Out of scope

* The real TypeSafe Jev interface, batching, concurrency, timeouts, retries, cancellation,
  credentials and unavailable-service behaviour:
  [#14](https://github.com/gmphto/tera/issues/14).
* Weighted combination, confidence thresholds, alternatives and the DSP-only fallback:
  [#15](https://github.com/gmphto/tera/issues/15).
* Decision caching, invalidation and de-duplicating a repeated question:
  [#26](https://github.com/gmphto/tera/issues/26).
* Surfacing questions, abstentions and judgments through the API and client:
  [#28](https://github.com/gmphto/tera/issues/28).
* Measuring tempo or rhythm: [#46](https://github.com/gmphto/tera/issues/46); this task consumes
  a reliable tempo and never produces one.
* Measuring stereo width, which a later texture question could use:
  [#44](https://github.com/gmphto/tera/issues/44).
* Full-palette aggregation beyond one kick-to-bass pair:
  [#43](https://github.com/gmphto/tera/issues/43).
* Role policies beyond kick-to-bass: [#40](https://github.com/gmphto/tera/issues/40),
  [#47](https://github.com/gmphto/tera/issues/47).
* A producer-specified intended arrangement role: the plan's input list names an intended role,
  but schema-1 `SongContext` has no field for it, so the arrangement question uses
  the declared kick and candidate roles plus optional genre. Tracked as
  [#63](https://github.com/gmphto/tera/issues/63) until it exists.

## Local-only rule

`questions.py` imports only the standard library, `backend.contracts` and the shared threshold
constant; `decisions.py` imports the standard library, `backend.contracts` and the question types.
Neither opens a file, reads audio, imports the #14 transport or touches the network, and neither
changes the behaviour of any existing module. Payloads and judgments carry no local path, file name,
sample ID or audio.

## Validation

```text
uv run pytest tests/test_jev_questions.py tests/test_jev_decisions.py --basetemp .pytest_cache/jev-focused
uv run pytest --basetemp .pytest_cache/jev-full
```

Focused validation: 213 passed (`tests/test_jev_questions.py` 113 and `tests/test_jev_decisions.py`
100, from 1081 collected tests). The full suite after this work is 1077 passed / 4 failed (1081
collected), against the 864 passed / 4 failed baseline (868 collected). The four failures are
pre-existing and sandbox-only: they spawn a subprocess with captured pipes, which this environment
forbids (`test_batch.py`, `test_evaluation_manifest.py`, `test_evaluation_prepare.py`), and they are
unrelated to this contract.
