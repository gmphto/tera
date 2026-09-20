"""Versioned decision caching for validated Jev decisions (issue #26).

The cache stores a #13 response document and the decision the caller produced,
keyed by one canonical content identity, so a repeated recommendation request
reuses a decision instead of asking Jev again. See _docs/decision-cache.md for
the table contract, the identity rules, the four miss reasons and the pruning
order. This module is the only entry point and it holds no statement of its
own: every read and write is a repository operation in
backend.library.repository.

Design rules:

- The key is one frozen record over the palette content hash (#24), the
  candidate and kick identity and content, the analysis, prompt, adapter and
  model versions and the ranking and weight-table versions. It never carries a
  local path, a filename, a library root, a pack name, a rank, a rendered reason
  or an audio byte.
- A stored document is never trusted: every hit is re-validated through
  backend.intelligence.decisions.validate_response, and a document that no
  longer validates is a miss, never a served judgment.
- A miss never deletes: a stale entry stops being addressable and is removed
  only by invalidate_entry, invalidate_candidate or prune_cache. An entry
  written by a newer version is kept, because deleting it would silently reset
  the cache.
- A cache hit is reused evidence, never a live outcome. Every stored judgment
  carries origin 'cache' plus the original run's provenance, so a cached
  judgment is excluded from every live count (#19) and from every lift claim.
- A store is an optimization and never the caller's correctness dependency: a
  refused or rolled-back write leaves every existing entry unchanged and the
  caller's already-computed decision valid and usable.

Standard library only, plus this product's contract, analysis, intelligence,
ranking and library modules. No ranking function is called, no file is opened
and no network is reached.
"""

from __future__ import annotations

from dataclasses import dataclass, fields, is_dataclass
import json
import sqlite3
from typing import get_args

from backend.analysis.batch import canonical, digest
from backend.contracts import Dimension, JevJudgment, LabelProbability
from backend.intelligence.decisions import (
    PROBABILITY_LABELS,
    RESPONSE_FIELDS,
    JevResponseError,
    validate_response,
)
from backend.intelligence.jev import (
    NOT_ATTEMPTED_CODES,
    TRANSPORT_ERROR_CODES,
    TRANSPORT_SOURCES,
    UNAVAILABLE_CODES,
    JevOutcome,
    JevScoringRun,
)
from backend.intelligence.questions import (
    PROMPT_VERSION,
    QUESTION_UNAVAILABLE_CODES,
    JevQuestion,
    UnavailableQuestion,
)
from backend.library.errors import (
    CacheKeyInvalid,
    CachePayloadInvalid,
    DecisionNotCacheable,
    InvalidCacheBound,
)
from backend.library.repository import LibraryRepository, coded_error, transaction
from backend.library.schema import utc_now
from backend.palette.ranking import (
    HYBRID_CODES,
    JEV_ABSENT,
    JEV_PARTIAL,
    JEV_PRESENT,
    MODE_DSP_ONLY,
    MODE_HYBRID,
)


# The key design version. A change to what a key covers needs a new value, and
# an entry written under an old value is a miss with reason version_unsupported
# rather than a deleted row.
CACHE_KEY_VERSION = "decision-cache-v1"

# The payload design version, carried by every stored record. A change to a
# stored record's shape needs a new value, with the same keep-don't-delete rule.
CACHE_PAYLOAD_VERSION = "decision-cache-payload-v1"

# The two decision kinds: one asked question, or one candidate's whole decision.
DECISION_KINDS = ("judgment", "candidate_decision")

# The one origin a cached judgment carries. A cache hit is reused evidence,
# never a live outcome.
CACHE_ORIGIN = "cache"

# The #14 outcome states that hold a validated terminal decision; every other
# state is refused with decision_not_cacheable.
CACHEABLE_OUTCOME_STATES = ("judged", "abstained")

# Every reason a lookup reports a miss, and nothing else.
CACHE_MISS_REASONS = ("absent", "version_unsupported", "cached_response_invalid", "cache_corrupt")

# The published entry and byte bounds. A bound outside them, or one that is not
# an int, raises invalid_cache_bound and writes nothing.
DEFAULT_CACHE_MAX_ENTRIES = 50000
MIN_CACHE_MAX_ENTRIES = 1
MAX_CACHE_MAX_ENTRIES = 1000000
DEFAULT_CACHE_MAX_BYTES = 67108864
MIN_CACHE_MAX_BYTES = 4096
MAX_CACHE_MAX_BYTES = 1073741824

# The largest stored payload this module writes, in canonical JSON bytes.
MAX_PAYLOAD_BYTES = 65536

_HIT = "hit"
_MISS = "miss"

# The six contract dimensions in the contract literal's own order.
_DIMENSIONS = get_args(Dimension)

# The mode and jev_status literals a decision may carry, from #15.
_MODES = (MODE_HYBRID, MODE_DSP_ONLY)
_JEV_STATUSES = (JEV_PRESENT, JEV_PARTIAL, JEV_ABSENT)

# The #13 question code set and every #14 outcome code a dimension entry's
# unavailable_reason may carry.
_DIMENSION_REASONS = (frozenset(QUESTION_UNAVAILABLE_CODES) | frozenset(TRANSPORT_ERROR_CODES)
                      | frozenset(NOT_ATTEMPTED_CODES) | frozenset(UNAVAILABLE_CODES))

# The key's field names, in the documented order.
_KEY_FIELDS = (
    "cache_key_version", "decision_kind", "source", "interface_name", "adapter_version",
    "prompt_version", "model_version", "palette_hash", "palette_hash_version", "candidate_id",
    "candidate_content_fingerprint", "candidate_analysis_version", "dimension", "question_id",
    "kick_id", "kick_content_fingerprint", "kick_analysis_version", "questions_digest",
    "ranking_version", "weight_table_id", "baseline_ranking_version", "baseline_weight_table_id",
)

# The fields every key carries as nonblank text.
_KEY_SHARED_FIELDS = _KEY_FIELDS[:12]

# The eight fields only a candidate_decision key carries; they are NULL for a
# judgment key, exactly as the table's CHECKs require.
_KEY_CANDIDATE_FIELDS = _KEY_FIELDS[14:]

