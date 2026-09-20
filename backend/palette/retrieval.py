"""Normalized kick-to-bass feature retrieval (issue #25).

One declared, versioned similarity space over the measurements issue #21 stores,
built as a pure function of contract `Sample` objects. Retrieval orders the
candidates issue #11's admission already accepted and cuts that order to a
configured 50-100 candidate shortlist for the Jev stage; it is a plausibility
order with its own version identifiers and is never a compatibility score, a
confidence or a recommendation.

Design rules:

- The representation is `RETRIEVAL_DIMENSIONS` in that order: fourteen stored
  `backend.contracts.MEASURES` names, transformed as `RETRIEVAL_TRANSFORMS`
  declares and then normalized. No new vocabulary and no renamed measurement.
- A dimension is known for one sample only when the stored value is not None,
  the transform's domain accepts it and -- for `fundamental` only -- its
  confidence reaches `RETRIEVAL_FUNDAMENTAL_CONFIDENCE_FLOOR`. Unknown values
  are masked: never zero-filled, never imputed, never dropped and never turned
  into `-inf`.
- The population the statistics come from is the caller's (the whole stored
  library at one analysis version, issue #25's integration), never the query's
  own return set, so one normalization record serves every query.
- Every statistic is a finite double. A population whose sum or deviations
  overflow a double falls back to a magnitude-scaled computation of the same
  formula, so no returned `mean` or `std` is ever non-finite and
  `backend.analysis.batch.canonical` never sees a NaN or an infinity.
- The module is pure: it imports the standard library, `backend.contracts` and
  `backend.analysis.batch`'s `digest` only. It never imports `sqlite3`, never
  imports `backend.library`, `backend.palette.compatibility`,
  `backend.palette.ranking` or `backend.intelligence`, never opens a path,
  never decodes audio, never touches the network, never spawns a process and
  never reads the clock or a random source.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, fields
import math

from backend.analysis.batch import digest
from backend.contracts import MEASURES, Sample


RETRIEVAL_POLICY_VERSION = "feature-retrieval-v1"
REPRESENTATION_VERSION = "kick-bass-feature-space-v1"

SHORTLIST_MIN = 50
SHORTLIST_MAX = 100
DEFAULT_SHORTLIST_SIZE = 100

MIN_RETRIEVAL_COVERAGE = 0.50
NORMALIZATION_EPSILON = 1e-9
MIN_NORMALIZATION_POPULATION = 2
MAX_ABSOLUTE_Z = 1e9

# A retrieval-side constant, deliberately not #11's CONFIDENCE_THRESHOLD: the
# two policies answer different questions and a change to one must not move the
# other. A test changes #11's constant and asserts retrieval is unchanged.
RETRIEVAL_FUNDAMENTAL_CONFIDENCE_FLOOR = 0.80

RETRIEVAL_CANDIDATE_ROLES = ("bass", "sub-bass")

RETRIEVAL_DIMENSIONS = (
    "fundamental",
    "spectral_centroid",
    "spectral_rolloff",
    "loudness",
    "crest_factor",
    "transient_strength",
    "attack",
    "decay",
    "band_sub",
    "band_bass",
    "band_low_mid",
    "band_mid",
    "band_high_mid",
    "band_high",
)

# (dimension, transform) in RETRIEVAL_DIMENSIONS order, so the declared order is
# visible in one place and cannot drift from the tuple above.
RETRIEVAL_TRANSFORMS = (
    ("fundamental", "log2_hz"),
    ("spectral_centroid", "log2_hz"),
    ("spectral_rolloff", "log2_hz"),
    ("loudness", "linear"),
    ("crest_factor", "linear"),
    ("transient_strength", "linear"),
    ("attack", "log1p_ms"),
    ("decay", "log1p_ms"),
    ("band_sub", "linear"),
    ("band_bass", "linear"),
    ("band_low_mid", "linear"),
    ("band_mid", "linear"),
    ("band_high_mid", "linear"),
    ("band_high", "linear"),
)

DIMENSION_INACTIVE_REASONS = ("insufficient_population", "zero_variance")

SIMILARITY_UNAVAILABLE_REASONS = (
    "no_active_dimensions",
    "kick_dimensions_insufficient",
    "insufficient_common_dimensions",
    "extreme_dimension_value",
)

LIMIT_REASONS = ("shortlist_size", "eligible_exhausted", "no_candidates")

RETRIEVAL_ERROR_CODES = (
    "invalid_policy",
    "invalid_shortlist_size",
    "invalid_normalization",
    "unknown_dimension",
    "invalid_candidates",
    "invalid_kick",
    "invalid_analysis_version",
    "stale_kick_analysis",
    "candidate_analysis_version_mismatch",
    "duplicate_candidate_id",
)

# The transform of one dimension, and the dimension names of each transform.
_TRANSFORM_OF = dict(RETRIEVAL_TRANSFORMS)
_LOGGED = tuple(name for name, transform in RETRIEVAL_TRANSFORMS if transform != "linear")


if not set(RETRIEVAL_DIMENSIONS) <= set(MEASURES):  # pragma: no cover - a typo guard
    raise RuntimeError("RETRIEVAL_DIMENSIONS must name stored MEASURES.")


class RetrievalInputError(ValueError):
    """Malformed retrieval input; inspect `code`, never the message text.

    The message is path-free: it names a dimension, a version or a count and
    never a local path, a sample name or an audio byte.
    """

    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class RetrievalPolicy:
    """The one retrieval policy field: the requested shortlist size.

    A value that is not an `int` (a `bool` included) or that lies outside
    [`SHORTLIST_MIN`, `SHORTLIST_MAX`] raises `invalid_shortlist_size` here
    and again from `select_shortlist`.
    """

    shortlist_size: int = DEFAULT_SHORTLIST_SIZE

    def __post_init__(self):
        _require_shortlist_size(self.shortlist_size)

    def to_dict(self) -> dict:
        return {"shortlist_size": self.shortlist_size}


@dataclass(frozen=True)
class DimensionNormalization:
    """One dimension's normalization over the declared population.

    `mean` and `std` are the finite population statistics of the known
    transformed values, or None together with `inactive_reason` when the
    dimension carries none: `insufficient_population` for fewer than
    `MIN_NORMALIZATION_POPULATION` known values, `zero_variance` for a
    standard deviation below `NORMALIZATION_EPSILON`.
    """

    name: str
    transform: str
    mean: float | None
    std: float | None
    known_count: int
    inactive_reason: str | None

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "transform": self.transform,
            "mean": self.mean,
            "std": self.std,
            "known_count": self.known_count,
            "inactive_reason": self.inactive_reason,
        }


@dataclass(frozen=True)
class DimensionMask:
    """How many population values one dimension kept and how many it rejected.

    `known_count` is the count `DimensionNormalization` carries;
    `domain_rejected` is the count of stored values the transform's domain
    refused (a non-positive frequency or a negative duration), which are masked
    as unknown rather than turned into `-inf`.
    """

    name: str
    known_count: int
    domain_rejected: int

    def to_dict(self) -> dict:
        return {"name": self.name, "known_count": self.known_count,
                "domain_rejected": self.domain_rejected}


@dataclass(frozen=True)
class NormalizationRecord:
    """One fitted normalization: the population it covers and its statistics.

    Versioned and path-free. `population_digest` is `digest` over the
    representation version, the analysis version, the candidate role tuple and
    the sorted `(sample_id, analysis_version)` pairs; `normalization_id` is
    `digest(record.to_dict())`. Both are bare 64-character lowercase hex and
    contain no local path, pack name, filename or timestamp, so two machines
    that hold the same content analysed by the same runtime versions derive the
    same id at different library roots.
    """

    representation_version: str
    analysis_version: str
    candidate_roles: tuple
    sample_count: int
    population_digest: str
    dimensions: tuple

    def to_dict(self) -> dict:
        return {
            "representation_version": self.representation_version,
            "analysis_version": self.analysis_version,
            "candidate_roles": list(self.candidate_roles),
            "sample_count": self.sample_count,
            "population_digest": self.population_digest,
            "dimensions": [dimension.to_dict() for dimension in self.dimensions],
        }


@dataclass(frozen=True)
class RetrievedCandidate:
    """One ordered candidate: its similarity or exactly one unavailable reason.

    `position` is 1-based within `RetrievalResult.ranked`, `coverage` is the
    share of active dimensions the candidate and the kick both know, and
    `common_dimensions` names them in declared order.
    """

    sample_id: str
    position: int
    similarity: float | None
    similarity_unavailable_reason: str | None
    coverage: float
    common_dimensions: tuple

    def to_dict(self) -> dict:
        return {
            "sample_id": self.sample_id,
            "position": self.position,
            "similarity": self.similarity,
            "similarity_unavailable_reason": self.similarity_unavailable_reason,
            "coverage": self.coverage,
            "common_dimensions": list(self.common_dimensions),
        }


@dataclass(frozen=True)
class CutoffEvidence:
    """Where the shortlist was cut, and every candidate tied at that value.

    `tied_ids` names every ranked candidate whose similarity is exactly equal
    to the last included candidate's (all of them, not only those beside the
    boundary); `included` is the last shortlist id, `excluded` the first id
    left out, or None when the shortlist was not cut.
    """

    similarity: float | None
    included: str | None
    excluded: str | None
    tied_ids: tuple

    def to_dict(self) -> dict:
        return {
            "similarity": self.similarity,
            "included": self.included,
            "excluded": self.excluded,
            "tied_ids": list(self.tied_ids),
        }


@dataclass(frozen=True)
class RetrievalResult:
    """One pure retrieval: the ordered eligible candidates and the shortlist.

    Carries no compatibility, confidence, dimension score or weight field, and
    no path: the order is a plausibility order for the Jev stage, not a
    recommendation.
    """

    policy_version: str
    representation_version: str
    normalization_id: str
    analysis_version: str
    policy: RetrievalPolicy
    shortlist_size_requested: int
    shortlist_size_returned: int
    limit_reason: str
    ranked: tuple
    shortlist: tuple
    cutoff: CutoffEvidence

    def to_dict(self) -> dict:
        return {
            "policy_version": self.policy_version,
            "representation_version": self.representation_version,
            "normalization_id": self.normalization_id,
            "analysis_version": self.analysis_version,
            "policy": self.policy.to_dict(),
            "shortlist_size_requested": self.shortlist_size_requested,
            "shortlist_size_returned": self.shortlist_size_returned,
            "limit_reason": self.limit_reason,
            "ranked": [candidate.to_dict() for candidate in self.ranked],
            "shortlist": list(self.shortlist),
            "cutoff": self.cutoff.to_dict(),
        }


def _require_shortlist_size(value) -> int:
    if isinstance(value, bool) or not isinstance(value, int) \
            or not SHORTLIST_MIN <= value <= SHORTLIST_MAX:
        raise RetrievalInputError(
            "invalid_shortlist_size",
            f"A shortlist size must be an int between {SHORTLIST_MIN} and {SHORTLIST_MAX}.")
    return value


def _require_samples(samples, *, code="invalid_candidates"):
    if isinstance(samples, (str, bytes)) or not isinstance(samples, Sequence):
        raise RetrievalInputError(code, "Expected a sequence of contract Samples.")
    result = []
    for sample in samples:
        if type(sample) is not Sample:
            raise RetrievalInputError(code, "Expected a sequence of contract Samples.")
        result.append(sample)
    return tuple(result)


def _require_analysis_version(analysis_version) -> str:
    if not isinstance(analysis_version, str) or not analysis_version.strip():
        raise RetrievalInputError(
            "invalid_analysis_version", "analysis_version must be a non-empty string.")
    return analysis_version


def _measurements(sample: Sample) -> dict:
    return {measurement.name: measurement for measurement in sample.features.measurements}


def _transformed(value: float, transform: str):
    """The transformed value, or None when the transform's domain refuses it."""

    if transform == "log2_hz":
        return math.log2(value) if value > 0 else None
    if transform == "log1p_ms":
        return math.log1p(value) if value >= 0 else None
    return value


