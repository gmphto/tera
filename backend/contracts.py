"""Validated, local-only kick-to-bass contracts. See _docs/contracts.md."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, fields, is_dataclass
from pathlib import PurePosixPath, PureWindowsPath
from types import UnionType
from typing import Literal, Self, Union, get_args, get_origin, get_type_hints


SCHEMA_VERSION = "1.0"


class ContractError(ValueError):
    """A payload or constructor argument violates the contract."""


class UnsupportedVersionError(ContractError):
    """A consumer cannot interpret this schema version."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ContractError(message)


def _number(value: float, low: float | None, high: float | None, name: str) -> None:
    _require(low is None or value >= low, f"{name}: below minimum {low}")
    _require(high is None or value <= high, f"{name}: above maximum {high}")


def _optional(value: object, reason: str | None, name: str) -> None:
    _require((value is None) == (reason is not None),
             f"{name}: unknown values require a reason; known values forbid one")


def _typed(value: object, annotation: object, path: str, *, wire: bool) -> object:
    """The small JSON type vocabulary used by these dataclasses; no coercion."""
    origin, args = get_origin(annotation), get_args(annotation)
    if origin in (UnionType, Union):
        for option in args:
            try:
                return _typed(value, option, path, wire=wire)
            except UnsupportedVersionError:
                raise
            except ContractError:
                pass
        raise ContractError(f"{path}: invalid optional value")
    if origin is Literal:
        _require(any(type(value) is type(item) and value == item for item in args),
                 f"{path}: unsupported value {value!r}")
        return value
    if origin is tuple:
        _require(type(value) is (list if wire else tuple), f"{path}: expected array")
        return tuple(_typed(item, args[0], f"{path}[{i}]", wire=wire)
                     for i, item in enumerate(value))
    if isinstance(annotation, type) and issubclass(annotation, Model):
        if wire:
            return annotation.from_dict(value)
        _require(type(value) is annotation, f"{path}: expected {annotation.__name__}")
        if annotation is int:
            _require(abs(value) <= 2**53 - 1, f"{path}: exceeds JSON safe integer range")
        return value
    if annotation is float:
        _require(type(value) in (int, float), f"{path}: expected finite number")
        try:
            finite = math.isfinite(value)
        except OverflowError:
            finite = False
        _require(finite, f"{path}: expected finite number")
    elif annotation is str:
        _require(type(value) is str and bool(value.strip()), f"{path}: expected nonblank text")
    else:
        _require(type(value) is annotation, f"{path}: expected {annotation.__name__}")
    return value


class Model:
    """Immutable subclasses validate both direct construction and JSON input."""

    def __post_init__(self) -> None:
        hints = get_type_hints(type(self))
        for field in fields(self):
            _typed(getattr(self, field.name), hints[field.name], field.name, wire=False)
        if hasattr(self, "schema_version") and self.schema_version != SCHEMA_VERSION:
            raise UnsupportedVersionError(f"Unsupported schema version: {self.schema_version!r}")
        self._validate()

    def _validate(self) -> None:
        pass

    @classmethod
    def from_dict(cls, payload: dict) -> Self:
        _require(type(payload) is dict, f"{cls.__name__}: expected object")
        hints = get_type_hints(cls)
        names = {field.name for field in fields(cls)}
        if "schema_version" in names:
            _require("schema_version" in payload, "schema_version: required on the wire")
            if payload["schema_version"] != SCHEMA_VERSION:
                raise UnsupportedVersionError(
                    f"Unsupported schema version: {payload['schema_version']!r}")
        _require(not (payload.keys() - names), f"{cls.__name__}: unexpected fields")
        kwargs = {name: _typed(value, hints[name], name, wire=True)
                  for name, value in payload.items()}
        try:
            return cls(**kwargs)
        except TypeError as error:
            raise ContractError(f"{cls.__name__}: missing required fields") from error

    def to_dict(self) -> dict:
        def encode(value: object) -> object:
            if is_dataclass(value):
                return {field.name: encode(getattr(value, field.name)) for field in fields(value)}
            if isinstance(value, tuple):
                return [encode(item) for item in value]
            return value
        return encode(self)

    @classmethod
    def from_json(cls, value: str) -> Self:
        def pairs(items: list[tuple[str, object]]) -> dict:
            result = {}
            for key, item in items:
                _require(key not in result, f"Duplicate JSON field: {key}")
                result[key] = item
            return result

        def constant(value: str) -> None:
            raise ContractError(f"Non-finite JSON number: {value}")

        try:
            payload = json.loads(value, object_pairs_hook=pairs, parse_constant=constant)
        except (TypeError, json.JSONDecodeError) as error:
            raise ContractError("Invalid JSON") from error
        return cls.from_dict(payload)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), allow_nan=False, sort_keys=True, indent=2)


Role = Literal["kick", "bass", "sub-bass"]
Dimension = Literal["frequency", "transient", "tonal", "rhythmic", "texture", "arrangement"]
Label = Literal["very-poor", "poor", "neutral", "good", "excellent"]
Unit = Literal["Hz", "ms", "BPM", "linear", "ratio", "LUFS", "normalized"]

