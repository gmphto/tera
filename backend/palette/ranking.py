"""Deterministic kick-to-bass ranking; see _docs/dsp-baseline.md and _docs/hybrid-ranking.md.

Pure local arithmetic over validated schema-1 records: no Jev import, no
network access, no audio files and no local paths. `rank_candidates` is the
DSP-only baseline and defines the three dimensions, their measured inputs and
mappings, the versioned weight table, the reliability threshold, the
missing-feature and renormalization rule, the confidence rule, the tie-break
and every stable reason code it returns. `rank_hybrid` combines that baseline
with caller-supplied schema-1 JevJudgment values under a second, five-dimension
weight table, keeps compatibility and coverage confidence separate, records one
entry per contract dimension and falls back to the baseline when no candidate
has usable judgment evidence; _docs/hybrid-ranking.md defines the hybrid weight
table, the source rules, the label map, the uncertainty rule, the alternatives
rule, the fallback and every hybrid code.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
import math
from typing import get_args

from backend.contracts import Dimension, DimensionScore, JevJudgment, LabelProbability, Sample
from backend.palette.compatibility import CONFIDENCE_THRESHOLD


RANKING_VERSION = "dsp-baseline-v1"
DEFAULT_WEIGHT_TABLE_ID = "dsp-baseline-weights-1"
WEIGHT_SUM_TOLERANCE = 1e-9
INSUFFICIENT_EVIDENCE = "insufficient_evidence"
DIMENSIONS = ("frequency", "transient", "tonal")
BAND_NAMES = ("band_sub", "band_bass", "band_low_mid", "band_mid", "band_high_mid", "band_high")
LOW_BAND_NAMES = ("band_sub", "band_bass")
# Indexed by interval class in semitones: 0, 1, 2, 3, 4, 5, 6. Provisional and
# uncalibrated; see the documented ordering in _docs/dsp-baseline.md.
TONAL_INTERVAL_COMPATIBILITY = (1.00, 0.10, 0.30, 0.75, 0.85, 0.95, 0.10)
# Schema-1 tonic literal order, C through B; local constant, no DSP import.
TONIC_PITCH_CLASSES = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")


class RankingInputError(ValueError):
    """Malformed ranking input or invalid policy; inspect code, not message text."""

    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class WeightTable:
    """Named positive dimension weights that must sum to 1 within 1e-9."""

    identifier: str
    frequency: float
    transient: float
    tonal: float

    def __post_init__(self):
        if type(self.identifier) is not str or not self.identifier.strip():
            raise RankingInputError("invalid_policy", "Weight table identifier must be nonblank text.")
        values = (self.frequency, self.transient, self.tonal)
        if any(type(value) not in (int, float) or not math.isfinite(value) or value <= 0 for value in values):
            raise RankingInputError("invalid_policy", "Weights must be finite positive numbers.")
        if abs(sum(values) - 1) > WEIGHT_SUM_TOLERANCE:
            raise RankingInputError("invalid_policy", "Weights must sum to 1 within 1e-9.")

    def weight(self, dimension):
        return getattr(self, dimension)


# The plan's 0.3/0.2/0.2 DSP dimensions, renormalized over the three scored
# dimensions; see the documented weight table in _docs/dsp-baseline.md.
DEFAULT_WEIGHT_TABLE = WeightTable(DEFAULT_WEIGHT_TABLE_ID, 3 / 7, 2 / 7, 2 / 7)


@dataclass(frozen=True)
class RankingPolicy:
    """Explicit ranking policy; the default table is the documented baseline."""

    weight_table: WeightTable = DEFAULT_WEIGHT_TABLE

    def __post_init__(self):
        if type(self.weight_table) is not WeightTable:
            raise RankingInputError("invalid_policy", "An explicit WeightTable is required.")
        self.weight_table.__post_init__()


@dataclass(frozen=True)
class ScoredCandidate:
    """Ordered evidence for a candidate with at least one available dimension."""

    candidate_id: str
    analysis_version: str
    rank: int
    compatibility: float
    confidence: float
    dsp_dimensions: tuple[DimensionScore, ...]
    reasons: tuple[str, ...]
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class UnscoredCandidate:
    """Explicit record for a candidate with no available dimension."""

    candidate_id: str
    analysis_version: str
    code: str
    dsp_dimensions: tuple[DimensionScore, ...]
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class RankingResult:
    """Versioned ranking outcome; ranked records carry ranks 1..n."""

    ranking_version: str
    weight_table_id: str
    kick_id: str
    kick_analysis_version: str
    ranked: tuple[ScoredCandidate, ...]
    unscored: tuple[UnscoredCandidate, ...]


def _shown(value):
    """Readable three-decimal rendering used by every scored reason string."""
    return f"{value:.3f}"


def _exact(value):
    """Shortest round-trip rendering used for rejected, unreliable evidence."""
    return repr(float(value))


def _measurements(sample):
    return {item.name: item for item in sample.features.measurements}


def _value(sample, name):
    return _measurements(sample)[name].value


def _bands(sample):
    measurements = _measurements(sample)
    return {name: measurements[name].value for name in BAND_NAMES}


def _scored(dimension, compatibility, reason):
    return DimensionScore(dimension=dimension, compatibility=compatibility), reason


def _unavailable(dimension, code, reason):
    return DimensionScore(dimension=dimension, compatibility=None, unavailable_reason=code), reason


def _frequency_evidence(kick, candidate):
    """Shared low-band concentration into 1 - shared; see the documented mapping."""
    kick_bands, candidate_bands = _bands(kick), _bands(candidate)
    # #11 admits a candidate whose band ratios are unknown, and #13 withholds
    # them rather than blocking: an incomplete band set is an unavailable
    # dimension here, never a sum over a missing value.
    if any(value is None for value in kick_bands.values()):
        return _unavailable("frequency", "kick_band_unknown",
            "frequency: unavailable (kick_band_unknown); kick band ratios are incomplete")
    if any(value is None for value in candidate_bands.values()):
        return _unavailable("frequency", "candidate_band_unknown",
            "frequency: unavailable (candidate_band_unknown); candidate band ratios are"
            " incomplete")
    kick_total = sum(kick_bands.values())
    if kick_total == 0:
        return _unavailable("frequency", "kick_band_energy_zero",
            "frequency: unavailable (kick_band_energy_zero); kick band ratios sum to 0.000")
    candidate_total = sum(candidate_bands.values())
    if candidate_total == 0:
        return _unavailable("frequency", "candidate_band_energy_zero",
            "frequency: unavailable (candidate_band_energy_zero); candidate band ratios sum to 0.000")
    kick_low = sum(kick_bands[name] for name in LOW_BAND_NAMES)
    candidate_low = sum(candidate_bands[name] for name in LOW_BAND_NAMES)
    kick_share, candidate_share = kick_low / kick_total, candidate_low / candidate_total
    shared = min(kick_share, candidate_share)
    compatibility = 1 - shared
    reason = (f"frequency: kick low band {_shown(kick_low)} of {_shown(kick_total)}"
              f" (band_sub {_shown(kick_bands['band_sub'])}, band_bass {_shown(kick_bands['band_bass'])}),"
              f" share {_shown(kick_share)}; candidate low band {_shown(candidate_low)} of"
              f" {_shown(candidate_total)} (band_sub {_shown(candidate_bands['band_sub'])},"
              f" band_bass {_shown(candidate_bands['band_bass'])}), share {_shown(candidate_share)};"
              f" shared concentration {_shown(shared)}; compatibility {_shown(compatibility)}")
    return _scored("frequency", compatibility, reason)


def _transient_evidence(kick, candidate):
    """Candidate onset against the kick decay window, scaled by kick strength."""
    strength = _value(kick, "transient_strength")
    attack = _value(kick, "attack")
    decay = _value(kick, "decay")
    onset = _value(candidate, "transient_position")
    if strength is None:
        return _unavailable("transient", "kick_transient_strength_unknown",
                            "transient: unavailable (kick_transient_strength_unknown); kick transient strength is unknown")
    if attack is None:
        return _unavailable("transient", "kick_attack_unknown",
                            "transient: unavailable (kick_attack_unknown); kick attack is unknown")
    if decay is None:
        return _unavailable("transient", "kick_decay_unknown",
                            "transient: unavailable (kick_decay_unknown); kick decay is unknown")
    if onset is None:
        return _unavailable("transient", "candidate_transient_position_unknown",
                            "transient: unavailable (candidate_transient_position_unknown);"
                            " candidate transient position is unknown")
    window_end = attack + decay
    if window_end <= 0:
        position, window = 1.0, "0.000"
    else:
        position = min(onset, window_end) / window_end
        window = _shown(window_end)
    compatibility = 1 - (1 - position) * strength
    reason = (f"transient: kick attack {_shown(attack)} ms, kick decay {_shown(decay)} ms,"
              f" window end {window} ms; kick transient strength {_shown(strength)};"
              f" candidate transient position {_shown(onset)} ms; onset ratio {_shown(position)};"
              f" compatibility {_shown(compatibility)}")
    return _scored("transient", compatibility, reason)


def _interval_class(tonic, other):
    first, second = TONIC_PITCH_CLASSES.index(tonic), TONIC_PITCH_CLASSES.index(other)
    distance = abs(first - second) % 12
    return min(distance, 12 - distance)


def _tonal_evidence(kick, candidate):
    """Reliable keys on both sides; a known fundamental must also be reliable."""
    for side, sample in (("kick", kick), ("candidate", candidate)):
        measurements = _measurements(sample)
        key, fundamental = sample.features.key, measurements["fundamental"]
        f0 = "unknown" if fundamental.value is None else f"{_shown(fundamental.value)} Hz confidence {_shown(fundamental.confidence)}"
        if key.tonic is None:
            return _unavailable("tonal", f"{side}_key_unknown",
                f"tonal: unavailable ({side}_key_unknown); {side} key has no tonic;"
                f" {side} fundamental {f0}")
        if key.confidence < CONFIDENCE_THRESHOLD:
            return _unavailable("tonal", f"{side}_key_unreliable",
                f"tonal: unavailable ({side}_key_unreliable); {side} key {key.tonic} {key.mode}"
                f" confidence {_exact(key.confidence)} is below the reliability threshold"
                f" {_shown(CONFIDENCE_THRESHOLD)};"
                f" {side} fundamental {f0}")
        if fundamental.value is not None and fundamental.confidence < CONFIDENCE_THRESHOLD:
            return _unavailable("tonal", f"{side}_f0_unreliable",
                f"tonal: unavailable ({side}_f0_unreliable); {side} fundamental"
                f" {_shown(fundamental.value)} Hz confidence {_exact(fundamental.confidence)} is below"
                f" the reliability threshold {_shown(CONFIDENCE_THRESHOLD)}; {side} key {key.tonic}"
                f" {key.mode}"
                f" confidence {_shown(key.confidence)}")
    kick_key, candidate_key = kick.features.key, candidate.features.key
    interval = _interval_class(kick_key.tonic, candidate_key.tonic)
    compatibility = TONAL_INTERVAL_COMPATIBILITY[interval]
    reason = (f"tonal: kick key {kick_key.tonic} {kick_key.mode} confidence {_shown(kick_key.confidence)},"
              f" kick fundamental {_fundamental_text(kick)}; candidate key {candidate_key.tonic}"
              f" {candidate_key.mode} confidence {_shown(candidate_key.confidence)}, candidate fundamental"
              f" {_fundamental_text(candidate)}; tonic interval class {interval} semitones;"
              f" compatibility {_shown(compatibility)}")
    return _scored("tonal", compatibility, reason)


def _fundamental_text(sample):
    fundamental = _measurements(sample)["fundamental"]
    if fundamental.value is None:
        return "unknown"
    return f"{_shown(fundamental.value)} Hz confidence {_shown(fundamental.confidence)}"


def _validated(value, kind, code):
    if type(value) is not kind:
        raise RankingInputError(code, f"Expected a validated {kind.__name__}.")
    try:
        kind.from_dict(value.to_dict())
    except (TypeError, ValueError, AttributeError) as error:
        raise RankingInputError(code, f"Invalid {kind.__name__} contract.") from error


def _evidence(kick, candidate):
    """Three dimension entries in fixed order with one reason string each."""
    plan = (_frequency_evidence(kick, candidate), _transient_evidence(kick, candidate),
            _tonal_evidence(kick, candidate))
    dimensions = tuple(entry[0] for entry in plan)
    return dimensions, tuple(entry[1] for entry in plan)


def rank_candidates(kick: Sample, candidates: Sequence[Sample], *, policy: RankingPolicy) -> RankingResult:
    """Rank #11-eligible bass candidates against one selected kick, locally.

    Inputs are a validated schema-1 kick Sample, a sequence of already-eligible
    bass/sub-bass Samples, and an explicit RankingPolicy. Malformed contracts,
    a non-kick selected Sample, a wrong candidate role, duplicate candidate IDs
    and a candidate sharing the kick ID raise RankingInputError with a stable
    code instead of being dropped or ranked. Song context is not used.

    The result records RANKING_VERSION, the policy weight-table identifier and
    the ordered ranked records plus explicit unscored records. Ranked records
    sort by compatibility descending, then candidate ID ascending; unscored
    records sort by candidate ID ascending. Inputs are never mutated and the
    output depends only on the supplied values. See _docs/dsp-baseline.md.
    """
    if type(policy) is not RankingPolicy:
        raise RankingInputError("invalid_policy", "An explicit RankingPolicy is required.")
    policy.__post_init__()
    _validated(kick, Sample, "invalid_kick")
    if kick.role != "kick":
        raise RankingInputError("invalid_kick", f"Selected kick role is {kick.role!r}, not 'kick'.")
    if not isinstance(candidates, Sequence) or isinstance(candidates, (str, bytes)):
        raise RankingInputError("invalid_candidates", "Candidates must be a sequence of Samples.")
    candidates = tuple(candidates)
    for sample in candidates:
        _validated(sample, Sample, "invalid_candidates")
        if sample.role not in ("bass", "sub-bass"):
            raise RankingInputError("invalid_candidates",
                f"Candidate {sample.sample_id!r} role is {sample.role!r}, not bass/sub-bass.")
    identifiers = [sample.sample_id for sample in candidates]
    if len(identifiers) != len(set(identifiers)):
        raise RankingInputError("duplicate_candidate_id", "Candidate IDs must be unique.")
    if kick.sample_id in identifiers:
        raise RankingInputError("selected_kick_in_candidates",
            "A candidate cannot share the selected kick ID.")
    weight_table = policy.weight_table
    scored, unscored = [], []
    for sample in candidates:
        dimensions, reasons = _evidence(kick, sample)
        available = [entry for entry in dimensions if entry.compatibility is not None]
        warnings = ()
        if sample.analysis_version != kick.analysis_version:
            warnings = (f"Candidate analysis version {sample.analysis_version!r} differs from kick"
                        f" analysis version {kick.analysis_version!r}; values came from different"
                        " analysis versions.",)
        if not available:
            unscored.append(UnscoredCandidate(sample.sample_id, sample.analysis_version,
                                              INSUFFICIENT_EVIDENCE, dimensions, reasons))
            continue
        covered = sum(weight_table.weight(entry.dimension) for entry in available)
        weighted = sum(weight_table.weight(entry.dimension) * entry.compatibility for entry in available)
        scored.append(ScoredCandidate(sample.sample_id, sample.analysis_version, 0, weighted / covered,
                                      covered, dimensions, reasons, warnings))
    ordered = sorted(scored, key=lambda record: (-record.compatibility, record.candidate_id))
    ranked = tuple(replace(record, rank=rank) for rank, record in enumerate(ordered, 1))
    return RankingResult(RANKING_VERSION, weight_table.identifier, kick.sample_id,
                         kick.analysis_version, ranked,
                         tuple(sorted(unscored, key=lambda record: record.candidate_id)))


# ---------------------------------------------------------------------------
# Hybrid DSP + Jev ranking; see _docs/hybrid-ranking.md.
#
# Everything below is pure local arithmetic over the #12 baseline above and
# schema-1 JevJudgment values the caller supplies. It imports the standard
# library, the contract module and the compatibility threshold only: no Jev
# transport, no file, no audio, no network, no credential, no clock and no
# randomness. The combination is provable without the #14 adapter, and no
# judgment is ever invented, defaulted or reconciled with a measured fact.
# ---------------------------------------------------------------------------

HYBRID_RANKING_VERSION = "hybrid-ranking-v1"
HYBRID_WEIGHT_TABLE_ID = "hybrid-weights-1"

# The product plan's five-dimension example table, _docs/plan.md section 6.
HYBRID_WEIGHTS = {"frequency": 0.30, "transient": 0.20, "tonal": 0.20,
                  "texture": 0.10, "arrangement": 0.20}

# Contract dimensions with no weight in this table. A supplied judgment for one
# is recorded as unweighted, never scored and never discarded.
UNWEIGHTED_DIMENSIONS = ("rhythmic",)

# Equal source shares when both a DSP and a Jev value exist for one dimension.
SOURCE_SHARE = 0.5

# The five schema-1 labels and the score each maps to under this version.
JEV_LABEL_SCORES = {"very-poor": 0.0, "poor": 0.25, "neutral": 0.5,
                    "good": 0.75, "excellent": 1.0}

# Coverage below which a recommendation is uncertain.
LOW_CONFIDENCE_THRESHOLD = 0.60

# The #11/#12 reliability threshold, imported rather than re-declared.
JEV_CONFIDENCE_THRESHOLD = CONFIDENCE_THRESHOLD

# Absolute source disagreement above which a dimension is reported as one.
DISAGREEMENT_DELTA = 0.50

# The most alternative candidate IDs one result carries.
ALTERNATIVES_LIMIT = 3

# The six contract dimensions in the contract literal's own order, and the five
# dimensions hybrid-weights-1 weights.
HYBRID_DIMENSIONS = get_args(Dimension)
HYBRID_WEIGHTED_DIMENSIONS = ("frequency", "transient", "tonal", "texture", "arrangement")

# Every stable hybrid code: dimension entry reasons, provenance markers and the
# leading code of every hybrid warning string.
NO_EVIDENCE = "no_evidence"
UNWEIGHTED_DIMENSION = "unweighted_dimension"
JEV_ABSTAINED = "jev_abstained"
JEV_EVIDENCE_UNAVAILABLE = "jev_evidence_unavailable"
DIMENSION_DISAGREEMENT = "dimension_disagreement"
CANDIDATE_NO_JEV_EVIDENCE = "candidate_no_jev_evidence"
LOW_COVERAGE = "low_coverage"
LOW_JEV_CONFIDENCE = "low_jev_confidence"
HYBRID_CODES = (NO_EVIDENCE, UNWEIGHTED_DIMENSION, JEV_ABSTAINED, JEV_EVIDENCE_UNAVAILABLE,
                DIMENSION_DISAGREEMENT, CANDIDATE_NO_JEV_EVIDENCE, LOW_COVERAGE,
                LOW_JEV_CONFIDENCE)

# The mode and status literals a hybrid result records; both modes are the
# schema-1 RecommendationBatch mode literals.
MODE_HYBRID = "hybrid"
MODE_DSP_ONLY = "dsp-only"
JEV_PRESENT = "jev_present"
JEV_PARTIAL = "jev_partial"
JEV_ABSENT = "jev_absent"
HYBRID_STATUS_CODES = (MODE_HYBRID, MODE_DSP_ONLY, JEV_PRESENT, JEV_PARTIAL, JEV_ABSENT)

HYBRID_INPUT_ERROR_CODES = ("invalid_policy", "invalid_baseline", "invalid_evidence",
                            "duplicate_evidence", "unknown_candidate")


@dataclass(frozen=True)
class HybridWeightTable:
    """Named positive weights for the five weighted dimensions, summing to 1."""

    identifier: str
    frequency: float
    transient: float
    tonal: float
    texture: float
    arrangement: float

    def __post_init__(self):
        if type(self.identifier) is not str or not self.identifier.strip():
            raise RankingInputError("invalid_policy", "Weight table identifier must be nonblank text.")
        values = (self.frequency, self.transient, self.tonal, self.texture, self.arrangement)
        if any(type(value) not in (int, float) or not math.isfinite(value) or value <= 0
               for value in values):
            raise RankingInputError("invalid_policy", "Weights must be finite positive numbers.")
        if abs(sum(values) - 1) > WEIGHT_SUM_TOLERANCE:
            raise RankingInputError("invalid_policy", "Weights must sum to 1 within 1e-9.")

    def weight(self, dimension):
        """This table's weight for a contract dimension; 0.0 when it is unweighted."""
        if dimension not in HYBRID_WEIGHTED_DIMENSIONS:
            return 0.0
        return getattr(self, dimension)