_CACHED_JUDGMENT_FIELDS = ("payload_version", "dimension", "question_id", "response_json",
                           "origin", "origin_source", "origin_interface_name", "adapter_version",
                           "attempts", "origin_elapsed_ms", "created_at")

_CACHED_DIMENSION_FIELDS = ("dimension", "dsp_compatibility", "dsp_unavailable_reason",
                            "judgment", "unavailable_reason", "combined_compatibility")

_CACHED_DECISION_FIELDS = ("payload_version", "candidate_id", "analysis_version",
                           "ranking_version", "weight_table_id", "mode", "jev_status",
                           "compatibility", "confidence", "uncertain", "dimensions", "warnings")

_MODEL_VERSION_PIN_FIELDS = ("interface_name", "source", "adapter_version", "prompt_version",
                             "model_version", "first_observed_at", "observed_at",
                             "observation_count")


class _ShapeError(ValueError):
    """A supplied or stored record is not the declared shape; never escapes as itself."""


def _blank(value) -> bool:
    return not isinstance(value, str) or not value.strip()


def _plain(value):
    """A record as plain JSON data: dataclasses become objects, tuples arrays."""

    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: _plain(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, tuple):
        return [_plain(item) for item in value]
    return value


def _exact(payload, names, label):
    if type(payload) is not dict:
        raise _ShapeError("A " + label + " record must be a JSON object.")
    missing = sorted(set(names) - set(payload))
    extra = sorted(set(payload) - set(names))
    if missing or extra:
        raise _ShapeError("A " + label + " record carries exactly its documented fields; missing "
                          + repr(missing) + ", unexpected " + repr(extra) + ".")
    return payload


def _text(payload, name, label):
    value = payload[name]
    if type(value) is not str or not value.strip():
        raise _ShapeError(label + "." + name + " must be nonblank text.")
    return value


def _text_value(value, name):
    if type(value) is not str or not value.strip():
        raise CachePayloadInvalid(name + " must be nonblank text.")
    return value


def _optional_text(payload, name, label):
    value = payload[name]
    if value is None:
        return None
    if type(value) is not str or not value.strip():
        raise _ShapeError(label + "." + name + " must be null or nonblank text.")
    return value


def _unit(payload, name, label):
    value = payload[name]
    if type(value) is bool or type(value) not in (int, float) or not 0 <= value <= 1:
        raise _ShapeError(label + "." + name + " must be a number in [0, 1].")
    return value


def _optional_unit(payload, name, label):
    value = payload[name]
    if value is None:
        return None
    return _unit(payload, name, label)


def _count(payload, name, label):
    value = payload[name]
    if type(value) is not int or isinstance(value, bool) or value < 0:
        raise _ShapeError(label + "." + name + " must be a nonnegative whole number.")
    return value


def _flag(payload, name, label):
    value = payload[name]
    if type(value) is not bool:
        raise _ShapeError(label + "." + name + " must be true or false.")
    return value


def _decoded(value, label):
    if type(value) is not str:
        raise _ShapeError("A " + label + " record must be JSON text.")
    try:
        return json.loads(value)
    except json.JSONDecodeError as error:
        raise _ShapeError("A " + label + " record must be JSON text.") from error


def _parsed(cls, payload, names, label, values):
    """One record from a mapping, or CachePayloadInvalid when it is not that shape."""

    try:
        _exact(payload, names, label)
        return cls(**values(payload))
    except _ShapeError as error:
        raise CachePayloadInvalid(str(error)) from error


@dataclass(frozen=True)
class JudgmentIdentity:
    """The content identity of one asked question about one candidate.

    'source', 'interface_name', 'adapter_version' and 'prompt_version' name the
    transport and the prompt the question was asked with; 'model_version' is the
    pinned model version; 'palette_hash' and 'palette_hash_version' are #24's;
    the three candidate fields identify the candidate's content and analysis.
    """

    source: str
    interface_name: str
    adapter_version: str
    prompt_version: str
    model_version: str
    palette_hash: str
    palette_hash_version: str
    candidate_id: str
    candidate_content_fingerprint: str
    candidate_analysis_version: str

    def to_dict(self):
        return _plain(self)


@dataclass(frozen=True)
class CandidateDecisionIdentity:
    """The content identity of one candidate's whole decision.

    Everything a JudgmentIdentity carries, plus the kick's identity and content,
    the question set's digest inputs and the ranking and baseline versions the
    decision was produced under.
    """

    source: str
    interface_name: str
    adapter_version: str
    prompt_version: str
    model_version: str
    palette_hash: str
    palette_hash_version: str
    candidate_id: str
    candidate_content_fingerprint: str
    candidate_analysis_version: str
    kick_id: str
    kick_content_fingerprint: str
    kick_analysis_version: str
    ranking_version: str
    weight_table_id: str
    baseline_ranking_version: str
    baseline_weight_table_id: str

    def to_dict(self):
        return _plain(self)