# name -> (unit, inclusive minimum, inclusive maximum). Frequency/tempo also
# have semantic checks below. Definitions are provisional until DSP feasibility.
MEASURES = {
    "fundamental": ("Hz", 0.0, None),
    "spectral_centroid": ("Hz", 0.0, None),
    "spectral_rolloff": ("Hz", 0.0, None),
    "rms": ("linear", 0.0, None),
    "peak": ("linear", 0.0, None),
    "crest_factor": ("ratio", 1.0, None),
    "loudness": ("LUFS", None, None),
    "transient_strength": ("normalized", 0.0, 1.0),
    "transient_position": ("ms", 0.0, None),
    "attack": ("ms", 0.0, None),
    "decay": ("ms", 0.0, None),
    "stereo_width": ("normalized", 0.0, 1.0),
    "tempo": ("BPM", 0.0, None),
    **{f"band_{band}": ("ratio", 0.0, 1.0)
       for band in ("sub", "bass", "low_mid", "mid", "high_mid", "high")},
}


@dataclass(frozen=True, kw_only=True)
class Measurement(Model):
    name: str
    value: float | None
    unit: Unit
    unavailable_reason: str | None = None
    confidence: float | None = None

    def _validate(self) -> None:
        _require(self.name in MEASURES, f"Unknown measurement: {self.name}")
        unit, low, high = MEASURES[self.name]
        _require(self.unit == unit, f"{self.name}: expected unit {unit}")
        _optional(self.value, self.unavailable_reason, self.name)
        if self.value is not None:
            _number(self.value, low, high, self.name)
            if self.name in ("fundamental", "tempo"):
                _require(self.value > 0, f"{self.name}: must be positive")
        else:
            _require(self.confidence is None, "Unknown measurement cannot claim confidence")
        if self.confidence is not None:
            _number(self.confidence, 0, 1, "measurement confidence")
        if self.name in ("fundamental", "tempo") and self.value is not None:
            _require(self.confidence is not None, f"{self.name}: reliability is required")


@dataclass(frozen=True, kw_only=True)
class MusicalKey(Model):
    tonic: Literal["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"] | None
    mode: Literal["major", "minor"] | None
    confidence: float | None
    unavailable_reason: str | None = None

    def _validate(self) -> None:
        _optional(self.tonic, self.unavailable_reason, "key")
        if self.tonic is None:
            _require(self.mode is None and self.confidence is None, "Unknown key has no mode/confidence")
        else:
            _require(self.mode is not None and self.confidence is not None, "Known key needs mode/confidence")
            _number(self.confidence, 0, 1, "key confidence")


@dataclass(frozen=True, kw_only=True)
class AudioMetadata(Model):
    local_path: str
    sample_rate_hz: int
    channels: int
    frame_count: int
    duration_ms: float

    def _validate(self) -> None:
        windows = PureWindowsPath(self.local_path)
        absolute = windows.is_absolute() or PurePosixPath(self.local_path).is_absolute()
        _require(absolute and not self.local_path.startswith(("\\\\", "//"))
                 and "://" not in self.local_path and "\0" not in self.local_path,
                 "local_path: expected absolute local filesystem path")
        _require(self.sample_rate_hz > 0, "sample_rate_hz: must be positive")
        _require(self.channels in (1, 2), "channels: only mono/stereo in this contract")
        _require(self.frame_count >= 0, "frame_count: must be nonnegative")
        _number(self.duration_ms, 0, None, "duration_ms")
        _require(math.isclose(self.duration_ms, self.frame_count * 1000 / self.sample_rate_hz,
                              rel_tol=0, abs_tol=0.001), "duration_ms: disagrees with frame count")


@dataclass(frozen=True, kw_only=True)
class AudioFeatures(Model):
    measurements: tuple[Measurement, ...]
    key: MusicalKey

    def _validate(self) -> None:
        names = [item.name for item in self.measurements]
        _require(len(names) == len(set(names)) and set(names) == set(MEASURES),
                 "features: provide every measurement exactly once, using explicit unknowns")


@dataclass(frozen=True, kw_only=True)
class Sample(Model):
    sample_id: str
    role: Role
    audio: AudioMetadata
    features: AudioFeatures
    analysis_version: str
    schema_version: str = SCHEMA_VERSION

    def _validate(self) -> None:
        for item in self.features.measurements:
            if item.value is None:
                continue
            if item.unit == "Hz":
                _number(item.value, 0, self.audio.sample_rate_hz / 2, item.name)
            if item.unit == "ms":
                _number(item.value, 0, self.audio.duration_ms, item.name)


@dataclass(frozen=True, kw_only=True)
class SongContext(Model):
    tempo: Measurement
    key: MusicalKey
    genre: str | None
    genre_unavailable_reason: str | None = None

    def _validate(self) -> None:
        _require(self.tempo.name == "tempo", "context tempo: expected tempo measurement")
        _optional(self.genre, self.genre_unavailable_reason, "genre")