DEFAULT_HYBRID_WEIGHT_TABLE = HybridWeightTable(HYBRID_WEIGHT_TABLE_ID, **HYBRID_WEIGHTS)


@dataclass(frozen=True)
class HybridPolicy:
    """Explicit hybrid policy; the default table is the documented five-dimension one."""

    weight_table: HybridWeightTable = DEFAULT_HYBRID_WEIGHT_TABLE

    def __post_init__(self):
        if type(self.weight_table) is not HybridWeightTable:
            raise RankingInputError("invalid_policy", "An explicit HybridWeightTable is required.")
        self.weight_table.__post_init__()


@dataclass(frozen=True)
class HybridEvidence:
    """One dimension's supplied evidence: exactly one of a judgment or an upstream reason.

    A labeled judgment is usable evidence; a judgment with a null label is the
    documented abstention, and an unavailable reason is a #13 question code or a
    #14 outcome code copied verbatim. The two are never both set and never both
    absent; rank_hybrid rejects either shape instead of fabricating a judgment.
    """

    dimension: str
    judgment: JevJudgment | None = None
    unavailable_reason: str | None = None


@dataclass(frozen=True)
class HybridDimensionScore:
    """One contract dimension: both sources kept separate, combination and reason explicit."""

    dimension: str
    weight: float
    dsp_compatibility: float | None
    dsp_unavailable_reason: str | None
    jev_label: str | None
    jev_score: float | None
    jev_confidence: float | None
    jev_probabilities: tuple[LabelProbability, ...]
    jev_model_version: str | None
    jev_prompt_version: str | None
    jev_unavailable_reason: str | None
    compatibility: float | None
    unavailable_reason: str | None
    reason: str