@dataclass(frozen=True)
class DecisionCacheKey:
    """One canonical cache key: the identity of exactly one stored decision.

    Constructed only by judgment_key or candidate_decision_key. The constructor
    refuses a blank text field, an unknown decision_kind or source, and any kind
    whose optional-field set does not match the table's CHECKs, with
    cache_key_invalid.
    """

    cache_key_version: str
    decision_kind: str
    source: str
    interface_name: str
    adapter_version: str
    prompt_version: str
    model_version: str
    palette_hash: str
    palette_hash_version: str
    candidate_id: str
    candidate_content_fingerprint: str
    candidate_analysis_version: str
    dimension: str | None
    question_id: str | None
    kick_id: str | None
    kick_content_fingerprint: str | None
    kick_analysis_version: str | None
    questions_digest: str | None
    ranking_version: str | None
    weight_table_id: str | None
    baseline_ranking_version: str | None
    baseline_weight_table_id: str | None

    def __post_init__(self):
        for name in _KEY_SHARED_FIELDS:
            if _blank(getattr(self, name)):
                raise CacheKeyInvalid("cache key " + name + " must be nonblank text.")
        if self.decision_kind not in DECISION_KINDS:
            raise CacheKeyInvalid("decision_kind must be one of "
                                  + ", ".join(DECISION_KINDS) + ".")
        if self.source not in TRANSPORT_SOURCES:
            raise CacheKeyInvalid("source must be one of " + ", ".join(TRANSPORT_SOURCES) + ".")
        if self.decision_kind == "judgment":
            required, forbidden = ("dimension", "question_id"), _KEY_CANDIDATE_FIELDS
        else:
            required, forbidden = _KEY_CANDIDATE_FIELDS, ("dimension", "question_id")
        for name in required:
            if _blank(getattr(self, name)):
                raise CacheKeyInvalid("a " + self.decision_kind + " key needs " + name + ".")
        for name in forbidden:
            if getattr(self, name) is not None:
                raise CacheKeyInvalid("a " + self.decision_kind + " key cannot carry " + name + ".")

    def to_dict(self):
        return {name: getattr(self, name) for name in _KEY_FIELDS}

    def to_json(self):
        return canonical(self.to_dict())

    @classmethod
    def from_dict(cls, payload):
        try:
            _exact(payload, _KEY_FIELDS, "DecisionCacheKey")
            values = {}
            for name in _KEY_FIELDS[:12]:
                values[name] = _text(payload, name, "DecisionCacheKey")
            for name in _KEY_FIELDS[12:]:
                values[name] = _optional_text(payload, name, "DecisionCacheKey")
        except _ShapeError as error:
            raise CacheKeyInvalid(str(error)) from error
        return cls(**values)

    @classmethod
    def from_json(cls, value):
        try:
            return cls.from_dict(_decoded(value, "DecisionCacheKey"))
        except _ShapeError as error:
            raise CacheKeyInvalid(str(error)) from error


@dataclass(frozen=True)
class CachedJudgment:
    """One stored #13 response document with the run provenance it was stored from.

    'response_json' is the canonical eight-field response object; 'origin' is
    always CACHE_ORIGIN; 'origin_source', 'origin_interface_name',
    'adapter_version', 'attempts' and 'origin_elapsed_ms' are copied verbatim
    from the #14 outcome; 'created_at' is the cache write's timestamp.
    """

    payload_version: str
    dimension: str
    question_id: str
    response_json: str
    origin: str
    origin_source: str
    origin_interface_name: str
    adapter_version: str
    attempts: int
    origin_elapsed_ms: int
    created_at: str

    def to_dict(self):
        return _plain(self)

    def to_json(self):
        return canonical(self.to_dict())

    @classmethod
    def from_dict(cls, payload):
        return _parsed(cls, payload, _CACHED_JUDGMENT_FIELDS, "CachedJudgment",
                       _cached_judgment_values)

    @classmethod
    def from_json(cls, value):
        try:
            return cls.from_dict(_decoded(value, "CachedJudgment"))
        except _ShapeError as error:
            raise CachePayloadInvalid(str(error)) from error


@dataclass(frozen=True)
class CachedDimensionDecision:
    """One dimension's stored evidence inside a candidate decision.

    Exactly one of 'judgment' and 'unavailable_reason' is set: the judgment is a
    stored #13 document, the reason is a #13 question code or a #14 outcome
    code copied verbatim. 'dsp_compatibility' and 'combined_compatibility' are
    #15's numbers, never re-derived here.
    """

    dimension: str
    dsp_compatibility: float | None
    dsp_unavailable_reason: str | None
    judgment: CachedJudgment | None
    unavailable_reason: str | None
    combined_compatibility: float | None

    def to_dict(self):
        return _plain(self)

    def to_json(self):
        return canonical(self.to_dict())

    @classmethod
    def from_dict(cls, payload):
        return _parsed(cls, payload, _CACHED_DIMENSION_FIELDS, "CachedDimensionDecision",
                       _cached_dimension_values)

    @classmethod
    def from_json(cls, value):
        try:
            return cls.from_dict(_decoded(value, "CachedDimensionDecision"))
        except _ShapeError as error:
            raise CachePayloadInvalid(str(error)) from error


@dataclass(frozen=True)
class CachedCandidateDecision:
    """One candidate's whole stored decision, without a rank or a rendered reason.

    A rank is a property of one run's candidate set and a rendered reason is
    regenerated by later tasks, so neither is stored. 'dimensions' always
    carries the six contract dimensions in contract order.
    """

    payload_version: str
    candidate_id: str
    analysis_version: str
    ranking_version: str
    weight_table_id: str
    mode: str
    jev_status: str
    compatibility: float
    confidence: float
    uncertain: bool
    dimensions: tuple
    warnings: tuple

    def to_dict(self):
        return _plain(self)

    def to_json(self):
        return canonical(self.to_dict())

    @classmethod
    def from_dict(cls, payload):
        return _parsed(cls, payload, _CACHED_DECISION_FIELDS, "CachedCandidateDecision",
                       _cached_decision_values)

    @classmethod
    def from_json(cls, value):
        try:
            return cls.from_dict(_decoded(value, "CachedCandidateDecision"))
        except _ShapeError as error:
            raise CachePayloadInvalid(str(error)) from error


@dataclass(frozen=True)
class ModelVersionPin:
    """The current observed model version for one interface identity.

    'first_observed_at' is set once and kept; 'observed_at' and
    'observation_count' move on every observation. The primary key is
    (interface_name, source, adapter_version, prompt_version), so a double pin
    can never answer an interface lookup.
    """

    interface_name: str
    source: str
    adapter_version: str
    prompt_version: str
    model_version: str
    first_observed_at: str
    observed_at: str
    observation_count: int

    def to_dict(self):
        return _plain(self)

    def to_json(self):
        return canonical(self.to_dict())

    @classmethod
    def from_dict(cls, payload):
        return _parsed(cls, payload, _MODEL_VERSION_PIN_FIELDS, "ModelVersionPin",
                       _model_version_pin_values)

    @classmethod
    def from_json(cls, value):
        try:
            return cls.from_dict(_decoded(value, "ModelVersionPin"))
        except _ShapeError as error:
            raise CachePayloadInvalid(str(error)) from error


