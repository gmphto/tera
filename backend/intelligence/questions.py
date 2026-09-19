"""Narrow typed Jev questions from schema-1 facts; see _docs/jev-questions.md.

This module asks one question per contract `Dimension` about one kick-to-bass
candidate, using only supplied schema-1 facts and optional song context. It never
asks Jev to measure audio, never opens a file, never touches the network and
never imports the #14 transport. Every fact is a contract fact: unknown or
unreliable evidence is withheld with a documented reason instead of being
presented as a value or defaulted, and a dimension whose required evidence is
missing returns an explicit `UnavailableQuestion` instead of a judgment.

The question payload carries `question_id`, `dimension`, `prompt_version`,
`instruction`, `evidence` and `withheld` and nothing else: no sample ID, no
local path, no filename, no frame count and no audio. `build_question` is pure,
so identical inputs return equal questions with an equal `question_id`.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import get_args

from backend.contracts import MEASURES, Dimension, Sample, SongContext
from backend.palette.compatibility import CONFIDENCE_THRESHOLD

# The prompt version owns the per-dimension instruction wording below; any
# wording change requires a new value. Responses must echo it exactly.
PROMPT_VERSION = "jev-questions-v1"

# The six schema-1 dimension literals in the contract literal's own order.
DIMENSIONS = get_args(Dimension)

# The six contract band ratios in MEASURES order.
BAND_NAMES = ("band_sub", "band_bass", "band_low_mid", "band_mid", "band_high_mid", "band_high")

# Documented non-measurement evidence names alongside every MEASURES name.
CONTEXT_NAMES = ("key", "role", "genre")

# Documented withheld reasons.
UNKNOWN = "unknown"
BELOW_RELIABILITY_THRESHOLD = "below_reliability_threshold"
NOT_SUPPLIED = "not_supplied"
WITHHELD_REASONS = (UNKNOWN, BELOW_RELIABILITY_THRESHOLD, NOT_SUPPLIED)

# A dimension that needs song context and has none.
SONG_CONTEXT_ABSENT = "song_context_absent"

INPUT_ERROR_CODES = ("invalid_dimension", "invalid_kick", "invalid_candidate", "invalid_context")

# Required evidence per dimension, in the documented check order. The
# fundamental entries are reliability guards: an unknown fundamental is
# withheld but does not block, while a known fundamental below the threshold
# blocks with the matching `{side}_f0_unreliable` code.
REQUIRED_EVIDENCE = {
    "frequency": (tuple(("kick", name) for name in BAND_NAMES)
                  + tuple(("candidate", name) for name in BAND_NAMES)),
    "transient": (("kick", "attack"), ("kick", "decay"), ("kick", "transient_strength"),
                  ("candidate", "transient_position")),
    "tonal": (("kick", "key"), ("kick", "fundamental"),
              ("candidate", "key"), ("candidate", "fundamental")),
    "rhythmic": (("candidate", "tempo"), ("song", "tempo")),
    "texture": (("kick", "spectral_centroid"), ("kick", "spectral_rolloff"),
                ("candidate", "spectral_centroid"), ("candidate", "spectral_rolloff")),
    "arrangement": (("kick", "role"), ("candidate", "role")),
}

# Optional evidence per dimension, in the documented presentation order. These
# facts are presented when usable and recorded as withheld when not; they never
# block a question.
OPTIONAL_EVIDENCE = {
    "frequency": (("kick", "fundamental"), ("candidate", "fundamental"),
                  ("kick", "loudness"), ("candidate", "loudness")),
    "transient": (("kick", "crest_factor"), ("candidate", "crest_factor"),
                  ("kick", "loudness"), ("candidate", "loudness")),
    "tonal": (("song", "key"), ("song", "genre")),
    "rhythmic": (("song", "genre"), ("kick", "transient_position")),
    "texture": (("kick", "loudness"), ("candidate", "loudness"),
                ("kick", "stereo_width"), ("candidate", "stereo_width")),
    "arrangement": (("song", "genre"), ("song", "key")),
}

# Every instruction asks for the five-label judgment, the five probabilities and
# the model's own confidence and for nothing else.
_INSTRUCTION_TAIL = (
    "Answer with exactly one of the five labels very-poor, poor, neutral, good or excellent,"
    " then the probability of each of those five labels as five numbers that sum to 1, then"
    " your own confidence between 0 and 1. Use only the facts in this payload: do not measure"
    " audio or estimate any measurement. Nothing else is requested."
)

INSTRUCTIONS = {
    "frequency": ("Judge how well the kick and the bass candidate share the frequency spectrum"
                  " and where they compete for the same band. " + _INSTRUCTION_TAIL),
    "transient": ("Judge how well the bass candidate's onset fits inside the kick's transient"
                  " and decay envelope. " + _INSTRUCTION_TAIL),
    "tonal": ("Judge how well the bass candidate's key fits the kick's key and, when supplied,"
              " the song's key. " + _INSTRUCTION_TAIL),
    "rhythmic": ("Judge how well the bass candidate's tempo fits the song's tempo. "
                 + _INSTRUCTION_TAIL),
    "texture": ("Judge how well the bass candidate's spectral brightness and roll-off complement"
                " the kick's. " + _INSTRUCTION_TAIL),
    "arrangement": ("Judge how well the bass candidate fills the arrangement role left open by"
                    " the kick, given the declared roles and the optional genre. " + _INSTRUCTION_TAIL),
}


class QuestionInputError(ValueError):
    """Malformed question input; inspect code, not message text."""

    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class QuestionFact:
    """One supplied contract fact; measurement facts carry their contract unit."""

    side: str
    name: str
    value: float | str
    unit: str | None = None


@dataclass(frozen=True)
class WithheldFact:
    """One fact the question does not present, with a documented withheld reason."""

    side: str
    name: str
    reason: str


@dataclass(frozen=True)
class JevQuestion:
    """One accepted question; its JSON payload is the product's prompt."""

    question_id: str
    dimension: str
    prompt_version: str
    instruction: str
    evidence: tuple[QuestionFact, ...]
    withheld: tuple[WithheldFact, ...]

    def to_dict(self) -> dict:
        return {
            "question_id": self.question_id,
            "dimension": self.dimension,
            "prompt_version": self.prompt_version,
            "instruction": self.instruction,
            "evidence": [{"side": fact.side, "name": fact.name, "value": fact.value,
                          "unit": fact.unit} for fact in self.evidence],
            "withheld": [{"side": item.side, "name": item.name, "reason": item.reason}
                         for item in self.withheld],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), allow_nan=False, sort_keys=True)