def _masked(measured: dict, dimension: str):
    """One dimension's transformed value for one sample, or None when unknown."""

    measurement = measured.get(dimension)
    if measurement is None or measurement.value is None:
        return None
    if dimension == "fundamental":
        confidence = measurement.confidence
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) \
                or confidence < RETRIEVAL_FUNDAMENTAL_CONFIDENCE_FLOOR:
            return None
    return _transformed(measurement.value, _TRANSFORM_OF[dimension])


def mask_counts(samples, *, analysis_version) -> tuple:
    """Per-dimension known and domain-rejected counts over one population.

    The one place the masking rule is applied, in declared dimension order: a
    dimension is known for a sample when the sample stores it with a value, the
    transform's domain accepts that value and -- for `fundamental` only -- its
    confidence is a number at or above
    `RETRIEVAL_FUNDAMENTAL_CONFIDENCE_FLOOR`. A refused value is counted as
    domain-rejected and treated as unknown: it is never `-inf`, the sample is
    never dropped and no unknown value is imputed.
    """

    _require_analysis_version(analysis_version)
    population = _require_samples(samples)
    measured = tuple(_measurements(sample) for sample in population)
    counts = []
    for dimension in RETRIEVAL_DIMENSIONS:
        known = rejected = 0
        for values in measured:
            measurement = values.get(dimension)
            if measurement is None or measurement.value is None:
                continue
            if _transformed(measurement.value, _TRANSFORM_OF[dimension]) is None:
                # The transform's domain refused the stored value; it is not
                # `-inf` and it is not the population mean.
                rejected += 1
            elif _masked(values, dimension) is not None:
                # The F0 confidence floor masks a value too, but that is a
                # reliability refusal, not a domain rejection, so it is counted
                # as neither known nor domain-rejected here.
                known += 1
        counts.append(DimensionMask(dimension, known, rejected))
    return tuple(counts)