@dataclass(frozen=True)
class CacheLookup:
    """What one lookup found: a hit record and its age, or a documented miss.

    'reason' is None on a hit and one of CACHE_MISS_REASONS on a miss;
    'entry_created_at' is the stored row's timestamp on a hit and None on a
    miss. 'stale_found' is True when a row exists but cannot be served.
    """

    status: str
    reason: str | None
    judgment: JevJudgment | None
    decision: CachedCandidateDecision | None
    stale_found: bool
    entry_created_at: str | None

    def to_dict(self):
        return _plain(self)


@dataclass(frozen=True)
class StoreResult:
    """What one store did: whether it wrote, the row's timestamp and what it pruned."""

    stored: bool
    created_at: str | None
    pruned: int

    def to_dict(self):
        return _plain(self)


@dataclass(frozen=True)
class PruneReport:
    """What one on-demand prune deleted and what the bounds now hold."""

    deleted: int
    remaining_entries: int
    remaining_bytes: int
    max_entries: int
    max_bytes: int

    def to_dict(self):
        return _plain(self)


@dataclass(frozen=True)
class CacheStats:
    """The cache's row counts, stored payload bytes and timestamp extremes."""

    entries: int
    judgments: int
    candidate_decisions: int
    bytes: int
    oldest_created_at: str | None
    newest_created_at: str | None

    def to_dict(self):
        return _plain(self)


def _cached_judgment_values(payload):
    return {
        "payload_version": _text(payload, "payload_version", "CachedJudgment"),
        "dimension": _text(payload, "dimension", "CachedJudgment"),
        "question_id": _text(payload, "question_id", "CachedJudgment"),
        "response_json": _text(payload, "response_json", "CachedJudgment"),
        "origin": _text(payload, "origin", "CachedJudgment"),
        "origin_source": _text(payload, "origin_source", "CachedJudgment"),
        "origin_interface_name": _text(payload, "origin_interface_name", "CachedJudgment"),
        "adapter_version": _text(payload, "adapter_version", "CachedJudgment"),
        "attempts": _count(payload, "attempts", "CachedJudgment"),
        "origin_elapsed_ms": _count(payload, "origin_elapsed_ms", "CachedJudgment"),
        "created_at": _text(payload, "created_at", "CachedJudgment"),
    }


def _cached_dimension_values(payload):
    judgment = payload["judgment"]
    if judgment is not None and type(judgment) is not dict:
        raise _ShapeError("CachedDimensionDecision.judgment must be an object or null.")
    return {
        "dimension": _text(payload, "dimension", "CachedDimensionDecision"),
        "dsp_compatibility": _optional_unit(payload, "dsp_compatibility", "CachedDimensionDecision"),
        "dsp_unavailable_reason": _optional_text(payload, "dsp_unavailable_reason",
                                                 "CachedDimensionDecision"),
        "judgment": None if judgment is None else CachedJudgment.from_dict(judgment),
        "unavailable_reason": _optional_text(payload, "unavailable_reason",
                                             "CachedDimensionDecision"),
        "combined_compatibility": _optional_unit(payload, "combined_compatibility",
                                                 "CachedDimensionDecision"),
    }


def _cached_decision_values(payload):
    dimensions = payload["dimensions"]
    warnings = payload["warnings"]
    if type(dimensions) is not list or any(type(item) is not dict for item in dimensions):
        raise _ShapeError("CachedCandidateDecision.dimensions must be an array of objects.")
    if type(warnings) is not list:
        raise _ShapeError("CachedCandidateDecision.warnings must be an array.")
    return {
        "payload_version": _text(payload, "payload_version", "CachedCandidateDecision"),
        "candidate_id": _text(payload, "candidate_id", "CachedCandidateDecision"),
        "analysis_version": _text(payload, "analysis_version", "CachedCandidateDecision"),
        "ranking_version": _text(payload, "ranking_version", "CachedCandidateDecision"),
        "weight_table_id": _text(payload, "weight_table_id", "CachedCandidateDecision"),
        "mode": _text(payload, "mode", "CachedCandidateDecision"),
        "jev_status": _text(payload, "jev_status", "CachedCandidateDecision"),
        "compatibility": _unit(payload, "compatibility", "CachedCandidateDecision"),
        "confidence": _unit(payload, "confidence", "CachedCandidateDecision"),
        "uncertain": _flag(payload, "uncertain", "CachedCandidateDecision"),
        "dimensions": tuple(CachedDimensionDecision.from_dict(item) for item in dimensions),
        "warnings": tuple(_text_value(item, "CachedCandidateDecision.warnings[]")
                          for item in warnings),
    }


def _model_version_pin_values(payload):
    return {
        "interface_name": _text(payload, "interface_name", "ModelVersionPin"),
        "source": _text(payload, "source", "ModelVersionPin"),
        "adapter_version": _text(payload, "adapter_version", "ModelVersionPin"),
        "prompt_version": _text(payload, "prompt_version", "ModelVersionPin"),
        "model_version": _text(payload, "model_version", "ModelVersionPin"),
        "first_observed_at": _text(payload, "first_observed_at", "ModelVersionPin"),
        "observed_at": _text(payload, "observed_at", "ModelVersionPin"),
        "observation_count": _count(payload, "observation_count", "ModelVersionPin"),
    }


def _question_tokens(questions):
    """The digest input of an asked question set, in the order supplied.

    One token per question: its dimension and either the asked question's own
    digest or the code of a question this product refused to ask.
    """

    if type(questions) not in (list, tuple):
        raise CacheKeyInvalid("questions must be a list or tuple of #13 questions.")
    tokens = []
    for question in questions:
        if type(question) is JevQuestion:
            tokens.append([question.dimension, question.question_id])
        elif type(question) is UnavailableQuestion:
            tokens.append([question.dimension, question.code])
        else:
            raise CacheKeyInvalid(
                "Every question must be a #13 JevQuestion or UnavailableQuestion.")
    return tokens