@dataclass(frozen=True)
class HybridCandidate:
    """A ranked candidate: the baseline record plus the combined evidence."""

    candidate_id: str
    analysis_version: str
    rank: int
    compatibility: float
    confidence: float
    uncertain: bool
    dsp_dimensions: tuple[DimensionScore, ...]
    hybrid_dimensions: tuple[HybridDimensionScore, ...]
    jev_judgments: tuple[JevJudgment, ...]
    reasons: tuple[str, ...]
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class HybridUnscored:
    """A candidate with no available weighted dimension, with the same evidence."""

    candidate_id: str
    analysis_version: str
    code: str
    dsp_dimensions: tuple[DimensionScore, ...]
    hybrid_dimensions: tuple[HybridDimensionScore, ...]
    reasons: tuple[str, ...]
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class HybridResult:
    """Versioned hybrid outcome; ranked records carry ranks 1..n and the alternatives."""

    ranking_version: str
    weight_table_id: str
    mode: str
    jev_status: str
    kick_id: str
    kick_analysis_version: str
    ranked: tuple[HybridCandidate, ...]
    unscored: tuple[HybridUnscored, ...]
    alternatives: tuple[str, ...]


def _hybrid_reason(dimension, weight, dsp_compatibility, dsp_unavailable, jev_label, jev_score,
                   jev_confidence, jev_unavailable, compatibility, unavailable_reason):
    """The one human-readable reason string for a hybrid dimension entry."""
    if dsp_compatibility is None:
        dsp_text = "dsp unavailable" + (f" ({dsp_unavailable})" if dsp_unavailable else "")
    else:
        dsp_text = f"dsp {_shown(dsp_compatibility)}"
    if jev_label is not None:
        score_text = "unweighted" if jev_score is None else _shown(jev_score)
        jev_text = f"jev {jev_label} {score_text} confidence {_shown(jev_confidence)}"
        if jev_unavailable is not None:
            jev_text += f" ({jev_unavailable})"
    elif jev_unavailable is not None:
        jev_text = f"jev unavailable ({jev_unavailable})"
    else:
        jev_text = "jev absent"
    if compatibility is None:
        combined_text = f"combined unavailable ({unavailable_reason}) at weight {_shown(weight)}"
    else:
        combined_text = f"combined {_shown(compatibility)} at weight {_shown(weight)}"
    return f"{dimension}: {dsp_text}; {jev_text}; {combined_text}"