@dataclass(frozen=True, kw_only=True)
class PaletteContext(Model):
    palette_id: str
    revision: int
    kick_id: str
    selected_bass_id: str | None
    song: SongContext
    schema_version: str = SCHEMA_VERSION

    def _validate(self) -> None:
        _require(self.revision >= 0, "revision: must be nonnegative")
        _require(self.kick_id != self.selected_bass_id, "Kick and bass IDs must differ")


@dataclass(frozen=True, kw_only=True)
class DimensionScore(Model):
    dimension: Dimension
    compatibility: float | None
    unavailable_reason: str | None = None

    def _validate(self) -> None:
        _optional(self.compatibility, self.unavailable_reason, "dimension compatibility")
        if self.compatibility is not None:
            _number(self.compatibility, 0, 1, "dimension compatibility")


@dataclass(frozen=True, kw_only=True)
class LabelProbability(Model):
    label: Label
    probability: float

    def _validate(self) -> None:
        _number(self.probability, 0, 1, "probability")


@dataclass(frozen=True, kw_only=True)
class JevJudgment(Model):
    dimension: Dimension
    label: Label | None
    confidence: float | None
    probabilities: tuple[LabelProbability, ...]
    model_version: str
    prompt_version: str
    unavailable_reason: str | None = None

    def _validate(self) -> None:
        _optional(self.label, self.unavailable_reason, "Jev judgment")
        if self.label is None:
            _require(self.confidence is None and not self.probabilities,
                     "Abstention cannot contain confidence or probabilities")
        else:
            _require(self.confidence is not None, "Jev judgment requires confidence")
            _number(self.confidence, 0, 1, "Jev confidence")
            labels = [item.label for item in self.probabilities]
            _require(len(labels) == 5 and set(labels) == set(get_args(Label)),
                     "Probabilities require each of the five labels exactly once")
            _require(math.isclose(sum(item.probability for item in self.probabilities),
                                 1, rel_tol=0, abs_tol=1e-6), "Probabilities must sum to one")


@dataclass(frozen=True, kw_only=True)
class RankedCandidate(Model):
    candidate_id: str
    analysis_version: str
    rank: int
    compatibility: float
    confidence: float
    similarity: float | None
    similarity_unavailable_reason: str | None
    dsp_dimensions: tuple[DimensionScore, ...]
    jev_judgments: tuple[JevJudgment, ...]
    reasons: tuple[str, ...]
    warnings: tuple[str, ...]

    def _validate(self) -> None:
        _require(self.rank >= 1, "rank: must be positive")
        _number(self.compatibility, 0, 1, "compatibility")
        _number(self.confidence, 0, 1, "confidence")
        _optional(self.similarity, self.similarity_unavailable_reason, "similarity")
        if self.similarity is not None:
            _number(self.similarity, 0, 1, "similarity")
        for name, items in (("DSP", self.dsp_dimensions), ("Jev", self.jev_judgments)):
            dimensions = [item.dimension for item in items]
            _require(len(dimensions) == len(set(dimensions)), f"Duplicate {name} dimensions")


@dataclass(frozen=True, kw_only=True)
class RecommendationBatch(Model):
    run_id: str
    palette: PaletteContext
    samples: tuple[Sample, ...]
    results: tuple[RankedCandidate, ...]
    ranking_version: str
    mode: Literal["dsp-only", "jev-only", "hybrid"]
    alternatives: tuple[str, ...]
    schema_version: str = SCHEMA_VERSION

    def _validate(self) -> None:
        samples = {sample.sample_id: sample for sample in self.samples}
        _require(len(samples) == len(self.samples), "Duplicate sample IDs")
        kick = samples.get(self.palette.kick_id)
        _require(kick is not None and kick.role == "kick", "Palette must reference a supplied kick")
        if self.palette.selected_bass_id is not None:
            bass = samples.get(self.palette.selected_bass_id)
            _require(bass is not None and bass.role in ("bass", "sub-bass"),
                     "Selected bass must reference a supplied bass")
        ids = [result.candidate_id for result in self.results]
        _require(len(ids) == len(set(ids)), "Duplicate ranked candidate IDs")
        _require([result.rank for result in self.results] == list(range(1, len(ids) + 1)),
                 "Results must be in contiguous one-based rank order")
        _require(len(self.alternatives) == len(set(self.alternatives))
                 and set(self.alternatives) <= set(ids), "Alternatives must reference unique ranked candidates")
        for result in self.results:
            sample = samples.get(result.candidate_id)
            _require(sample is not None and sample.role in ("bass", "sub-bass"),
                     "Ranked candidate must reference a supplied bass")
            _require(result.analysis_version == sample.analysis_version,
                     "Candidate analysis version mismatch")
            if self.mode == "dsp-only":
                _require(not result.jev_judgments, "DSP-only results cannot claim Jev evidence")
            elif self.mode == "jev-only":
                _require(not result.dsp_dimensions, "Jev-only results cannot claim DSP rule scores")
