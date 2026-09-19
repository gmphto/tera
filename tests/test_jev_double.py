"""The contract double proof: deterministic, scriptable and never a live claim.

The double is contract evidence only. These tests prove its determinism, every scripted answer
form, the echo of the asked question, the repeat-last-entry rule, the fixed fallback judgment and
that it never reads the clock, sleeps, opens a file or touches the network.
"""

import json
from pathlib import Path

import pytest

from backend.intelligence import jev_double as double_module
from backend.intelligence.decisions import MODEL_ABSTAINED, PROBABILITY_LABELS
from backend.intelligence.jev import (JevScoringRequest, JevTransport, JevTransportError,
                                      TRANSPORT_ERROR_CODES, score_questions)
from backend.intelligence.jev_double import (DEFAULT_CONFIDENCE, DEFAULT_MODEL_VERSION,
                                              DEFAULT_PROBABILITIES, DOUBLE_INTERFACE_NAME,
                                              DOUBLE_SOURCE, ENTRY_KINDS, JevContractDouble)
from backend.intelligence.questions import PROMPT_VERSION, JevQuestion, build_question
from backend.contracts import (AudioFeatures, AudioMetadata, MEASURES, Measurement, MusicalKey,
                               Sample)

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "jev"
DOUBLE_FILE = ROOT / "backend" / "intelligence" / "jev_double.py"
RATE = 48000
FRAMES = 48000
ANALYSIS = "jev-double-test-1"
PAYLOAD = ('{"dimension": "frequency", "evidence": [], "instruction": "synthetic",'
           ' "prompt_version": "jev-questions-v1", "question_id": "q-synthetic-double",'
           ' "withheld": []}')

QUESTION_CASES = json.loads((FIXTURES / "question-cases.json").read_text(encoding="utf-8"))
CASE_BY_NAME = {case["name"]: case for case in QUESTION_CASES}


def measure(name, value=None, confidence=None):
    if value is not None and confidence is None and name in ("fundamental", "tempo"):
        confidence = 0.90
    return Measurement(name=name, value=value, unit=MEASURES[name][0], confidence=confidence,
                       unavailable_reason="not_implemented" if value is None else None)


def musical_key(spec):
    if spec is None or spec.get("tonic") is None:
        return MusicalKey(tonic=None, mode=None, confidence=None,
                          unavailable_reason="insufficient_key_context")
    return MusicalKey(tonic=spec["tonic"], mode=spec["mode"], confidence=spec["confidence"])


def side_sample(spec, default_id, default_role):
    values = {name: measure(name) for name in MEASURES}
    for item in spec["measurements"]:
        values[item["name"]] = measure(item["name"], item.get("value"), item.get("confidence"))
    frames = spec.get("frame_count", FRAMES)
    return Sample(sample_id=spec.get("sample_id", default_id),
                  role=spec.get("role", default_role),
                  audio=AudioMetadata(local_path=spec.get("local_path",
                                                          "C:/synthetic-double/side.wav"),
                                      sample_rate_hz=RATE, channels=1, frame_count=frames,
                                      duration_ms=frames * 1000 / RATE),
                  features=AudioFeatures(measurements=tuple(values.values()),
                                         key=musical_key(spec.get("key"))),
                  analysis_version=spec.get("analysis_version", ANALYSIS))


def build(case):
    return build_question(case["dimension"],
                          side_sample(case["kick"], "kick-001", "kick"),
                          side_sample(case["candidate"], "bass-001", "bass"), song=None)


def questions():
    return [build(CASE_BY_NAME["full_frequency"]), build(CASE_BY_NAME["full_transient"])]


def send_batch(double, questions):
    requests = [JevScoringRequest("candidate-" + str(index), question)
                for index, question in enumerate(questions)]
    return score_questions(requests, transport=double, sleep=lambda seconds: None,
                           monotonic=lambda: 0.0)