def judgment_key(identity, question) -> DecisionCacheKey:
    """The canonical key of one asked question about one candidate.

    Fills 'dimension' and 'question_id' from the asked question and leaves the
    eight candidate-decision-only fields None. A question this product refused
    to ask has no digest and is a cache_key_invalid: there is nothing to ask
    again and nothing to key.
    """

    if type(identity) is not JudgmentIdentity:
        raise CacheKeyInvalid("judgment_key needs a JudgmentIdentity.")
    if type(question) is not JevQuestion:
        raise CacheKeyInvalid(
            "A judgment key names an asked #13 question; an unavailable or unknown question "
            "has no question_id.")
    return DecisionCacheKey(
        cache_key_version=CACHE_KEY_VERSION, decision_kind="judgment", source=identity.source,
        interface_name=identity.interface_name, adapter_version=identity.adapter_version,
        prompt_version=identity.prompt_version, model_version=identity.model_version,
        palette_hash=identity.palette_hash, palette_hash_version=identity.palette_hash_version,
        candidate_id=identity.candidate_id,
        candidate_content_fingerprint=identity.candidate_content_fingerprint,
        candidate_analysis_version=identity.candidate_analysis_version,
        dimension=question.dimension, question_id=question.question_id,
        kick_id=None, kick_content_fingerprint=None, kick_analysis_version=None,
        questions_digest=None, ranking_version=None, weight_table_id=None,
        baseline_ranking_version=None, baseline_weight_table_id=None)


def candidate_decision_key(identity, questions) -> DecisionCacheKey:
    """The canonical key of one candidate's whole decision over one question set.

    'questions_digest' is the digest of the asked questions in the order
    supplied; 'dimension' and 'question_id' stay None. Every identity field that
    the key does not take from the questions is copied from the identity.
    """

    if type(identity) is not CandidateDecisionIdentity:
        raise CacheKeyInvalid("candidate_decision_key needs a CandidateDecisionIdentity.")
    return DecisionCacheKey(
        cache_key_version=CACHE_KEY_VERSION, decision_kind="candidate_decision",
        source=identity.source, interface_name=identity.interface_name,
        adapter_version=identity.adapter_version, prompt_version=identity.prompt_version,
        model_version=identity.model_version, palette_hash=identity.palette_hash,
        palette_hash_version=identity.palette_hash_version, candidate_id=identity.candidate_id,
        candidate_content_fingerprint=identity.candidate_content_fingerprint,
        candidate_analysis_version=identity.candidate_analysis_version,
        dimension=None, question_id=None, kick_id=identity.kick_id,
        kick_content_fingerprint=identity.kick_content_fingerprint,
        kick_analysis_version=identity.kick_analysis_version,
        questions_digest=digest(_question_tokens(questions)),
        ranking_version=identity.ranking_version, weight_table_id=identity.weight_table_id,
        baseline_ranking_version=identity.baseline_ranking_version,
        baseline_weight_table_id=identity.baseline_weight_table_id)


def cache_key_digest(key) -> str:
    """The stored primary key of one cache key: bare 64-character lowercase hex.

    The digest is over the key's canonical JSON, so it is equal for equal keys
    whatever order their fields were built in, and it never contains a local
    path, a filename or a library name.
    """

    _require_key(key)
    return digest(key.to_dict())


def response_document(question, judgment) -> dict:
    """The exact eight-field #13 response object for one asked question.

    Everything the interface would have returned is here: the echoed question
    id, dimension and prompt version, the label, the five probabilities in
    contract order, the model's confidence, the model version and the
    abstention reason. Nothing else is added.
    """

    if type(question) is not JevQuestion:
        raise CachePayloadInvalid("response_document needs an asked #13 JevQuestion.")
    if type(judgment) is not JevJudgment:
        raise CachePayloadInvalid("response_document needs a contract JevJudgment.")
    return {
        "question_id": question.question_id,
        "dimension": question.dimension,
        "label": judgment.label,
        "probabilities": _ordered_probabilities(judgment),
        "confidence": judgment.confidence,
        "model_version": judgment.model_version,
        "prompt_version": question.prompt_version,
        "unavailable_reason": judgment.unavailable_reason,
    }


def _ordered_probabilities(judgment):
    """The judgment's probabilities as contract-ordered label/probability pairs."""

    if judgment.label is None:
        return None
    report = {}
    for item in judgment.probabilities:
        if type(item) is not LabelProbability:
            raise CachePayloadInvalid("A labeled judgment carries contract label probabilities.")
        if item.label in report:
            raise CachePayloadInvalid("A labeled judgment lists each label exactly once.")
        report[item.label] = item.probability
    if set(report) != set(PROBABILITY_LABELS):
        raise CachePayloadInvalid("A labeled judgment lists each of the five labels exactly once.")
    return [{"label": label, "probability": report[label]} for label in PROBABILITY_LABELS]


def _require_key(key):
    if type(key) is not DecisionCacheKey:
        raise CacheKeyInvalid("Expected a DecisionCacheKey.")
    key.__post_init__()
    return key


def _require_bounds(max_entries, max_bytes):
    if type(max_entries) is not int or not MIN_CACHE_MAX_ENTRIES <= max_entries <= MAX_CACHE_MAX_ENTRIES:
        raise InvalidCacheBound(
            "max_entries must be a whole number in [" + str(MIN_CACHE_MAX_ENTRIES) + ", "
            + str(MAX_CACHE_MAX_ENTRIES) + "].")
    if type(max_bytes) is not int or not MIN_CACHE_MAX_BYTES <= max_bytes <= MAX_CACHE_MAX_BYTES:
        raise InvalidCacheBound(
            "max_bytes must be a whole number in [" + str(MIN_CACHE_MAX_BYTES) + ", "
            + str(MAX_CACHE_MAX_BYTES) + "].")


def _payload_json(record) -> str:
    return canonical(_plain(record))


def _require_payload_size(payload_json) -> None:
    if len(payload_json.encode("utf-8")) > MAX_PAYLOAD_BYTES:
        raise CachePayloadInvalid(
            "A stored payload must be at most " + str(MAX_PAYLOAD_BYTES) + " bytes.")


