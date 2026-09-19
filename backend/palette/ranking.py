"""Deterministic DSP-only kick-to-bass compatibility baseline; see _docs/dsp-baseline.md.

Pure local arithmetic over validated schema-1 records: no Jev import, no
network access, no audio files and no local paths. The document defines the
three dimensions, their measured inputs and mappings, the versioned weight
table, the reliability threshold, the missing-feature and renormalization
rule, the confidence rule, the tie-break and every stable reason code this
module returns.
"""

from collections.abc import Sequence
from dataclasses import dataclass, replace
import math

from backend.contracts import DimensionScore, Sample
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
    dimensions: tuple[DimensionScore, ...]
    reasons: tuple[str, ...]
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class UnscoredCandidate:
    """Explicit record for a candidate with no available dimension."""

    candidate_id: str
    analysis_version: str
    code: str
    dimensions: tuple[DimensionScore, ...]
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