def test_the_double_is_a_transport_with_the_documented_identity():
    double = JevContractDouble()
    assert isinstance(double, JevTransport)
    assert double.interface_name == DOUBLE_INTERFACE_NAME == "jev-contract-double"
    assert double.source == DOUBLE_SOURCE == "double"
    assert DEFAULT_MODEL_VERSION == "synthetic-jev-double-v1"
    assert DEFAULT_PROBABILITIES == (0.05, 0.05, 0.10, 0.60, 0.20)
    assert DEFAULT_CONFIDENCE == 0.60
    assert ENTRY_KINDS == ("judgment", "abstain", "text", "error")
    assert double.model_version == DEFAULT_MODEL_VERSION
    assert double.calls == [] and double.script == {} and double.default is None


def test_the_double_echoes_the_asked_ids_and_builds_a_labeled_judgment():
    double = JevContractDouble()
    body = json.loads(double.send(PAYLOAD, timeout_s=4.0))
    assert body["question_id"] == "q-synthetic-double"
    assert body["dimension"] == "frequency"
    assert body["prompt_version"] == "jev-questions-v1"
    assert body["label"] == "good"
    assert body["confidence"] == DEFAULT_CONFIDENCE
    assert body["model_version"] == DEFAULT_MODEL_VERSION
    assert body["unavailable_reason"] is None
    assert [item["label"] for item in body["probabilities"]] == list(PROBABILITY_LABELS)
    assert [item["probability"] for item in body["probabilities"]] == list(DEFAULT_PROBABILITIES)
    assert double.calls == [(PAYLOAD, 4.0)]


def test_the_judgment_label_is_the_first_contract_label_at_the_highest_probability():
    double = JevContractDouble(script={"q-synthetic-double": {
        "judgment": {"probabilities": [0.1, 0.1, 0.4, 0.4, 0.0], "confidence": 0.5}}})
    body = json.loads(double.send(PAYLOAD, timeout_s=1.0))
    assert body["label"] == "neutral"
    assert body["confidence"] == 0.5


def test_the_double_builds_an_abstention_with_the_models_own_version():
    double = JevContractDouble(script={"q-synthetic-double": {
        "abstain": {"model_version": "synthetic-model-9"}}})
    body = json.loads(double.send(PAYLOAD, timeout_s=1.0))
    assert body["label"] is None and body["confidence"] is None and body["probabilities"] is None
    assert body["unavailable_reason"] == MODEL_ABSTAINED
    assert body["model_version"] == "synthetic-model-9"


def test_a_scripted_malformed_text_is_returned_verbatim():
    text = '{"reply": "synthetic non-Jev response"}'
    double = JevContractDouble(script={"q-synthetic-double": {"text": text}})
    assert double.send(PAYLOAD, timeout_s=1.0) == text


@pytest.mark.parametrize("code", TRANSPORT_ERROR_CODES)
def test_a_scripted_transport_error_is_raised_with_its_code(code):
    double = JevContractDouble(script={"q-synthetic-double": {"error": code}})
    with pytest.raises(JevTransportError) as error:
        double.send(PAYLOAD, timeout_s=1.0)
    assert error.value.code == code
    assert double.calls == [(PAYLOAD, 1.0)]


@pytest.mark.parametrize("entry", [
    {"error": "synthetic_unknown"}, {"error": None}, {"error": 500}, {"text": 7},
    {"judgment": {"probabilities": [0.5, 0.5]}}, {"judgment": {}},
    {"judgment": {"probabilities": [0.1, 0.1, 0.1, 0.1, 0.6], "confidence": 2}},
    {"judgment": {"probabilities": [0.1, 0.1, 0.1, 0.1, "0.6"]}},
    {"abstain": {"model_version": ""}}, {"synthetic": {}}, {}, ["judgment"],
])
def test_a_script_entry_outside_the_contract_is_rejected_at_construction(entry):
    with pytest.raises(ValueError):
        JevContractDouble(script={"q-synthetic-double": entry})
    with pytest.raises(ValueError):
        JevContractDouble(default=entry)
    with pytest.raises(ValueError):
        JevContractDouble(script={"": entry})
    with pytest.raises(ValueError):
        JevContractDouble(script=[("q-synthetic-double", entry)])
    with pytest.raises(ValueError):
        JevContractDouble(model_version="  ")