def store_judgment(connection, key, *, outcome, max_entries=DEFAULT_CACHE_MAX_ENTRIES,
                   max_bytes=DEFAULT_CACHE_MAX_BYTES) -> StoreResult:
    """Store one validated terminal judgment, or refuse it with a coded failure.

    Only a #14 outcome whose state is in CACHEABLE_OUTCOME_STATES and whose
    judgment is a contract JevJudgment is storable; every other state raises
    decision_not_cacheable and writes no row. The stored document is
    response_document(question, judgment) over the question the key names, and
    the call refuses with cache_payload_invalid unless re-validating that
    document through #13 reproduces the outcome's judgment exactly. A key whose
    model version is not the judgment's own raises cache_key_invalid.
    """

    _require_key(key)
    if key.decision_kind != "judgment":
        raise CacheKeyInvalid("store_judgment needs a judgment key.")
    _require_bounds(max_entries, max_bytes)
    if type(outcome) is not JevOutcome or outcome.state not in CACHEABLE_OUTCOME_STATES:
        raise DecisionNotCacheable(
            "Only outcomes in " + ", ".join(CACHEABLE_OUTCOME_STATES) + " hold a decision to cache.")
    judgment = outcome.judgment
    if type(judgment) is not JevJudgment or key.source not in TRANSPORT_SOURCES:
        raise DecisionNotCacheable("A cached judgment needs a contract judgment and a transport source.")
    if judgment.model_version != key.model_version:
        raise CacheKeyInvalid("The key's model_version is not the judgment's own model_version.")
    if judgment.prompt_version != PROMPT_VERSION:
        raise CachePayloadInvalid("A stored judgment carries this product's prompt version.")
    question = _key_question(key)
    document = response_document(question, judgment)
    if set(document) != set(RESPONSE_FIELDS):
        raise CachePayloadInvalid("A stored response carries exactly the eight #13 fields.")
    payload_json = canonical(document)
    _require_payload_size(payload_json)
    try:
        reproduced = validate_response(question, document)
    except JevResponseError as error:
        raise CachePayloadInvalid(
            "The stored document is not a valid #13 response: " + error.code + ".") from error
    if reproduced != judgment:
        raise CachePayloadInvalid(
            "Re-validating the stored document does not reproduce the outcome's judgment.")
    record = CachedJudgment(
        payload_version=CACHE_PAYLOAD_VERSION, dimension=key.dimension, question_id=key.question_id,
        response_json=payload_json, origin=CACHE_ORIGIN, origin_source=key.source,
        origin_interface_name=key.interface_name, adapter_version=key.adapter_version,
        attempts=outcome.attempts, origin_elapsed_ms=outcome.elapsed_ms, created_at=utc_now())
    return _store(connection, key, _payload_json(record), record.created_at, max_entries, max_bytes)


def _key_question(key) -> JevQuestion:
    """The asked question one judgment key names, rebuilt from the key itself.

    store_judgment has no question parameter: the key already carries the asked
    question_id, dimension and prompt_version, and #13's validate_response
    compares exactly those three against the question it is given.
    """

    return JevQuestion(question_id=key.question_id, dimension=key.dimension,
                       prompt_version=key.prompt_version, instruction="", evidence=(),
                       withheld=())


def store_candidate_decision(connection, key, *, decision,
                             max_entries=DEFAULT_CACHE_MAX_ENTRIES,
                             max_bytes=DEFAULT_CACHE_MAX_BYTES) -> StoreResult:
    """Store one candidate decision, or refuse it with cache_payload_invalid.

    The decision must be exactly the CachedCandidateDecision shape over the six
    contract dimensions in order, with #15's literals, #13/#14 reason codes, one
    of a judgment or a reason per dimension, and no disagreement with the key.
    No ranking function is called: the caller produced the decision.
    """

    _require_key(key)
    if key.decision_kind != "candidate_decision":
        raise CacheKeyInvalid("store_candidate_decision needs a candidate_decision key.")
    _require_bounds(max_entries, max_bytes)
    record = _validated_decision(decision, key)
    payload_json = _payload_json(record)
    _require_payload_size(payload_json)
    return _store(connection, key, payload_json, utc_now(), max_entries, max_bytes)


def _validated_decision(decision, key) -> CachedCandidateDecision:
    if type(decision) is CachedCandidateDecision:
        payload = _plain(decision)
    elif type(decision) is dict:
        payload = decision
    else:
        raise CachePayloadInvalid("decision must be a CachedCandidateDecision record.")
    record = CachedCandidateDecision.from_dict(payload)
    if record.payload_version != CACHE_PAYLOAD_VERSION:
        raise CachePayloadInvalid("A stored decision carries this payload version.")
    if record.candidate_id != key.candidate_id:
        raise CachePayloadInvalid("decision.candidate_id disagrees with the key.")
    if record.analysis_version != key.candidate_analysis_version:
        raise CachePayloadInvalid("decision.analysis_version disagrees with the key.")
    if record.mode not in _MODES:
        raise CachePayloadInvalid("decision.mode must be one of " + ", ".join(_MODES) + ".")
    if record.jev_status not in _JEV_STATUSES:
        raise CachePayloadInvalid("decision.jev_status must be one of "
                                  + ", ".join(_JEV_STATUSES) + ".")
    if [entry.dimension for entry in record.dimensions] != list(_DIMENSIONS):
        raise CachePayloadInvalid("decision.dimensions must be the six contract dimensions in "
                                  "contract order.")
    for entry in record.dimensions:
        if entry.judgment is None and entry.unavailable_reason is None:
            raise CachePayloadInvalid("dimension " + entry.dimension
                                      + " needs a judgment or an unavailable_reason.")
        if entry.judgment is not None and entry.unavailable_reason is not None:
            raise CachePayloadInvalid("dimension " + entry.dimension
                                      + " cannot carry both a judgment and an unavailable_reason.")
        if entry.unavailable_reason is not None and entry.unavailable_reason not in _DIMENSION_REASONS:
            raise CachePayloadInvalid("dimension " + entry.dimension
                                      + " carries an unknown unavailable_reason.")
        if entry.judgment is not None:
            if entry.judgment.dimension != entry.dimension:
                raise CachePayloadInvalid("dimension " + entry.dimension
                                          + " carries another dimension's judgment.")
            if entry.judgment.payload_version != CACHE_PAYLOAD_VERSION:
                raise CachePayloadInvalid("dimension " + entry.dimension
                                          + " carries another payload version.")
    for warning in record.warnings:
        if warning not in HYBRID_CODES:
            raise CachePayloadInvalid("decision.warnings must be #15 hybrid codes.")
    return record