def _hybrid_dimension(dimension, weight, dsp_entry, evidence):
    """One dimension entry and its warnings: sources kept separate, never reconciled."""
    dsp_compatibility = None if dsp_entry is None else dsp_entry.compatibility
    dsp_unavailable = None if dsp_entry is None else dsp_entry.unavailable_reason
    judgment = None if evidence is None else evidence.judgment
    jev_unavailable = None if evidence is None else evidence.unavailable_reason
    jev_label = jev_confidence = None
    jev_probabilities = ()
    jev_model_version = jev_prompt_version = None
    jev_score = None
    if judgment is not None:
        jev_model_version, jev_prompt_version = judgment.model_version, judgment.prompt_version
        if judgment.label is None:
            jev_unavailable = JEV_ABSTAINED
        else:
            jev_label, jev_confidence = judgment.label, judgment.confidence
            jev_probabilities = judgment.probabilities
            if weight > 0:
                jev_score = JEV_LABEL_SCORES[judgment.label]
    warnings = []
    if weight <= 0:
        compatibility = None
        unavailable_reason = NO_EVIDENCE if evidence is None else UNWEIGHTED_DIMENSION
    elif dsp_compatibility is not None and jev_score is not None:
        compatibility, unavailable_reason = (dsp_compatibility + jev_score) / 2, None
        difference = abs(dsp_compatibility - jev_score)
        if difference > DISAGREEMENT_DELTA:
            warnings.append(
                f"{DIMENSION_DISAGREEMENT}: {dimension} dsp {_shown(dsp_compatibility)} and jev"
                f" {_shown(jev_score)} differ by {_shown(difference)}, more than the disagreement"
                f" delta {_shown(DISAGREEMENT_DELTA)}; both values are kept unchanged and combined"
                " as their mean")
    elif dsp_compatibility is not None:
        compatibility, unavailable_reason = dsp_compatibility, None
    elif jev_score is not None:
        compatibility, unavailable_reason = jev_score, None
    else:
        compatibility, unavailable_reason = None, NO_EVIDENCE
    if weight > 0 and jev_unavailable is not None and jev_unavailable != JEV_ABSTAINED:
        if dsp_compatibility is not None:
            warnings.append(
                f"{JEV_EVIDENCE_UNAVAILABLE}: {dimension} carried the upstream code"
                f" {jev_unavailable!r} with no usable judgment; the dimension was scored from the"
                " DSP baseline score alone")
        else:
            warnings.append(
                f"{JEV_EVIDENCE_UNAVAILABLE}: {dimension} carried the upstream code"
                f" {jev_unavailable!r} with no usable judgment and no DSP baseline score; the"
                " dimension is no_evidence")
    entry = HybridDimensionScore(
        dimension=dimension, weight=weight, dsp_compatibility=dsp_compatibility,
        dsp_unavailable_reason=dsp_unavailable, jev_label=jev_label, jev_score=jev_score,
        jev_confidence=jev_confidence, jev_probabilities=jev_probabilities,
        jev_model_version=jev_model_version, jev_prompt_version=jev_prompt_version,
        jev_unavailable_reason=jev_unavailable, compatibility=compatibility,
        unavailable_reason=unavailable_reason,
        reason=_hybrid_reason(dimension, weight, dsp_compatibility, dsp_unavailable, jev_label,
                              jev_score, jev_confidence, jev_unavailable, compatibility,
                              unavailable_reason))
    return entry, warnings