def _statistics(values) -> tuple:
    """The population mean and standard deviation of the known values.

    The declared formulas are `mean = sum(values) / count` and
    `std = sqrt(sum((value - mean) ** 2) / count)` (ddof 0), computed in that
    order. A population whose sum or squared deviations overflow a double would
    return a non-finite statistic; that population is re-scaled by its largest
    magnitude, which keeps both statistics finite without changing the formula
    for any population that does not overflow.
    """

    count = len(values)
    total = 0.0
    for value in values:
        total += value
    mean = total / count if math.isfinite(total) else None
    if mean is None:
        scale = max(abs(value) for value in values)
        scaled_total = 0.0
        for value in values:
            scaled_total += value / scale
        mean = scaled_total / count * scale
    squared = 0.0
    for value in values:
        squared += (value - mean) ** 2
    if math.isfinite(squared):
        std = math.sqrt(squared / count)
    else:
        scale = max(abs(value - mean) for value in values)
        scaled_squared = 0.0
        for value in values:
            scaled_squared += ((value - mean) / scale) ** 2
        std = math.sqrt(scaled_squared / count) * scale
    return mean, std


def fit_normalization(samples, *, analysis_version) -> NormalizationRecord:
    """Fit the population's normalization at one analysis version.

    `samples` is the declared population, one `Sample` per content identity:
    every stored row whose role is in `RETRIEVAL_CANDIDATE_ROLES` at exactly
    `analysis_version`, after the storage-side duplicate collapse. A supplied
    sample whose role is not a candidate role raises `invalid_candidates`, one
    whose analysis version differs raises `candidate_analysis_version_mismatch`
    and a repeated id raises `duplicate_candidate_id`. The population is fitted
    in ascending `sample_id` order, so the sums do not depend on the caller's
    order and the record reproduces byte for byte.
    """

    version = _require_analysis_version(analysis_version)
    population = _require_samples(samples)
    seen = set()
    for sample in population:
        if sample.role not in RETRIEVAL_CANDIDATE_ROLES:
            raise RetrievalInputError(
                "invalid_candidates",
                "The population holds only " + " and ".join(RETRIEVAL_CANDIDATE_ROLES) + " rows.")
        if sample.analysis_version != version:
            raise RetrievalInputError(
                "candidate_analysis_version_mismatch",
                "Every population sample must carry the normalization's analysis version.")
        if sample.sample_id in seen:
            raise RetrievalInputError(
                "duplicate_candidate_id",
                "One sample id appears twice; the storage-side collapse owns duplicate rows.")
        seen.add(sample.sample_id)
    population = tuple(sorted(population, key=lambda sample: sample.sample_id))
    measured = tuple(_measurements(sample) for sample in population)
    masks = mask_counts(population, analysis_version=version)
    dimensions = []
    for mask in masks:
        values = [value for values in measured
                  for value in (_masked(values, mask.name),) if value is not None]
        transform = _TRANSFORM_OF[mask.name]
        if mask.known_count < MIN_NORMALIZATION_POPULATION:
            dimensions.append(DimensionNormalization(
                mask.name, transform, None, None, mask.known_count, "insufficient_population"))
            continue
        mean, std = _statistics(values)
        if std < NORMALIZATION_EPSILON:
            dimensions.append(DimensionNormalization(
                mask.name, transform, None, None, mask.known_count, "zero_variance"))
            continue
        dimensions.append(DimensionNormalization(
            mask.name, transform, mean, std, mask.known_count, None))
    payload = {
        "representation_version": REPRESENTATION_VERSION,
        "analysis_version": version,
        "candidate_roles": list(RETRIEVAL_CANDIDATE_ROLES),
        "samples": [[sample.sample_id, sample.analysis_version] for sample in population],
    }
    return NormalizationRecord(
        representation_version=REPRESENTATION_VERSION,
        analysis_version=version,
        candidate_roles=RETRIEVAL_CANDIDATE_ROLES,
        sample_count=len(population),
        population_digest=digest(payload),
        dimensions=tuple(dimensions))


