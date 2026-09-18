# Kick-to-bass contracts (schema 1.0)

Implements [issue #2](https://github.com/gmphto/tera/issues/2). The public types live in `backend/contracts.py` and use only the Python standard library. Frozen, keyword-only dataclasses validate on construction and when loading JSON. All validation failures inherit from `ContractError`; unsupported schema versions raise `UnsupportedVersionError` specifically.

## Local boundary

These are local analysis/ranking exchange objects. `Sample.audio.local_path` and therefore the complete `RecommendationBatch` JSON contain local file paths. Never send these objects, their JSON, or their complete dictionaries to Jev or a remote service. The adapter in #14 must construct a separate request from only the necessary facts and context. This module performs no I/O, checks no file existence, and makes no network calls. Fixture paths are invented examples, not user library locations.

## Objects and ownership

| Type | Meaning |
| --- | --- |
| `AudioMetadata` | Local absolute path, native rate/channels, frame count and duration. |
| `Measurement` | Named DSP value, explicit unit, optional reliability, or a reason the value is unavailable. |
| `MusicalKey` | Tonic/mode and reliability, or an explicit unknown key. |
| `AudioFeatures` | Every supported measurement exactly once, plus measured key. |
| `Sample` | Stable sample ID, kick/bass/sub-bass role, metadata, features, analysis version, schema version. |
| `SongContext` | Optional tempo, key and genre values; unknown values remain explicitly represented. |
| `PaletteContext` | Stable palette ID, revision, selected kick ID, optional selected bass ID, song context and schema version. |
| `DimensionScore` | Application-computed DSP compatibility for one named dimension, or an unavailable reason. |
| `JevJudgment` | A typed categorical judgment and probabilities for one dimension, with confidence and model/prompt versions, or an explicit abstention. |
| `RankedCandidate` | Candidate ID/analysis version, rank, separate similarity/compatibility/confidence, DSP scores, Jev judgments, reasons and warnings. |
| `RecommendationBatch` | Run ID, palette snapshot, referenced samples, ordered results, ranking version, mode and alternative candidate IDs. |

IDs and version strings are opaque nonblank text. IDs must remain stable across a local round trip. Schema 1.0 supports `kick`, `bass` and `sub-bass`; wider roles require a later contract version. A selected bass and every ranked candidate must reference a supplied bass/sub-bass sample. The selected kick must reference a supplied kick. Duplicate sample/candidate IDs, duplicate dimensions within an evidence source, and mismatched candidate analysis versions are rejected.

The batch's `samples` contains the selected kick, an existing selected bass when present, and the ranked candidate samples. `Sample` and `PaletteContext` can also be exchanged independently. Nested objects inherit their owning envelope's schema; each `Sample`, `PaletteContext`, and `RecommendationBatch` carries `schema_version` explicitly on the wire.

## Numbers and units

Every numeric value must be finite. Booleans and numeric strings are not numbers. Integer fields accept only integers in the JSON-safe range, with a maximum of `2**53 - 1`; floats such as `1.0` are not accepted for integer fields. Bounds below are inclusive unless stated otherwise.

| Field | Unit and valid range |
| --- | --- |
| `AudioMetadata.sample_rate_hz` | Integer Hz, greater than zero. |
| `AudioMetadata.channels` | Integer channel count, 1 or 2. |
| `AudioMetadata.frame_count` | Integer frames per channel, zero or greater. |
| `AudioMetadata.duration_ms` | Milliseconds, zero or greater; equals `frame_count * 1000 / sample_rate_hz` within 0.001 ms absolute tolerance. |
| `PaletteContext.revision` | Integer revision counter, zero or greater; advance when selected items or song context change. |
| `RankedCandidate.rank` | Integer ordinal, at least 1; results must appear in contiguous order 1, 2, ... N. |
| Compatibility (aggregate and DSP dimension) | Unitless [0,1], higher means better compatibility. |
| Retrieval similarity | Unitless [0,1], higher means more similar; normalization belongs to the retrieval algorithm/version. |
| Confidence/reliability (measurement, key, Jev, final result) | Unitless [0,1]. Its calibration belongs to the producer of that evidence; confidence is not compatibility. |
| `LabelProbability.probability` | Unitless [0,1]; exactly one entry for each of five labels, with total 1 within absolute tolerance 0.000001. |

All entries below are required once in `AudioFeatures.measurements`. A required entry can be explicitly unknown. Schema coverage does not assert that every extractor is already implemented; later-stage tempo/width extraction can use `not_supported_by_analysis_version` until available.

| Measurement name | Explicit `unit` | Valid known value |
| --- | --- | --- |
| `fundamental` | `Hz` | Greater than 0, at most native Nyquist; reliability required. |
| `spectral_centroid`, `spectral_rolloff` | `Hz` | [0, native Nyquist]. |
| `rms`, `peak` | `linear` | [0, infinity), where 1 is nominal full-scale amplitude; floating-point audio can exceed 1. |
| `crest_factor` | `ratio` | [1, infinity), linear peak/RMS, not decibels. Silence makes this unknown. |
| `loudness` | `LUFS` | Any finite real value; clips that cannot support a meaningful measurement are unknown. |
| `transient_strength` | `normalized` | [0,1], normalized by the versioned extractor. |
| `transient_position`, `attack`, `decay` | `ms` | [0, sample duration]; transient position is relative to the first decoded frame. |
| `stereo_width` | `normalized` | [0,1], where 0 denotes mono/identical stereo and 1 the extractor's documented maximum. |
| `tempo` | `BPM` | Greater than 0; reliability required. One-shots may be unknown. |
| `band_sub`, `band_bass`, `band_low_mid`, `band_mid`, `band_high_mid`, `band_high` | `ratio` | [0,1], fraction of the analyzer's reference spectral energy. |

`Measurement` validates the name/unit pairing and scalar domain. The enclosing `Sample` additionally checks Nyquist and duration bounds against its audio metadata. `SongContext.tempo` must have measurement name `tempo` and unit `BPM`; it is context, not a claim that the sample has that tempo. User-entered tempo/key can carry reliability 1 to indicate an explicit context choice.

DSP methods, frequency-band boundaries, spectral normalization, width/transient mappings, and reliability thresholds are deliberately left to #3 and #5–#8/#44/#46. Their choices must be documented and included in `analysis_version`. These transport conventions do not approve a dependency or implement an extractor. Cross-measurement physical relationships such as peak/RMS consistency and band-energy sums belong to the extractor's validation, not this transport layer.

## Unknown values and evidence

Use `value: null` plus a nonblank `unavailable_reason`, for example `silent_audio`, `clip_too_short`, `one_shot`, `not_provided`, or `not_supported_by_analysis_version`. Known measurements require `unavailable_reason: null`. Unknown measurements cannot claim confidence. Reliability is required for a known fundamental/tempo estimate and optional for other measurements; null reliability on a deterministic measurement means no statistical confidence is supplied, not missing measured data.

For an unknown key, tonic, mode and confidence are all null and the reason is present. A known key has a canonical sharp-spelled tonic (`C`, `C#`, ..., `B`), `major` or `minor`, confidence, and no unavailable reason. Other modes/ambiguous keys remain unknown with a reason; no pitch estimate is automatically promoted to key.

Unknown genre is null with `genre_unavailable_reason`. Known genre is nonblank free text with no unavailable reason. `SongContext` always has the tempo/key/genre slots even when none of their values are known. Thus optional musical context does not mean silently dropping fields.

Unavailable similarity and dimension compatibility follow the same value/reason rule. `selected_bass_id: null` means no selected bass (a valid state, not missing evidence). Empty results mean no recommendations; empty alternatives mean no alternatives. These states do not need fabricated measurements or judgments.

The dimensions are `frequency`, `transient`, `tonal`, `rhythmic`, `texture`, and `arrangement`. Evidence tuples contain dimensions actually evaluated/requested; an omitted dimension was not evaluated, whereas an evaluated dimension with insufficient evidence has an explicit unavailable/abstention reason. No weighting is implied by presence or omission.

Jev labels are `very-poor`, `poor`, `neutral`, `good`, `excellent`. Known judgments require the label, confidence, all label probabilities, model version and prompt version. Abstentions retain model/prompt provenance but have null label/confidence, no probabilities, and a reason. Label-selection and confidence-calibration policy is owned by #13. The contract does not generate prompts or call Jev.

`dsp-only` forbids Jev judgments; `jev-only` forbids DSP dimension scores but may still carry measured facts. `hybrid` allows both. Product code owns the final compatibility and ordered ranks, so these contracts never derive final rank from confidence or similarity. Empty or abstained evidence must be handled by the ranking policies in #12/#15. Alternatives reference unique IDs already present in the ordered results.

## Kick-to-bass example

Run from the repository root:

```python
from pathlib import Path
from backend.contracts import RecommendationBatch, UnsupportedVersionError

text = Path("tests/fixtures/contracts/hybrid.json").read_text(encoding="utf-8")
batch = RecommendationBatch.from_json(text)
kick, bass = batch.samples
result = batch.results[0]

assert kick.role == "kick" and bass.role == "bass"
assert batch.palette.kick_id == kick.sample_id
assert result.candidate_id == bass.sample_id
assert result.analysis_version == bass.analysis_version
assert batch.palette.revision == 3
assert batch.palette.song.tempo.value is None  # not_provided
assert result.similarity == 0.41
assert result.compatibility == 0.74
assert result.confidence == 0.68
assert result.jev_judgments[0].model_version == "fixture-model-1"
assert RecommendationBatch.from_json(batch.to_json()) == batch

payload = batch.to_dict()
payload["schema_version"] = "2.0"
try:
    RecommendationBatch.from_dict(payload)
except UnsupportedVersionError:
    pass  # consumer must migrate or explicitly reject, never guess
else:
    raise AssertionError("Unsupported version was accepted")
```

The example follows `kick-001` and `bass-001` from metadata and versioned measurements into a revision-3 palette and rank-1 result. Its scores and model names are illustrative test data, not measured quality or a live Jev response. The kick's key and song tempo are unknown; that does not force rejection. The candidate carries separate DSP dimension scores, Jev evidence, similarity, final compatibility, and final confidence.

Additional runnable fixtures:

- `dsp-only.json`: the same local inputs with no Jev evidence/model provenance and explicitly unavailable retrieval similarity.
- `silent-sample.json`: zero RMS/peak, unknown pitch/key/other unavailable measurements with reasons.
- `invalid-cases.json`: mutations of `hybrid.json` with paths, invalid values, and expected errors. Actual NaN/infinity and overflow tokens are also tested directly because standard JSON cannot encode them.

`from_dict` accepts JSON-shaped dictionaries/lists; constructors accept the typed dataclasses/tuples. `to_dict` returns a fresh JSON-shaped tree. `to_json` emits strict JSON. Duplicate JSON keys, unknown fields, missing required fields, invalid units/roles/ranges, and non-finite values are rejected without silent coercion.

## Version changes

Schema version `1.0` is required on the wire and is the only version this consumer accepts. Constructors default to `1.0` for convenience. Nested versioned envelopes are checked too. There is no silent upgrade, version guessing, or partial loading.

- Documentation corrections, implementation fixes that preserve the documented accepted data and meaning, and additional valid fixtures are backwards-compatible without changing the schema version.
- Optional field additions and new enum/measurement names need a new version because this consumer intentionally rejects unknown fields and values. A future consumer may explicitly support both old and new versions, with a tested migration.
- Removing/renaming fields, changing units/ranges/normalization meaning, or changing required relationships requires a breaking schema version and explicit migration.
- Changes to DSP method/configuration use a new `analysis_version`; model/prompt changes use their respective versions; weights, fallback policy and tie-breaking use a new `ranking_version`. These opaque algorithm versions do not change structural schema unless field meaning/shape changes too.
- Every selected item or song-context change advances `PaletteContext.revision`; a consumer must compare that revision before displaying results for a current palette. This module transports the snapshot; stale-response handling belongs to #28/#32.

## Verification

Run `uv run pytest`. Tests exercise valid/unknown/silent/DSP-only round trips, direct construction, malformed JSON, every invalid fixture, finite numeric bounds, evidence provenance, rank/ID/version relationships, and unsupported schema versions. No new dependencies, decoding, scoring algorithm, Jev integration, or persistence are introduced.
