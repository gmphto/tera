"""Consumer-facing validation and local JSON examples for issue #2."""

import copy
import json
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest

from backend.contracts import (
    ContractError,
    Measurement,
    MusicalKey,
    RecommendationBatch,
    Sample,
    UnsupportedVersionError,
)


FIXTURES = Path(__file__).parent / "fixtures" / "contracts"


def read(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def change(payload, path, value):
    result = copy.deepcopy(payload)
    target = result
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    return result


@pytest.mark.parametrize("name,model", [
    ("hybrid.json", RecommendationBatch),
    ("dsp-only.json", RecommendationBatch),
    ("silent-sample.json", Sample),
])
def test_public_examples_round_trip_without_losing_evidence(name, model):
    payload = read(name)
    value = model.from_dict(payload)
    assert value.to_dict() == payload
    assert model.from_json(value.to_json()) == value


@pytest.mark.parametrize("case", read("invalid-cases.json"), ids=lambda case: case["name"])
def test_invalid_wire_examples_fail_with_useful_errors(case):
    payload = change(read("hybrid.json"), case["path"], case["value"])
    with pytest.raises(ContractError, match=case["error"]):
        RecommendationBatch.from_dict(payload)


def test_end_to_end_input_and_output_identity_and_separate_scores():
    batch = RecommendationBatch.from_json((FIXTURES / "hybrid.json").read_text())
    kick, bass = batch.samples
    result, = batch.results
    assert (kick.role, bass.role) == ("kick", "bass")
    assert batch.palette.kick_id == kick.sample_id
    assert result.candidate_id == bass.sample_id
    assert result.analysis_version == bass.analysis_version
    assert batch.palette.revision == 3
    assert batch.ranking_version == "fixture-weights-1"
    assert (result.similarity, result.compatibility, result.confidence) == (0.41, 0.74, 0.68)
    assert result.jev_judgments[0].model_version == "fixture-model-1"
    assert result.jev_judgments[0].prompt_version == "fixture-prompt-1"
    assert kick.features.measurements[0].value == 55


def test_silence_is_zero_amplitude_and_unknown_pitch_not_a_poor_judgment():
    sample = Sample.from_dict(read("silent-sample.json"))
    values = {item.name: item for item in sample.features.measurements}
    assert values["rms"].value == values["peak"].value == 0
    assert values["fundamental"].value is None
    assert values["fundamental"].unavailable_reason == "silent_audio"
    assert sample.features.key.tonic is None
    assert sample.features.key.unavailable_reason == "silent_audio"


def test_dsp_only_does_not_manufacture_jev_provenance():
    dsp = RecommendationBatch.from_dict(read("dsp-only.json"))
    assert not dsp.results[0].jev_judgments
    assert dsp.results[0].similarity is None
    hybrid = RecommendationBatch.from_dict(read("hybrid.json"))
    with pytest.raises(ContractError, match="cannot claim Jev"):
        replace(dsp, results=hybrid.results)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf"), True, "0.8", 10**400])
def test_nonfinite_and_coerced_scores_are_rejected_in_memory_and_on_wire(value):
    batch = RecommendationBatch.from_dict(read("hybrid.json"))
    with pytest.raises(ContractError):
        replace(batch.results[0], compatibility=value)
    with pytest.raises(ContractError):
        RecommendationBatch.from_dict(change(read("hybrid.json"), ["results", 0, "compatibility"], value))


@pytest.mark.parametrize("field", ["compatibility", "confidence", "similarity"])
@pytest.mark.parametrize("value", [-0.01, 1.01])
def test_score_ranges(field, value):
    with pytest.raises(ContractError):
        RecommendationBatch.from_dict(change(read("hybrid.json"), ["results", 0, field], value))


@pytest.mark.parametrize("token", ["NaN", "Infinity", "-Infinity", "1e400"])
def test_invalid_json_numbers_fail(token):
    payload = json.dumps(read("hybrid.json")).replace('"compatibility": 0.74', '"compatibility": ' + token)
    with pytest.raises(ContractError):
        RecommendationBatch.from_json(payload)


@pytest.mark.parametrize("payload", ['{"schema_version":"1.0","schema_version":"1.0"}', '{', '[]', 'null'])
def test_invalid_json_structure_and_duplicate_keys(payload):
    with pytest.raises(ContractError):
        RecommendationBatch.from_json(payload)


def test_schema_evolution_fails_explicitly_and_unknown_fields_are_not_ignored():
    payload = read("hybrid.json")
    with pytest.raises(UnsupportedVersionError):
        RecommendationBatch.from_dict({**payload, "schema_version": "1.1"})
    del payload["schema_version"]
    with pytest.raises(ContractError, match="required on the wire"):
        RecommendationBatch.from_dict(payload)
    payload = read("hybrid.json")
    with pytest.raises(ContractError, match="unexpected fields"):
        RecommendationBatch.from_dict({**payload, "new_field": 1})
    with pytest.raises(UnsupportedVersionError):
        replace(RecommendationBatch.from_dict(payload), schema_version="2.0")


def test_missing_required_field_and_wrong_nested_object():
    payload = read("hybrid.json")
    del payload["run_id"]
    with pytest.raises(ContractError, match="missing required fields"):
        RecommendationBatch.from_dict(payload)
    with pytest.raises(ContractError):
        RecommendationBatch.from_dict(change(read("hybrid.json"), ["samples", 0, "audio"], []))


def test_constructor_uses_same_validation_and_models_are_immutable():
    value = Measurement(name="peak", value=0.5, unit="linear")
    with pytest.raises(FrozenInstanceError):
        value.value = 3
    with pytest.raises(ContractError, match="expected unit"):
        Measurement(name="peak", value=0.5, unit="Hz")
    with pytest.raises(ContractError):
        replace(RecommendationBatch.from_dict(read("hybrid.json")), samples=read("hybrid.json")["samples"])


def test_unknown_context_and_known_context_both_round_trip():
    batch = RecommendationBatch.from_dict(read("hybrid.json"))
    assert batch.palette.song.tempo.value is None
    assert batch.palette.song.key.tonic is None
    tempo = Measurement(name="tempo", value=120, unit="BPM", confidence=1)
    key = MusicalKey(tonic="A", mode="minor", confidence=1)
    song = replace(batch.palette.song, tempo=tempo, key=key, genre="house", genre_unavailable_reason=None)
    updated = replace(batch, palette=replace(batch.palette, song=song, revision=4))
    assert RecommendationBatch.from_json(updated.to_json()) == updated


@pytest.mark.parametrize("kwargs", [
    {"name": "peak", "value": None, "unit": "linear"},
    {"name": "peak", "value": 1, "unit": "linear", "unavailable_reason": "unknown"},
    {"name": "fundamental", "value": 55, "unit": "Hz"},
    {"name": "tempo", "value": 0, "unit": "BPM", "confidence": 1},
    {"name": "peak", "value": None, "unit": "linear", "unavailable_reason": "silent", "confidence": 1},
])
def test_missing_evidence_cannot_claim_a_value_or_reliability(kwargs):
    with pytest.raises(ContractError):
        Measurement(**kwargs)


@pytest.mark.parametrize("path,value", [
    (["samples", 0, "audio", "duration_ms"], 501),
    (["samples", 0, "audio", "channels"], 3),
    (["samples", 0, "audio", "frame_count"], True),
    (["samples", 0, "audio", "sample_rate_hz"], 0),
    (["samples", 0, "features", "measurements", 0, "value"], 25000),
    (["samples", 0, "features", "measurements", 8, "value"], 501),
    (["palette", "revision"], -1),
    (["palette", "kick_id"], "bass-001"),
    (["palette", "selected_bass_id"], "absent"),
    (["results", 0, "candidate_id"], "kick-001"),
    (["alternatives"], ["absent"]),
    (["results", 0, "jev_judgments", 0, "model_version"], " "),
    (["results", 0, "jev_judgments", 0, "confidence"], -0.1),
    (["results", 0, "jev_judgments", 0, "probabilities", 0, "label"], "good"),
])
def test_inconsistent_evidence_identity_and_metadata_are_rejected(path, value):
    with pytest.raises(ContractError):
        RecommendationBatch.from_dict(change(read("hybrid.json"), path, value))


def test_missing_feature_is_explicit_and_duplicate_candidates_are_rejected():
    payload = read("hybrid.json")
    payload["samples"][0]["features"]["measurements"].pop()
    with pytest.raises(ContractError, match="every measurement"):
        RecommendationBatch.from_dict(payload)
    payload = read("hybrid.json")
    payload["results"].append(copy.deepcopy(payload["results"][0]))
    with pytest.raises(ContractError, match="Duplicate ranked"):
        RecommendationBatch.from_dict(payload)


def test_empty_results_and_explicit_jev_abstention_round_trip():
    batch = RecommendationBatch.from_dict(read("hybrid.json"))
    empty = replace(batch, results=(), alternatives=())
    assert RecommendationBatch.from_json(empty.to_json()) == empty
    judgment = replace(batch.results[0].jev_judgments[0], label=None, confidence=None,
                       probabilities=(), unavailable_reason="insufficient_context")
    result = replace(batch.results[0], jev_judgments=(judgment,))
    updated = replace(batch, results=(result,))
    assert RecommendationBatch.from_json(updated.to_json()) == updated


def integer_model(field):
    batch = RecommendationBatch.from_dict(read("hybrid.json"))
    if field == "revision":
        return batch.palette
    if field == "rank":
        return batch.results[0]
    return batch.samples[0].audio


def construct_integer(model, field, value, route):
    if route == "constructor":
        return replace(model, **{field: value})
    payload = {**model.to_dict(), field: value}
    if route == "dict":
        return type(model).from_dict(payload)
    return type(model).from_json(json.dumps(payload))


@pytest.mark.parametrize("route", ["constructor", "dict", "json"])
@pytest.mark.parametrize("field", ["revision", "rank", "sample_rate_hz", "channels", "frame_count"])
@pytest.mark.parametrize("value", [2**53, -(2**53), 9007199254740993, 10**400, -(10**400)],
                         ids=["above-safe", "below-safe", "qa-precision", "qa-overflow", "negative-overflow"])
def test_unsafe_integers_fail_before_domain_validation(field, value, route):
    with pytest.raises(ContractError, match="exceeds JSON safe integer range"):
        construct_integer(integer_model(field), field, value, route)


@pytest.mark.parametrize("route", ["constructor", "dict", "json"])
@pytest.mark.parametrize("field", ["revision", "rank", "sample_rate_hz", "channels", "frame_count"])
@pytest.mark.parametrize("value", [True, False, 1.0, "1"])
def test_integer_fields_reject_coercion(field, value, route):
    with pytest.raises(ContractError, match="expected int"):
        construct_integer(integer_model(field), field, value, route)


@pytest.mark.parametrize("route", ["constructor", "dict", "json"])
@pytest.mark.parametrize("field", ["revision", "rank", "sample_rate_hz", "frame_count"])
def test_safe_integer_upper_boundary_round_trips_exactly(field, route):
    value = 2**53 - 1
    model = integer_model(field)
    if field == "sample_rate_hz":
        model = replace(model, frame_count=0, duration_ms=0)
    elif field == "frame_count":
        # Change the duration with the count to preserve metadata consistency.
        model = replace(model, sample_rate_hz=1000, frame_count=value, duration_ms=float(value))
    result = construct_integer(model, field, value, route)
    assert getattr(result, field) == value
    assert type(result).from_json(result.to_json()) == result


@pytest.mark.parametrize("route", ["constructor", "dict", "json"])
@pytest.mark.parametrize("field", ["revision", "rank", "sample_rate_hz", "channels", "frame_count"])
def test_safe_negative_boundary_reaches_domain_validation(field, route):
    # Integer safety includes both endpoints, but these domain fields forbid negatives.
    with pytest.raises(ContractError, match="must be|only mono/stereo"):
        construct_integer(integer_model(field), field, -(2**53 - 1), route)
