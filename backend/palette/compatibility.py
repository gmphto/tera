"""Pure deterministic admission gates, not musical scoring; see filter-policy.md."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
import math

from backend.contracts import Sample, SongContext


POLICY_VERSION = "candidate-admission-v1"
CONFIDENCE_THRESHOLD = 0.80
TEMPO_RELATIVE_TOLERANCE = 0.05
CORE_RELATIVE_ROUNDOFF = 1e-12


class FilterInputError(ValueError):
    """Malformed inputs or invalid selected kick; inspect code, not message text."""

    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


class Availability(StrEnum):
    AVAILABLE = "available"
    MISSING = "missing"
    UNREADABLE = "unreadable"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class FilterPolicy:
    tempo_lock: bool = False
    exact_key_lock: bool = False

    def __post_init__(self):
        if type(self.tempo_lock) is not bool or type(self.exact_key_lock) is not bool:
            raise FilterInputError("invalid_policy", "Lock settings must be explicit booleans.")


@dataclass(frozen=True)
class ExclusionReason:
    code: str
    facts: tuple[tuple[str, str | float | int | bool | None], ...]


@dataclass(frozen=True)
class ExcludedCandidate:
    sample_id: str
    analysis_version: str
    reasons: tuple[ExclusionReason, ...]


@dataclass(frozen=True)
class FilterResult:
    policy_version: str
    policy: FilterPolicy
    kick_id: str
    kick_analysis_version: str
    eligible_ids: tuple[str, ...]
    excluded: tuple[ExcludedCandidate, ...]
    analysis_versions: tuple[tuple[str, str], ...]


def _reason(code, **facts):
    return ExclusionReason(code, tuple(sorted(facts.items())))


def _measurements(sample):
    return {m.name: m for m in sample.features.measurements}


def _core(sample):
    measures = _measurements(sample)
    peak, rms = measures["peak"], measures["rms"]
    reasons = []
    if sample.audio.frame_count == 0:
        reasons.append(_reason("empty_audio", frame_count=0))
    if peak.value == 0:
        reasons.append(_reason("silent_audio", peak=0))
    if peak.value is None:
        reasons.append(_reason("unusable_analysis", peak=None, unavailable_reason=peak.unavailable_reason))
    if peak.value is not None and rms.value is not None and rms.value > peak.value:
        if peak.value == 0 or not math.isclose(rms.value, peak.value, rel_tol=CORE_RELATIVE_ROUNDOFF, abs_tol=0):
            reasons.append(_reason("inconsistent_analysis", rms=rms.value, peak=peak.value,
                                   relative_roundoff=CORE_RELATIVE_ROUNDOFF))
    return reasons


def _validated(value, kind, code):
    if type(value) is not kind:
        raise FilterInputError(code, f"Expected a validated {kind.__name__}.")
    try:
        kind.from_dict(value.to_dict())
    except (TypeError, ValueError, AttributeError) as error:
        raise FilterInputError(code, f"Invalid {kind.__name__} contract.") from error


def filter_candidates(kick: Sample, candidates: Sequence[Sample], *, policy: FilterPolicy,
                      availability: Mapping[str, Availability], context: SongContext | None = None) -> FilterResult:
    """Return immutable eligibility evidence without touching paths or scoring.

    Availability must be a mapping for only supplied IDs; missing entries mean
    unverified. It is caller evidence, not a freshness guarantee. See docs for
    all-reason ordering and stable input-error codes.
    """
    if type(policy) is not FilterPolicy:
        raise FilterInputError("invalid_policy", "An explicit FilterPolicy is required.")
    policy.__post_init__()
    _validated(kick, Sample, "invalid_kick")
    if not isinstance(candidates, Sequence) or isinstance(candidates, (str, bytes)):
        raise FilterInputError("invalid_candidates", "Candidates must be a sequence of Samples.")
    candidates = tuple(candidates)
    for sample in candidates:
        _validated(sample, Sample, "invalid_candidates")
    ids = [sample.sample_id for sample in candidates]
    if len(ids) != len(set(ids)):
        raise FilterInputError("duplicate_candidate_id", "Candidate IDs must be unique.")
    if context is not None:
        _validated(context, SongContext, "invalid_context")
    if not isinstance(availability, Mapping):
        raise FilterInputError("invalid_availability", "Availability must be a mapping keyed by supplied IDs.")
    allowed = set(ids) | {kick.sample_id}
    if any(type(key) is not str or key not in allowed or type(value) is not Availability
           for key, value in availability.items()):
        raise FilterInputError("invalid_availability", "Unknown IDs or invalid availability states.")
    if kick.role != "kick" or availability.get(kick.sample_id, Availability.UNKNOWN) is not Availability.AVAILABLE or _core(kick):
        raise FilterInputError("invalid_kick", "Selected kick needs kick role, available file, positive frames/peak and consistent core analysis.")
    available_reasons = {Availability.MISSING: "file_missing", Availability.UNREADABLE: "file_unreadable",
                         Availability.UNKNOWN: "availability_unknown"}
    excluded, eligible = [], []
    for sample in sorted(candidates, key=lambda s: s.sample_id):
        reasons = []
        if sample.sample_id == kick.sample_id:
            reasons.append(_reason("selected_kick", selected_id=kick.sample_id))
        elif sample.role not in ("bass", "sub-bass"):
            reasons.append(_reason("wrong_role", role=sample.role))
        state = availability.get(sample.sample_id, Availability.UNKNOWN)
        if state in available_reasons:
            reasons.append(_reason(available_reasons[state], availability=state.value))
        reasons.extend(_core(sample))
        if context is not None:
            tempo, song = _measurements(sample)["tempo"], context.tempo
            if (policy.tempo_lock and tempo.value is not None and song.value is not None
                    and tempo.confidence >= CONFIDENCE_THRESHOLD and song.confidence >= CONFIDENCE_THRESHOLD):
                difference = abs(tempo.value - song.value)
                # Equivalent relative gate, avoiding a potentially overflowing ratio.
                if difference > TEMPO_RELATIVE_TOLERANCE * song.value:
                    reasons.append(_reason("tempo_mismatch", candidate_bpm=tempo.value, song_bpm=song.value,
                        candidate_confidence=tempo.confidence, song_confidence=song.confidence,
                        maximum_relative_difference=TEMPO_RELATIVE_TOLERANCE))
            key, song_key = sample.features.key, context.key
            if (policy.exact_key_lock and key.tonic is not None and song_key.tonic is not None
                    and key.confidence >= CONFIDENCE_THRESHOLD and song_key.confidence >= CONFIDENCE_THRESHOLD
                    and (key.tonic, key.mode) != (song_key.tonic, song_key.mode)):
                reasons.append(_reason("key_mismatch", candidate_tonic=key.tonic, candidate_mode=key.mode,
                    song_tonic=song_key.tonic, song_mode=song_key.mode,
                    candidate_confidence=key.confidence, song_confidence=song_key.confidence))
        if reasons:
            excluded.append(ExcludedCandidate(sample.sample_id, sample.analysis_version, tuple(reasons)))
        else:
            eligible.append(sample.sample_id)
    return FilterResult(POLICY_VERSION, policy, kick.sample_id, kick.analysis_version, tuple(eligible),
                        tuple(excluded), tuple(sorted((s.sample_id, s.analysis_version) for s in candidates)))