def _require_normalization(value) -> NormalizationRecord:
    if type(value) is not NormalizationRecord:
        raise RetrievalInputError("invalid_normalization", "An explicit NormalizationRecord is required.")
    if value.representation_version != REPRESENTATION_VERSION:
        raise RetrievalInputError(
            "invalid_normalization", "The normalization belongs to another representation version.")
    if value.candidate_roles != RETRIEVAL_CANDIDATE_ROLES:
        raise RetrievalInputError(
            "invalid_normalization", "The normalization was fitted over other candidate roles.")
    if isinstance(value.dimensions, (str, bytes)) or not isinstance(value.dimensions, Sequence):
        raise RetrievalInputError("invalid_normalization", "The normalization needs its dimension list.")
    names = [dimension.name for dimension in value.dimensions]
    if len(names) != len(set(names)):
        raise RetrievalInputError("invalid_normalization", "A dimension name appears twice.")
    for name in names:
        if name not in RETRIEVAL_DIMENSIONS:
            raise RetrievalInputError("unknown_dimension", "The normalization names an unknown dimension.")
    if tuple(names) != RETRIEVAL_DIMENSIONS:
        raise RetrievalInputError(
            "invalid_normalization", "The normalization is not the declared dimension list in order.")
    for dimension in value.dimensions:
        if type(dimension) is not DimensionNormalization:
            raise RetrievalInputError("invalid_normalization", "A dimension entry is not a normalization.")
        if dimension.transform != _TRANSFORM_OF[dimension.name]:
            raise RetrievalInputError("invalid_normalization", "A transform disagrees with the declared one.")
        if isinstance(dimension.known_count, bool) or not isinstance(dimension.known_count, int) \
                or dimension.known_count < 0:
            raise RetrievalInputError("invalid_normalization", "A known count is not a non-negative int.")
        if dimension.inactive_reason is None:
            if dimension.mean is None or dimension.std is None \
                    or not math.isfinite(dimension.mean) or not math.isfinite(dimension.std):
                raise RetrievalInputError("invalid_normalization", "An active dimension needs finite statistics.")
        elif dimension.inactive_reason not in DIMENSION_INACTIVE_REASONS \
                or dimension.mean is not None or dimension.std is not None:
            raise RetrievalInputError("invalid_normalization", "An inactive dimension entry is malformed.")
    return value