def _hybrid_scoring(record, evidence_entries, weight_table):
    """Six dimension entries, covered weight, weighted sum, warnings and flags."""
    dsp_by_dimension = {entry.dimension: entry for entry in record.dsp_dimensions}
    evidence_by_dimension = {entry.dimension: entry for entry in evidence_entries}
    dimensions, warnings, judgments = [], [], []
    covered = weighted = 0.0
    contributing = []
    for dimension in HYBRID_DIMENSIONS:
        weight = weight_table.weight(dimension)
        evidence = evidence_by_dimension.get(dimension)
        entry, dimension_warnings = _hybrid_dimension(dimension, weight, dsp_by_dimension.get(dimension),
                                                      evidence)
        dimensions.append(entry)
        warnings.extend(dimension_warnings)
        if weight > 0 and entry.jev_label is not None:
            judgments.append(evidence.judgment)
        if entry.compatibility is not None:
            covered += weight
            weighted += weight * entry.compatibility
            if entry.jev_score is not None:
                contributing.append(entry.jev_confidence)
    usable = any(entry.jev_score is not None for entry in dimensions if entry.weight > 0)
    return (tuple(dimensions), covered, weighted, tuple(warnings), tuple(contributing), usable,
            tuple(judgments))


def _validated_dimensions(entries):
    """The baseline record's own dimension entries, revalidated and unique."""
    if type(entries) is not tuple:
        raise RankingInputError("invalid_baseline", "Baseline dsp_dimensions must be a tuple.")
    names = []
    for entry in entries:
        _validated(entry, DimensionScore, "invalid_baseline")
        if entry.dimension not in DIMENSIONS:
            raise RankingInputError("invalid_baseline",
                                    f"Unknown baseline dimension: {entry.dimension!r}.")
        names.append(entry.dimension)
    if len(names) != len(set(names)):
        raise RankingInputError("invalid_baseline", "Baseline dsp_dimensions must be unique.")
    return entries