def _store(connection, key, payload_json, created_at, max_entries, max_bytes) -> StoreResult:
    values = dict(key.to_dict())
    values["cache_key"] = cache_key_digest(key)
    values["payload_json"] = payload_json
    values["created_at"] = created_at
    repository = LibraryRepository(connection)
    try:
        with transaction(connection):
            stored = repository.insert_decision_cache_entry(values)
            existing = (None if stored
                        else repository.decision_cache_created_at(values["cache_key"]))
            pruned = (_evict(repository, values["cache_key"], max_entries, max_bytes)
                      if stored else 0)
    except sqlite3.Error as error:
        raise coded_error(error) from error
    return StoreResult(stored=stored, created_at=created_at if stored else existing, pruned=pruned)


def _evict(repository, keep_key, max_entries, max_bytes) -> int:
    """Delete the oldest rows until both bounds hold, never the row just written."""

    totals = repository.decision_cache_totals()
    entries, total = totals["entries"], totals["bytes"]
    deleted = 0
    if entries <= max_entries and total <= max_bytes:
        return 0
    for row in repository.decision_cache_oldest_keys(keep_key):
        if entries <= max_entries and total <= max_bytes:
            break
        repository.delete_decision_cache_key(row["cache_key"])
        entries -= 1
        total -= row["payload_bytes"]
        deleted += 1
    return deleted


def lookup_judgment(connection, key, *, question) -> CacheLookup:
    """One stored judgment for one asked question, or a documented miss.

    The key must name the asked question exactly; a caller whose key and
    question disagree raises cache_key_invalid rather than reporting a stale
    entry. On a hit the stored document is re-validated through #13 and the
    judgment that call produces is returned with the entry's timestamp; a
    document that no longer validates is a miss with reason
    cached_response_invalid and no judgment is returned.
    """

    _require_key(key)
    if key.decision_kind != "judgment":
        raise CacheKeyInvalid("lookup_judgment needs a judgment key.")
    if type(question) is not JevQuestion:
        raise CacheKeyInvalid("lookup_judgment needs the asked #13 JevQuestion.")
    if (key.question_id != question.question_id or key.dimension != question.dimension
            or key.prompt_version != question.prompt_version):
        raise CacheKeyInvalid(
            "The key and the asked question disagree; this is a caller bug, never a stale entry.")
    row = LibraryRepository(connection).decision_cache_row(cache_key_digest(key))
    if row is None:
        return _miss("absent", False)
    if row["cache_key_version"] != CACHE_KEY_VERSION:
        return _miss("version_unsupported", True)
    record = _decoded_judgment(row["payload_json"])
    if record is None:
        return _miss("cache_corrupt", True)
    if record.payload_version != CACHE_PAYLOAD_VERSION:
        return _miss("version_unsupported", True)
    if record.dimension != key.dimension or record.question_id != key.question_id:
        return _miss("cache_corrupt", True)
    try:
        judgment = validate_response(question, record.response_json)
    except JevResponseError:
        return _miss("cached_response_invalid", True)
    return CacheLookup(status=_HIT, reason=None, judgment=judgment, decision=None,
                       stale_found=False, entry_created_at=row["created_at"])


def lookup_candidate_decision(connection, key, *, questions) -> CacheLookup:
    """One stored candidate decision for one question set, or a documented miss.

    The key's questions_digest must equal the digest of the supplied questions
    in the order supplied, or cache_key_invalid is raised. On a hit every
    embedded judgment is re-validated against the question supplied for its
    dimension; a judgment that no longer validates is a miss with reason
    cached_response_invalid and no decision is returned.
    """

    _require_key(key)
    if key.decision_kind != "candidate_decision":
        raise CacheKeyInvalid("lookup_candidate_decision needs a candidate_decision key.")
    tokens = _question_tokens(questions)
    if digest(tokens) != key.questions_digest:
        raise CacheKeyInvalid(
            "The key and the supplied questions disagree; this is a caller bug, never a stale "
            "entry.")
    row = LibraryRepository(connection).decision_cache_row(cache_key_digest(key))
    if row is None:
        return _miss("absent", False)
    if row["cache_key_version"] != CACHE_KEY_VERSION:
        return _miss("version_unsupported", True)
    decision = _decoded_decision(row["payload_json"])
    if decision is None:
        return _miss("cache_corrupt", True)
    if decision.payload_version != CACHE_PAYLOAD_VERSION:
        return _miss("version_unsupported", True)
    if (decision.candidate_id != key.candidate_id
            or decision.analysis_version != key.candidate_analysis_version):
        return _miss("cache_corrupt", True)
    by_dimension = {}
    for question in questions:
        by_dimension.setdefault(question.dimension, question)
    for entry in decision.dimensions:
        if entry.judgment is None:
            continue
        if entry.judgment.payload_version != CACHE_PAYLOAD_VERSION:
            return _miss("version_unsupported", True)
        question = by_dimension.get(entry.dimension)
        if question is None:
            return _miss("cache_corrupt", True)
        try:
            validate_response(question, entry.judgment.response_json)
        except JevResponseError:
            return _miss("cached_response_invalid", True)
    return CacheLookup(status=_HIT, reason=None, judgment=None, decision=decision,
                       stale_found=False, entry_created_at=row["created_at"])


def _miss(reason, stale_found) -> CacheLookup:
    return CacheLookup(status=_MISS, reason=reason, judgment=None, decision=None,
                       stale_found=stale_found, entry_created_at=None)