@dataclass(frozen=True)
class UnavailableQuestion:
    """A question this product does not ask, with the first failure it found."""

    dimension: str
    code: str
    withheld: tuple[WithheldFact, ...]

    def to_dict(self) -> dict:
        return {"dimension": self.dimension, "code": self.code,
                "withheld": [{"side": item.side, "name": item.name, "reason": item.reason}
                             for item in self.withheld]}

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), allow_nan=False, sort_keys=True)


def _code(side, name, reason):
    """Stable `{side}_{fact}_unknown|_unreliable` code; fundamental is `f0`."""
    token = "f0" if name == "fundamental" else name
    return f"{side}_{token}_{'unknown' if reason == UNKNOWN else 'unreliable'}"


def _unavailable_codes():
    """Every code a missing or unreliable required fact can produce."""
    codes = {SONG_CONTEXT_ABSENT}
    for dimension in DIMENSIONS:
        for side, name in REQUIRED_EVIDENCE[dimension]:
            if name == "role":
                continue
            if name == "fundamental":
                codes.add(_code(side, name, BELOW_RELIABILITY_THRESHOLD))
            else:
                codes.add(_code(side, name, UNKNOWN))
                codes.add(_code(side, name, BELOW_RELIABILITY_THRESHOLD))
    codes.update(f"{side}_band_energy_zero" for side in ("kick", "candidate"))
    return frozenset(codes)


QUESTION_UNAVAILABLE_CODES = _unavailable_codes()


def _measurements(sample):
    return {item.name: item for item in sample.features.measurements}


def _validated(value, kind, code):
    if type(value) is not kind:
        raise QuestionInputError(code, f"Expected a validated {kind.__name__}.")
    try:
        kind.from_dict(value.to_dict())
    except (TypeError, ValueError, AttributeError) as error:
        raise QuestionInputError(code, f"Invalid {kind.__name__} contract.") from error


def _validate_inputs(dimension, kick, candidate, song):
    if type(dimension) is not str or dimension not in DIMENSIONS:
        raise QuestionInputError("invalid_dimension", f"Unknown question dimension: {dimension!r}.")
    _validated(kick, Sample, "invalid_kick")
    if kick.role != "kick":
        raise QuestionInputError("invalid_kick", f"Selected kick role is {kick.role!r}, not 'kick'.")
    _validated(candidate, Sample, "invalid_candidate")
    if candidate.role not in ("bass", "sub-bass"):
        raise QuestionInputError("invalid_candidate",
                                 f"Candidate role is {candidate.role!r}, not bass/sub-bass.")
    if candidate.sample_id == kick.sample_id:
        raise QuestionInputError("invalid_candidate", "A candidate cannot share the kick's sample ID.")
    if song is not None:
        _validated(song, SongContext, "invalid_context")