def _validated_baseline_text(record, names):
    for name in names:
        value = getattr(record, name)
        if type(value) is not str or not value.strip():
            raise RankingInputError("invalid_baseline", f"A baseline record {name} must be nonblank text.")
    for name in ("reasons", "warnings"):
        if not hasattr(record, name):
            continue
        value = getattr(record, name)
        if type(value) is not tuple or any(type(item) is not str or not item.strip() for item in value):
            raise RankingInputError("invalid_baseline", f"A baseline record {name} must be a tuple of text.")


def _baseline_records(baseline):
    """The baseline's candidate records in their own order, or an invalid_baseline error."""
    if type(baseline) is not RankingResult:
        raise RankingInputError("invalid_baseline", "An explicit RankingResult is required.")
    _validated_baseline_text(baseline, ("ranking_version", "weight_table_id", "kick_id",
                                        "kick_analysis_version"))
    if type(baseline.ranked) is not tuple or type(baseline.unscored) is not tuple:
        raise RankingInputError("invalid_baseline", "Baseline ranked and unscored must be tuples.")
    records = {}
    for position, record in enumerate(baseline.ranked, 1):
        if type(record) is not ScoredCandidate or record.rank != position:
            raise RankingInputError("invalid_baseline",
                                    "Baseline ranked records need type ScoredCandidate and ranks 1..n in order.")
        _validated_baseline_text(record, ("candidate_id", "analysis_version"))
        for name, value in (("compatibility", record.compatibility), ("confidence", record.confidence)):
            if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1:
                raise RankingInputError("invalid_baseline",
                                        f"Baseline {name} must be a finite number in [0, 1].")
        _validated_dimensions(record.dsp_dimensions)
        if record.candidate_id in records:
            raise RankingInputError("invalid_baseline", "Baseline candidate IDs must be unique.")
        records[record.candidate_id] = record
    for record in baseline.unscored:
        if type(record) is not UnscoredCandidate:
            raise RankingInputError("invalid_baseline", "Baseline unscored records need type UnscoredCandidate.")
        _validated_baseline_text(record, ("candidate_id", "analysis_version", "code"))
        _validated_dimensions(record.dsp_dimensions)
        if record.candidate_id in records:
            raise RankingInputError("invalid_baseline", "Baseline candidate IDs must be unique.")
        records[record.candidate_id] = record
    return records