def _decoded_judgment(payload_json):
    """The stored CachedJudgment, or None when the row is not that shape."""

    try:
        parsed = json.loads(payload_json)
    except (TypeError, ValueError):
        return None
    if type(parsed) is not dict:
        return None
    try:
        _exact(parsed, _CACHED_JUDGMENT_FIELDS, "CachedJudgment")
        return CachedJudgment(**_cached_judgment_values(parsed))
    except _ShapeError:
        return None


def _decoded_decision(payload_json):
    """The stored CachedCandidateDecision, or None when the row is not that shape."""

    try:
        parsed = json.loads(payload_json)
    except (TypeError, ValueError):
        return None
    if type(parsed) is not dict:
        return None
    try:
        return CachedCandidateDecision.from_dict(parsed)
    except (CachePayloadInvalid, _ShapeError):
        return None


def invalidate_entry(connection, key) -> bool:
    """Delete exactly the row for one key; False when it had no row.

    Invalidation is explicit and idempotent: a lookup never deletes, so this is
    the documented way to drop a stale or invalid row.
    """

    _require_key(key)
    repository = LibraryRepository(connection)
    try:
        with transaction(connection):
            return repository.delete_decision_cache_key(cache_key_digest(key))
    except sqlite3.Error as error:
        raise coded_error(error) from error


def invalidate_candidate(connection, *, candidate_id) -> int:
    """Delete every cache row for one candidate, in both kinds and every palette.

    One transaction; returns the number of rows removed, and 0 on a second call.
    No sample row, no palette row and no model-version pin is touched, so this
    is the documented path after #22 re-scans or #71 prunes a sample.
    """

    if _blank(candidate_id):
        raise CacheKeyInvalid("candidate_id must be nonblank text.")
    repository = LibraryRepository(connection)
    try:
        with transaction(connection):
            return repository.delete_decision_cache_candidate(candidate_id)
    except sqlite3.Error as error:
        raise coded_error(error) from error


def prune_cache(connection, *, max_entries=DEFAULT_CACHE_MAX_ENTRIES,
                max_bytes=DEFAULT_CACHE_MAX_BYTES) -> PruneReport:
    """Delete the oldest rows until both bounds hold, and report what remains.

    The deletion order is exactly (created_at ASC, cache_key ASC) and no lookup
    or store ever prunes without being asked: this is the on-demand operation
    and store_judgment/store_candidate_decision own the inline one.
    """

    _require_bounds(max_entries, max_bytes)
    repository = LibraryRepository(connection)
    try:
        with transaction(connection):
            totals = repository.decision_cache_totals()
            entries, total = totals["entries"], totals["bytes"]
            deleted = 0
            for row in repository.decision_cache_oldest_keys():
                if entries <= max_entries and total <= max_bytes:
                    break
                repository.delete_decision_cache_key(row["cache_key"])
                entries -= 1
                total -= row["payload_bytes"]
                deleted += 1
    except sqlite3.Error as error:
        raise coded_error(error) from error
    return PruneReport(deleted=deleted, remaining_entries=entries, remaining_bytes=total,
                       max_entries=max_entries, max_bytes=max_bytes)


def cache_stats(connection) -> CacheStats:
    """The cache's row counts, stored payload bytes and timestamp extremes."""

    row = LibraryRepository(connection).decision_cache_stats()
    return CacheStats(entries=row["entries"], judgments=row["judgments"],
                      candidate_decisions=row["candidate_decisions"], bytes=row["bytes"],
                      oldest_created_at=row["oldest_created_at"],
                      newest_created_at=row["newest_created_at"])


def pinned_model_version(connection, *, interface_name, source, adapter_version,
                         prompt_version):
    """The stored ModelVersionPin for one interface identity, or None.

    The caller resolves the pin first and then builds a key with
    model_version = pin.model_version; with no pin no key can name a model
    version and the caller must call Jev.
    """

    for name, value in (("interface_name", interface_name), ("adapter_version", adapter_version),
                        ("prompt_version", prompt_version)):
        if _blank(value):
            raise CacheKeyInvalid(name + " must be nonblank text.")
    if source not in TRANSPORT_SOURCES:
        raise CacheKeyInvalid("source must be one of " + ", ".join(TRANSPORT_SOURCES) + ".")
    row = LibraryRepository(connection).decision_model_version_pin(
        interface_name=interface_name, source=source, adapter_version=adapter_version,
        prompt_version=prompt_version)
    return None if row is None else _pin(row)


def record_model_version(connection, run):
    """Record the one model version a #14 run observed, or return None.

    A cancelled run, a run whose source is not a transport, a run with zero or
    more than one distinct observed version and a run with no interface name
    write nothing. An observation updates observed_at and increments
    observation_count while first_observed_at stays put.
    """

    if type(run) is not JevScoringRun:
        raise CachePayloadInvalid("record_model_version needs a #14 JevScoringRun.")
    if run.cancelled or run.source not in TRANSPORT_SOURCES:
        return None
    if len(run.model_versions) != 1 or _blank(run.interface_name):
        return None
    now = utc_now()
    values = {
        "interface_name": run.interface_name, "source": run.source,
        "adapter_version": run.adapter_version, "prompt_version": run.prompt_version,
        "model_version": run.model_versions[0], "first_observed_at": now, "observed_at": now,
    }
    repository = LibraryRepository(connection)
    try:
        with transaction(connection):
            repository.upsert_decision_model_version_pin(values)
            row = repository.decision_model_version_pin(
                interface_name=run.interface_name, source=run.source,
                adapter_version=run.adapter_version, prompt_version=run.prompt_version)
    except sqlite3.Error as error:
        raise coded_error(error) from error
    return None if row is None else _pin(row)


def _pin(row) -> ModelVersionPin:
    return ModelVersionPin(interface_name=row["interface_name"], source=row["source"],
                           adapter_version=row["adapter_version"],
                           prompt_version=row["prompt_version"],
                           model_version=row["model_version"],
                           first_observed_at=row["first_observed_at"],
                           observed_at=row["observed_at"],
                           observation_count=row["observation_count"])