def test_a_question_id_with_no_script_entry_uses_the_default_or_the_fixed_judgment():
    unscripted = JevContractDouble(script={"q-other": {"text": "never"}})
    body = json.loads(unscripted.send(PAYLOAD, timeout_s=1.0))
    assert body["label"] == "good" and body["model_version"] == DEFAULT_MODEL_VERSION
    declared = JevContractDouble(default={"judgment": {"probabilities": [0.6, 0.1, 0.1, 0.1, 0.1],
                                                       "confidence": 0.9,
                                                       "model_version": "synthetic-default"}})
    first = json.loads(declared.send(PAYLOAD, timeout_s=1.0))
    second = json.loads(declared.send(PAYLOAD, timeout_s=2.0))
    assert first == second
    assert first["label"] == "very-poor" and first["model_version"] == "synthetic-default"
    assert declared.calls == [(PAYLOAD, 1.0), (PAYLOAD, 2.0)]


def test_an_occurrence_beyond_the_scripted_entries_repeats_the_last_one():
    double = JevContractDouble(script={"q-synthetic-double": [
        {"judgment": {"probabilities": [0.6, 0.1, 0.1, 0.1, 0.1], "model_version": "synthetic-1"}},
        {"abstain": {"model_version": "synthetic-2"}},
    ]})
    kinds = [json.loads(double.send(PAYLOAD, timeout_s=float(index))).get("model_version")
             for index in range(4)]
    assert kinds == ["synthetic-1", "synthetic-2", "synthetic-2", "synthetic-2"]
    assert len(double.calls) == 4
    single = JevContractDouble(script={"q-synthetic-double": {"text": "only"}})
    assert [single.send(PAYLOAD, timeout_s=1.0) for _ in range(2)] == ["only", "only"]


def test_two_runs_over_equal_inputs_are_byte_identical():
    script = {"q-synthetic-double": [{"judgment": {"probabilities": [0.05, 0.05, 0.1, 0.6, 0.2]}},
                                     {"error": "timeout"}]}
    runs = []
    logs = []
    for _ in range(2):
        double = JevContractDouble(script=script)
        run = send_batch(double, questions())
        runs.append(run.to_json())
        logs.append(list(double.calls))
    assert runs[0] == runs[1]
    assert logs[0] == logs[1]
    assert len(logs[0]) == 2


def test_every_outcome_of_a_double_run_records_the_double_source():
    double = JevContractDouble(script={})
    run = send_batch(double, questions())
    assert run.source == DOUBLE_SOURCE and run.interface_name == DOUBLE_INTERFACE_NAME
    assert run.adapter_version == "jev-adapter-v1"
    assert run.prompt_version == PROMPT_VERSION
    assert [outcome.state for outcome in run.outcomes] == ["judged", "judged"]
    assert all(outcome.attempts == 1 for outcome in run.outcomes)
    assert run.model_versions == (DEFAULT_MODEL_VERSION,)


def test_the_double_never_sleeps_reads_the_clock_opens_a_file_or_touches_the_network():
    source = DOUBLE_FILE.read_text(encoding="utf-8")
    for forbidden in ("import time", "time.sleep", "time.monotonic", "import random", "random.",
                      "import socket", "import urllib", "import os", "pathlib", "subprocess",
                      "open(", "environ"):
        assert forbidden not in source, forbidden
    double = JevContractDouble()
    assert json.loads(double.send(PAYLOAD, timeout_s=0.0))["label"] == "good"
    requests = [JevScoringRequest("candidate", questions()[0])]
    slept = []
    run = score_questions(requests, transport=double, sleep=slept.append,
                          monotonic=lambda: 0.0)
    assert slept == [] and len(double.calls) == 2
    assert run.outcomes[-1].state == "judged"


def test_the_double_rejects_a_payload_that_is_not_the_adapter_payload():
    double = JevContractDouble()
    for broken in (None, 7, ["not", "text"], "not json", '{"question_id": 7}',
                   '{"question_id": "q", "dimension": "", "prompt_version": "v"}',
                   '{"question_id": "q", "dimension": "frequency"}'):
        with pytest.raises(ValueError):
            double.send(broken, timeout_s=1.0)