def _validated_evidence(evidence, records):
    """Validated evidence entries per candidate ID, in the baseline's own candidate order."""
    if not isinstance(evidence, Mapping):
        raise RankingInputError("invalid_evidence", "Evidence must be a mapping keyed by candidate ID.")
    for key in evidence:
        if type(key) is not str or not key.strip():
            raise RankingInputError("invalid_evidence",
                                    "Every evidence key must be a nonblank candidate ID.")
    unknown = sorted(key for key in evidence if key not in records)
    if unknown:
        raise RankingInputError("unknown_candidate",
                                f"Evidence names candidates the baseline does not hold: {unknown}.")
    checked = {}
    for candidate_id in records:
        if candidate_id not in evidence:
            checked[candidate_id] = ()
            continue
        entries = evidence[candidate_id]
        if not isinstance(entries, Sequence) or isinstance(entries, (str, bytes)):
            raise RankingInputError("invalid_evidence",
                                    "A candidate's evidence must be a tuple of HybridEvidence.")
        names = []
        for entry in entries:
            if type(entry) is not HybridEvidence:
                raise RankingInputError("invalid_evidence", "Every evidence entry must be a HybridEvidence.")
            if entry.dimension not in HYBRID_DIMENSIONS:
                raise RankingInputError("invalid_evidence",
                                        f"Unknown evidence dimension: {entry.dimension!r}.")
            if (entry.judgment is None) == (entry.unavailable_reason is None):
                raise RankingInputError("invalid_evidence",
                                        "An evidence entry carries exactly one of a judgment and an"
                                        " unavailable reason.")
            if entry.unavailable_reason is not None and (type(entry.unavailable_reason) is not str
                                                         or not entry.unavailable_reason.strip()):
                raise RankingInputError("invalid_evidence",
                                        "An unavailable reason must be nonblank text.")
            if entry.judgment is not None:
                _validated(entry.judgment, JevJudgment, "invalid_evidence")
                if entry.judgment.dimension != entry.dimension:
                    raise RankingInputError("invalid_evidence",
                                            "A judgment must carry the dimension of its evidence entry.")
            if entry.dimension in names:
                raise RankingInputError("duplicate_evidence",
                                        f"Two evidence entries for candidate {candidate_id!r} and"
                                        f" dimension {entry.dimension!r}.")
            names.append(entry.dimension)
        checked[candidate_id] = tuple(entries)
    return checked


def _candidate_warnings(record, dimensions, confidence, contributing, dimension_warnings):
    """The hybrid record's warnings: the baseline's own, then the documented codes."""
    warnings = list(record.warnings) if hasattr(record, "warnings") else []
    warnings.extend(dimension_warnings)
    if not any(entry.jev_score is not None for entry in dimensions if entry.weight > 0):
        warnings.append(
            f"{CANDIDATE_NO_JEV_EVIDENCE}: no usable judgment in any weighted dimension; the record"
            f" uses the DSP baseline dimensions alone with confidence {_shown(confidence)}")
    if confidence < LOW_CONFIDENCE_THRESHOLD:
        warnings.append(f"{LOW_COVERAGE}: confidence {_shown(confidence)} is below the low-confidence"
                        f" threshold {_shown(LOW_CONFIDENCE_THRESHOLD)}")
    for entry in dimensions:
        if entry.jev_score is not None and entry.jev_confidence < JEV_CONFIDENCE_THRESHOLD:
            warnings.append(
                f"{LOW_JEV_CONFIDENCE}: {entry.dimension} judgment confidence"
                f" {_shown(entry.jev_confidence)} is below the Jev confidence threshold"
                f" {_shown(JEV_CONFIDENCE_THRESHOLD)}")
    return tuple(warnings)