def normalization_id(record) -> str:
    """The content identity of one `NormalizationRecord`: `digest(to_dict())`.

    Bare 64-character lowercase hex, matching `analysis_digest`, with no local
    path, pack name, filename or timestamp anywhere in the payload.
    """

    return digest(_require_normalization(record).to_dict())


def _require_policy(policy) -> RetrievalPolicy:
    if type(policy) is not RetrievalPolicy:
        raise RetrievalInputError("invalid_policy", "An explicit RetrievalPolicy is required.")
    _require_shortlist_size(policy.shortlist_size)
    return policy


def select_shortlist(kick, candidates, *, policy, normalization) -> RetrievalResult:
    """Order the candidates whose admission already happened and cut the order.

    Pure and I/O-free: it orders exactly the sequence it receives and cannot
    re-admit anything, because this module imports nothing from
    `backend.palette.compatibility`; #11 owns admission, this function owns the
    order. `kick` and every candidate must carry exactly the normalization's
    analysis version: a kick at another version raises `stale_kick_analysis`, a
    candidate at another version `candidate_analysis_version_mismatch` and a
    mismatched `representation_version` `invalid_normalization`.

    Coverage is `len(D) / len(active)` over the dimensions both samples know,
    `d = sqrt(sum((z_kick - z_candidate) ** 2 for i in D) / len(D))` over
    declared dimension order and `similarity = 1 / (1 + d)` in (0, 1]. An
    unknown similarity always carries exactly one of
    `SIMILARITY_UNAVAILABLE_REASONS` and a known one carries none.
    """

    policy = _require_policy(policy)
    normalization = _require_normalization(normalization)
    if type(kick) is not Sample:
        raise RetrievalInputError("invalid_kick", "The selected kick must be a contract Sample.")
    if kick.analysis_version != normalization.analysis_version:
        raise RetrievalInputError(
            "stale_kick_analysis", "The kick carries another analysis version than the normalization.")
    supplied = _require_samples(candidates)
    ids = [sample.sample_id for sample in supplied]
    if len(ids) != len(set(ids)):
        raise RetrievalInputError("duplicate_candidate_id", "Candidate ids must be unique.")
    for sample in supplied:
        if sample.analysis_version != normalization.analysis_version:
            raise RetrievalInputError(
                "candidate_analysis_version_mismatch",
                "Every candidate must carry the normalization's analysis version.")
    active = [dimension for dimension in normalization.dimensions
              if dimension.inactive_reason is None]
    mean = {dimension.name: dimension.mean for dimension in active}
    std = {dimension.name: dimension.std for dimension in active}
    kick_measured = _measurements(kick)
    kick_values = {dimension.name: _masked(kick_measured, dimension.name)
                   for dimension in active}
    kick_common = {name for name, value in kick_values.items() if value is not None}
    kick_coverage = (len(kick_common) / len(active)) if active else 0.0
    ordered = []
    for sample in sorted(supplied, key=lambda item: item.sample_id):
        values = _measurements(sample)
        candidate_values = {dimension.name: _masked(values, dimension.name)
                            for dimension in active}
        common = tuple(dimension.name for dimension in active
                       if dimension.name in kick_common
                       and candidate_values[dimension.name] is not None)
        coverage = (len(common) / len(active)) if active else 0.0
        similarity, reason = None, None
        if not active:
            reason = "no_active_dimensions"
        elif kick_coverage < MIN_RETRIEVAL_COVERAGE:
            reason = "kick_dimensions_insufficient"
        elif coverage < MIN_RETRIEVAL_COVERAGE:
            reason = "insufficient_common_dimensions"
        else:
            z_kick, z_candidate, extreme = {}, {}, False
            for name in common:
                z_kick[name] = (kick_values[name] - mean[name]) / std[name]
                z_candidate[name] = (candidate_values[name] - mean[name]) / std[name]
                if not math.isfinite(z_kick[name]) or not math.isfinite(z_candidate[name]) \
                        or abs(z_kick[name]) > MAX_ABSOLUTE_Z \
                        or abs(z_candidate[name]) > MAX_ABSOLUTE_Z:
                    extreme = True
            if extreme:
                reason = "extreme_dimension_value"
            else:
                total = 0.0
                for name in common:
                    total += (z_kick[name] - z_candidate[name]) ** 2
                distance = math.sqrt(total / len(common))
                similarity = 1.0 / (1.0 + distance)
        ordered.append((sample.sample_id, similarity, reason, coverage, common))
    scored = sorted((row for row in ordered if row[1] is not None),
                    key=lambda row: (-row[1], row[0]))
    unscored = sorted((row for row in ordered if row[1] is None), key=lambda row: row[0])
    ranked = tuple(
        RetrievedCandidate(sample_id=row[0], position=position, similarity=row[1],
                           similarity_unavailable_reason=row[2], coverage=row[3],
                           common_dimensions=row[4])
        for position, row in enumerate(scored + unscored, start=1))
    size = policy.shortlist_size
    returned = min(len(ranked), size)
    shortlist = tuple(candidate.sample_id for candidate in ranked[:returned])
    if not ranked:
        limit_reason = "no_candidates"
    elif len(ranked) > size:
        limit_reason = "shortlist_size"
    else:
        limit_reason = "eligible_exhausted"
    if not ranked:
        cutoff = CutoffEvidence(None, None, None, ())
    else:
        last = ranked[returned - 1]
        excluded = ranked[returned].sample_id if returned < len(ranked) else None
        if last.similarity is None:
            # An unavailable similarity is not a value that can tie; the
            # unscored order is already sample_id ascending, so the set is the
            # candidate itself.
            tied = (last.sample_id,)
        else:
            tied = tuple(candidate.sample_id for candidate in ranked
                         if candidate.similarity is not None
                         and candidate.similarity == last.similarity)
        cutoff = CutoffEvidence(last.similarity, last.sample_id, excluded, tied)
    return RetrievalResult(
        policy_version=RETRIEVAL_POLICY_VERSION,
        representation_version=REPRESENTATION_VERSION,
        normalization_id=normalization_id(normalization),
        analysis_version=normalization.analysis_version,
        policy=policy,
        shortlist_size_requested=size,
        shortlist_size_returned=returned,
        limit_reason=limit_reason,
        ranked=ranked,
        shortlist=shortlist,
        cutoff=cutoff)


# The public records' field lists, so a test can assert they did not grow.
RECORD_FIELDS = {
    "RetrievalPolicy": tuple(field.name for field in fields(RetrievalPolicy)),
    "DimensionNormalization": tuple(field.name for field in fields(DimensionNormalization)),
    "DimensionMask": tuple(field.name for field in fields(DimensionMask)),
    "NormalizationRecord": tuple(field.name for field in fields(NormalizationRecord)),
    "RetrievedCandidate": tuple(field.name for field in fields(RetrievedCandidate)),
    "CutoffEvidence": tuple(field.name for field in fields(CutoffEvidence)),
    "RetrievalResult": tuple(field.name for field in fields(RetrievalResult)),
}