def _read_fact(side, name, records, *, guard=False):
    """Return (fact, withheld, failure) for one documented evidence entry."""
    record = records[side]
    if record is None:
        return None, WithheldFact(side, name, NOT_SUPPLIED), SONG_CONTEXT_ABSENT
    if name == "role":
        return QuestionFact(side, name, record.role, None), None, None
    if name == "key":
        key = record.key if side == "song" else record.features.key
        if key.tonic is None:
            return None, WithheldFact(side, name, UNKNOWN), _code(side, name, UNKNOWN)
        if key.confidence < CONFIDENCE_THRESHOLD:
            return None, WithheldFact(side, name, BELOW_RELIABILITY_THRESHOLD), _code(
                side, name, BELOW_RELIABILITY_THRESHOLD)
        return QuestionFact(side, name, f"{key.tonic} {key.mode}", None), None, None
    if name == "genre":
        if record.genre is None:
            return None, WithheldFact(side, name, UNKNOWN), _code(side, name, UNKNOWN)
        return QuestionFact(side, name, record.genre, None), None, None
    measurement = record.tempo if side == "song" else _measurements(record)[name]
    if measurement.value is None:
        code = None if guard else _code(side, name, UNKNOWN)
        return None, WithheldFact(side, name, UNKNOWN), code
    if measurement.confidence is not None and measurement.confidence < CONFIDENCE_THRESHOLD:
        return None, WithheldFact(side, name, BELOW_RELIABILITY_THRESHOLD), _code(
            side, name, BELOW_RELIABILITY_THRESHOLD)
    return QuestionFact(side, name, measurement.value, measurement.unit), None, None


def _zero_energy(records, side):
    """True when that side's six required band ratios are all usable and sum to zero."""
    measurements = _measurements(records[side])
    for name in BAND_NAMES:
        measurement = measurements[name]
        if measurement.value is None:
            return False
        if measurement.confidence is not None and measurement.confidence < CONFIDENCE_THRESHOLD:
            return False
    return sum(measurements[name].value for name in BAND_NAMES) == 0


def _collect(dimension, records):
    """Presented facts, withheld facts and the first required failure, in order."""
    evidence, withheld, failure = [], [], None
    for side, name in REQUIRED_EVIDENCE[dimension]:
        fact, missing, code = _read_fact(side, name, records, guard=(name == "fundamental"))
        if fact is not None:
            evidence.append(fact)
        if missing is not None:
            withheld.append(missing)
        if code is not None and failure is None:
            failure = code
        if name == BAND_NAMES[-1] and failure is None and _zero_energy(records, side):
            failure = f"{side}_band_energy_zero"
    for side, name in OPTIONAL_EVIDENCE[dimension]:
        fact, missing, _ = _read_fact(side, name, records)
        if fact is not None:
            evidence.append(fact)
        if missing is not None:
            withheld.append(missing)
    return evidence, withheld, failure


def _fact_digest(fact):
    value = float(fact.value) if type(fact.value) in (int, float) else fact.value
    return json.dumps([fact.side, fact.name, value, fact.unit], allow_nan=False)


def _withheld_digest(item):
    return json.dumps([item.side, item.name, item.reason], allow_nan=False)


def _question_id(dimension, evidence, withheld):
    """Opaque digest of the dimension, the prompt version and the question's own facts."""
    canonical = json.dumps({"dimension": dimension, "prompt_version": PROMPT_VERSION,
                            "evidence": sorted(_fact_digest(fact) for fact in evidence),
                            "withheld": sorted(_withheld_digest(item) for item in withheld)},
                           allow_nan=False, sort_keys=True)
    return "q-" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_question(dimension: str, kick: Sample, candidate: Sample, *,
                   song: SongContext | None = None) -> JevQuestion | UnavailableQuestion:
    """Build the one question for a dimension, or an explicit unavailable record.

    Inputs are revalidated schema-1 records: a non-`kick` selected record, a
    candidate that is not `bass`/`sub-bass`, a candidate sharing the kick's
    sample ID or a malformed song context raise QuestionInputError with a stable
    code. Nothing is measured, defaulted or invented, inputs are never mutated,
    and the same inputs always produce an equal question and question ID.
    """
    _validate_inputs(dimension, kick, candidate, song)
    records = {"kick": kick, "candidate": candidate, "song": song}
    evidence, withheld, failure = _collect(dimension, records)
    if failure is not None:
        return UnavailableQuestion(dimension=dimension, code=failure, withheld=tuple(withheld))
    return JevQuestion(question_id=_question_id(dimension, evidence, withheld),
                       dimension=dimension, prompt_version=PROMPT_VERSION,
                       instruction=INSTRUCTIONS[dimension], evidence=tuple(evidence),
                       withheld=tuple(withheld))