def _hybrid_alternatives(ranked):
    """Up to ALTERNATIVES_LIMIT ranked IDs after rank 1, or nothing when all are certain."""
    if not any(record.uncertain for record in ranked):
        return ()
    return tuple(record.candidate_id for record in ranked[1:1 + ALTERNATIVES_LIMIT])


def rank_hybrid(baseline: RankingResult, evidence: Mapping[str, Sequence[HybridEvidence]], *,
                policy: HybridPolicy) -> HybridResult:
    """Combine one #12 RankingResult with caller-supplied Jev evidence, locally.

    Inputs are a validated #12 RankingResult, a mapping of candidate ID to that
    candidate's HybridEvidence entries, and an explicit HybridPolicy. Every
    candidate keeps one entry per contract dimension: the DSP baseline score,
    the Jev judgment and the combined score are recorded separately, and a
    dimension is scored from whichever sources exist. Compatibility is the
    weighted mean over the covered dimensions using the policy's table;
    confidence is that same coverage and is never mixed with compatibility.
    Ranked records sort by compatibility descending then candidate ID ascending,
    unscored records by candidate ID ascending.

    A malformed policy, baseline or evidence raises RankingInputError with a
    stable code, and nothing is dropped, defaulted or silently ranked. When no
    candidate has a usable judgment in any weighted dimension the result is the
    documented dsp-only fallback: mode "dsp-only", jev_status "jev_absent" and
    records that keep the baseline's own rank order, compatibility, confidence,
    dsp_dimensions, reasons and warnings, so the fallback cannot drift from
    rank_candidates. Inputs are never mutated and the output depends only on the
    supplied values: no clock, randomness, locale, hash iteration or process
    state participates. See _docs/hybrid-ranking.md.
    """
    if type(policy) is not HybridPolicy:
        raise RankingInputError("invalid_policy", "An explicit HybridPolicy is required.")
    policy.__post_init__()
    records = _baseline_records(baseline)
    checked = _validated_evidence(evidence, records)
    weight_table = policy.weight_table
    prepared = {}
    for candidate_id, record in records.items():
        prepared[candidate_id] = (record, *_hybrid_scoring(record, checked[candidate_id],
                                                           weight_table))
    any_usable = any(item[6] for item in prepared.values())
    if not any_usable:
        ranked = tuple(HybridCandidate(
            candidate_id=record.candidate_id, analysis_version=record.analysis_version,
            rank=record.rank, compatibility=record.compatibility, confidence=record.confidence,
            uncertain=record.confidence < LOW_CONFIDENCE_THRESHOLD,
            dsp_dimensions=record.dsp_dimensions, hybrid_dimensions=prepared[record.candidate_id][1],
            jev_judgments=(), reasons=record.reasons, warnings=record.warnings)
            for record in baseline.ranked)
        unscored = tuple(HybridUnscored(
            candidate_id=record.candidate_id, analysis_version=record.analysis_version,
            code=record.code, dsp_dimensions=record.dsp_dimensions,
            hybrid_dimensions=prepared[record.candidate_id][1], reasons=record.reasons, warnings=())
            for record in baseline.unscored)
        return HybridResult(RANKING_VERSION, baseline.weight_table_id, MODE_DSP_ONLY, JEV_ABSENT,
                            baseline.kick_id, baseline.kick_analysis_version, ranked, unscored,
                            _hybrid_alternatives(ranked))
    scored_items = [item for item in prepared.values() if item[2] > 0]
    jev_status = JEV_PRESENT if all(
        all(entry.jev_score is not None for entry in item[1] if entry.weight > 0)
        for item in scored_items) else JEV_PARTIAL
    scored, unscored = [], []
    for record, dimensions, covered, weighted, dimension_warnings, contributing, usable, judgments \
            in prepared.values():
        warnings = _candidate_warnings(record, dimensions, covered, contributing, dimension_warnings)
        if covered == 0:
            code = record.code if type(record) is UnscoredCandidate else INSUFFICIENT_EVIDENCE
            unscored.append(HybridUnscored(
                candidate_id=record.candidate_id, analysis_version=record.analysis_version, code=code,
                dsp_dimensions=record.dsp_dimensions, hybrid_dimensions=dimensions,
                reasons=record.reasons, warnings=warnings))
            continue
        scored.append(HybridCandidate(
            candidate_id=record.candidate_id, analysis_version=record.analysis_version, rank=0,
            compatibility=weighted / covered, confidence=covered,
            uncertain=(covered < LOW_CONFIDENCE_THRESHOLD
                       or any(value < JEV_CONFIDENCE_THRESHOLD for value in contributing)),
            dsp_dimensions=record.dsp_dimensions, hybrid_dimensions=dimensions,
            jev_judgments=judgments, reasons=record.reasons, warnings=warnings))
    ordered = sorted(scored, key=lambda record: (-record.compatibility, record.candidate_id))
    ranked = tuple(replace(record, rank=rank) for rank, record in enumerate(ordered, 1))
    return HybridResult(HYBRID_RANKING_VERSION, weight_table.identifier, MODE_HYBRID, jev_status,
                        baseline.kick_id, baseline.kick_analysis_version, ranked,
                        tuple(sorted(unscored, key=lambda record: record.candidate_id)),
                        _hybrid_alternatives(ranked))
