"""Synthetic Jev response fixtures; no real Jev call, transport or network use.

The fixtures declare every response and its expected stable error code. Accepted
cases assert the exact schema-1 JevJudgment the validator returns, including the
contradiction case where a judgment disagrees with the supplied measured facts
and is still returned unchanged.
"""

import json
import math
from dataclasses import replace
from pathlib import Path
from typing import get_args

import pytest

from backend.contracts import (AudioFeatures, AudioMetadata, JevJudgment, Label, MEASURES,
                               Measurement, MusicalKey, Sample, SongContext)
from backend.intelligence.decisions import (JevResponseError, MODEL_ABSTAINED,
                                            PROBABILITY_LABELS, PROBABILITY_SUM_TOLERANCE,
                                            RESPONSE_ERROR_CODES, RESPONSE_FIELDS, validate_response)
from backend.intelligence.questions import (DIMENSIONS, OPTIONAL_EVIDENCE, PROMPT_VERSION,
                                            JevQuestion, UnavailableQuestion, build_question)
from backend.palette.ranking import RankingPolicy, rank_candidates


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "jev"
DOCUMENT = ROOT / "_docs" / "jev-questions.md"
DECISIONS_FILE = ROOT / "backend" / "intelligence" / "decisions.py"

TICK = chr(96)
RATE = 48000
FRAMES = 48000
ANALYSIS = "jev-test-1"
LABELS = list(PROBABILITY_LABELS)
STANDARD = [0.05, 0.05, 0.10, 0.60, 0.20]

QUESTION_CASES = json.loads((FIXTURES / "question-cases.json").read_text(encoding="utf-8"))
CASE_BY_NAME = {case["name"]: case for case in QUESTION_CASES}
RESPONSE_CASES = json.loads((FIXTURES / "response-cases.json").read_text(encoding="utf-8"))
MALFORMED_CASES = json.loads((FIXTURES / "malformed-responses.json").read_text(encoding="utf-8"))


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
    return Sample(sample_id=spec.get("sample_id", default_id), role=spec.get("role", default_role),
                  audio=AudioMetadata(local_path=spec.get("local_path", "C:/synthetic-jev/side.wav"),
                                      sample_rate_hz=RATE, channels=1, frame_count=frames,
                                      duration_ms=frames * 1000 / RATE),
                  features=AudioFeatures(measurements=tuple(values.values()),
                                         key=musical_key(spec.get("key"))),
                  analysis_version=spec.get("analysis_version", ANALYSIS))


def song_context(spec):
    if spec is None:
        return None
    return SongContext(tempo=measure("tempo", spec["tempo"].get("value"),
                                     spec["tempo"].get("confidence")),
                       key=musical_key(spec.get("key")), genre=spec.get("genre"),
                       genre_unavailable_reason=None if spec.get("genre") is not None else "not_provided")


def pair(case):
    return (side_sample(case["kick"], "kick-001", "kick"),
            side_sample(case["candidate"], "bass-001", "bass"), song_context(case["song"]))


def build(case):
    kick, candidate, song = pair(case)
    return build_question(case["dimension"], kick, candidate, song=song)


def substitute(value, question):
    """Replace the fixture placeholders with the built question's own values."""
    if type(value) is str:
        return (value.replace("<question_id>", question.question_id)
                     .replace("<prompt_version>", question.prompt_version)
                     .replace("<dimension>", question.dimension))
    return json.loads(substitute(json.dumps(value), question))


def response_payload(question, label="good", values=None, confidence=0.77,
                     model_version="synthetic-model-1", reason=None):
    values = STANDARD if values is None else values
    return {"question_id": question.question_id, "dimension": question.dimension, "label": label,
            "probabilities": [{"label": name, "probability": value}
                              for name, value in zip(LABELS, values)],
            "confidence": confidence, "model_version": model_version,
            "prompt_version": question.prompt_version, "unavailable_reason": reason}


def abstention_payload(question, reason=MODEL_ABSTAINED, probabilities=None, confidence=None,
                       model_version="synthetic-model-1"):
    return {"question_id": question.question_id, "dimension": question.dimension, "label": None,
            "probabilities": probabilities, "confidence": confidence, "model_version": model_version,
            "prompt_version": question.prompt_version, "unavailable_reason": reason}


def question_argument(case):
    if "question_value" in case:
        return {"none": None, "mapping": {}, "text": "frequency"}[case["question_value"]]
    return build(CASE_BY_NAME[case["question_case"]])


def response_argument(case, question):
    if "response_text" in case:
        return substitute(case["response_text"], question) if isinstance(question, JevQuestion) \
            else case["response_text"]
    if "response_value" in case:
        return {"none": None, "list": [1, 2], "number": 7}[case["response_value"]]
    return substitute(case["response"], question) if isinstance(question, JevQuestion) \
        else case["response"]


def table_rows(text, heading):
    lines = text.splitlines()
    rows = []
    for line in lines[lines.index(heading) + 1:]:
        if line.startswith("## "):
            break
        if line.startswith("|") and not set(line) <= set("|- "):
            rows.append([cell.strip() for cell in line.strip().strip("|").split("|")])
    return rows


@pytest.mark.parametrize("case", RESPONSE_CASES, ids=lambda case: case["name"])
def test_accepted_response_cases(case):
    question = build(CASE_BY_NAME[case["question_case"]])
    assert type(question) is JevQuestion
    judgment = validate_response(question, response_argument(case, question))
    expects = case["expects"]
    assert type(judgment) is JevJudgment
    assert judgment.dimension == question.dimension
    assert judgment.model_version == expects["model_version"]
    assert judgment.prompt_version == question.prompt_version
    assert judgment.prompt_version == PROMPT_VERSION
    if expects["outcome"] == "abstention":
        assert judgment.label is None and judgment.confidence is None
        assert judgment.probabilities == ()
        assert judgment.unavailable_reason == MODEL_ABSTAINED
    else:
        assert judgment.label == expects["label"]
        assert judgment.confidence == expects["confidence"]
        assert judgment.unavailable_reason is None
        assert {item.label: item.probability for item in judgment.probabilities} == \
            expects["probabilities"]
        assert [item.label for item in judgment.probabilities] == list(PROBABILITY_LABELS)
    assert JevJudgment.from_json(judgment.to_json()) == judgment


@pytest.mark.parametrize("case", MALFORMED_CASES, ids=lambda case: case["name"])
def test_malformed_response_cases(case):
    question = question_argument(case)
    argument = response_argument(case, question)
    with pytest.raises(JevResponseError) as error:
        validate_response(question, argument)
    assert error.value.code == case["expects"]["code"]
    assert error.value.code in RESPONSE_ERROR_CODES
    assert isinstance(error.value, ValueError) and error.value.code


def test_accepted_response_cases_cover_the_criterion_question_list():
    """A question with a withheld optional fact and one with no song context are answered."""
    referenced = {case["question_case"] for case in RESPONSE_CASES}
    no_song, withheld_optional = set(), set()
    for case in QUESTION_CASES:
        if case["expects"]["outcome"] != "question":
            continue
        question = build(case)
        assert type(question) is JevQuestion
        withheld = {(item.side, item.name) for item in question.withheld}
        if case["song"] is None:
            no_song.add(case["name"])
        if withheld & set(OPTIONAL_EVIDENCE[case["dimension"]]):
            withheld_optional.add(case["name"])
    assert no_song and withheld_optional
    assert no_song & referenced, "an accepted response case must answer a no-song-context question"
    assert withheld_optional & referenced, "an accepted response case must answer a question with a withheld optional fact"
    assert any(case["expects"]["outcome"] == "abstention" for case in RESPONSE_CASES)
    assert "frequency_low_band_contradiction" in referenced

    def values(case):
        expectations = case["expects"]
        return None if expectations["outcome"] != "judgment" else expectations["probabilities"]

    def tied_argmax(case):
        numbers = values(case)
        return numbers is not None and list(numbers.values()).count(max(numbers.values())) > 1

    def out_of_contract_order(case):
        entries = case.get("response", {}).get("probabilities")
        return (isinstance(entries, list) and bool(entries)
                and [entry["label"] for entry in entries] != LABELS)

    assert any(tied_argmax(case) for case in RESPONSE_CASES)
    assert any(out_of_contract_order(case) for case in RESPONSE_CASES)


def test_every_response_error_code_is_declared_and_exercised_by_a_case():
    raised = {case["expects"]["code"] for case in MALFORMED_CASES}
    assert raised == set(RESPONSE_ERROR_CODES)
    assert all(type(code) is str and code.strip() for code in RESPONSE_ERROR_CODES)
    assert len(RESPONSE_ERROR_CODES) == 17


def test_response_fields_are_exactly_the_documented_eight():
    assert RESPONSE_FIELDS == ("question_id", "dimension", "label", "probabilities", "confidence",
                               "model_version", "prompt_version", "unavailable_reason")
    question = build(CASE_BY_NAME["full_frequency"])
    payload = response_payload(question)
    assert set(payload) == set(RESPONSE_FIELDS)
    assert validate_response(question, payload) == validate_response(question, json.dumps(payload))
    for field in RESPONSE_FIELDS:
        broken = {name: value for name, value in payload.items() if name != field}
        with pytest.raises(JevResponseError) as error:
            validate_response(question, broken)
        assert error.value.code == "missing_field"
    for field in ("overall", "compatibility", "score", "rank"):
        with pytest.raises(JevResponseError) as error:
            validate_response(question, dict(payload, **{field: 0.5}))
        assert error.value.code == "unexpected_field"


def test_json_text_duplicate_keys_non_finite_and_non_object_are_invalid_responses():
    question = build(CASE_BY_NAME["full_frequency"])
    for value in ("not json", '{"question_id": "a", "question_id": "b"}',
                  '{"confidence": NaN}', "[1, 2]", '"frequency"', "7"):
        with pytest.raises(JevResponseError) as error:
            validate_response(question, value)
        assert error.value.code == "invalid_response"


def test_echo_checks_and_the_plan_era_dimension_alias():
    question = build(CASE_BY_NAME["full_frequency"])
    payload = response_payload(question)
    for field, value, code in (("question_id", "q-" + "0" * 64, "question_id_mismatch"),
                               ("dimension", "texture", "dimension_mismatch"),
                               ("prompt_version", "other-prompt", "prompt_version_mismatch"),
                               ("dimension", "frequencyFit", "unsupported_dimension")):
        with pytest.raises(JevResponseError) as error:
            validate_response(question, dict(payload, **{field: value}))
        assert error.value.code == code
    for dimension in DIMENSIONS:
        if dimension != "frequency":
            with pytest.raises(JevResponseError) as error:
                validate_response(question, dict(payload, dimension=dimension))
            assert error.value.code == "dimension_mismatch"


def test_probability_order_does_not_change_the_validated_judgment():
    question = build(CASE_BY_NAME["full_frequency"])
    values = dict(zip(LABELS, STANDARD))
    forward = [{"label": name, "probability": values[name]} for name in LABELS]
    backward = [{"label": name, "probability": values[name]} for name in reversed(LABELS)]
    first = validate_response(question, response_payload(question) | {"probabilities": forward})
    second = validate_response(question, response_payload(question) | {"probabilities": backward})
    assert first == second
    assert [item.label for item in first.probabilities] == LABELS


@pytest.mark.parametrize("confidence", [0.0, 0.05, 0.60, 0.95, 1.0])
def test_confidence_is_recorded_unchanged_and_never_derived(confidence):
    question = build(CASE_BY_NAME["full_frequency"])
    judgment = validate_response(question, response_payload(question, confidence=confidence))
    assert judgment.confidence == confidence
    assert {item.label: item.probability for item in judgment.probabilities}[judgment.label] == 0.60


@pytest.mark.parametrize("delta,accepted", [(5e-7, True), (2e-6, False), (-5e-7, True),
                                            (-2e-6, False), (0.0, True)])
def test_probability_sum_uses_the_contract_tolerance(delta, accepted):
    assert PROBABILITY_SUM_TOLERANCE == 1e-6
    question = build(CASE_BY_NAME["full_frequency"])
    values = [0.5 + delta, 0.2, 0.1, 0.1, 0.1]
    payload = response_payload(question, label="very-poor", values=values)
    if accepted:
        judgment = validate_response(question, payload)
        assert judgment.label == "very-poor"
    else:
        with pytest.raises(JevResponseError) as error:
            validate_response(question, payload)
        assert error.value.code == "probability_sum"


def test_a_judgment_that_contradicts_measured_facts_is_surfaced_not_reconciled():
    case = CASE_BY_NAME["frequency_low_band_contradiction"]
    kick, candidate, song = pair(case)
    question = build_question("frequency", kick, candidate, song=song)
    baseline = rank_candidates(kick, [candidate], policy=RankingPolicy()).ranked[0]
    frequency = next(entry for entry in baseline.dsp_dimensions if entry.dimension == "frequency")
    assert frequency.compatibility is not None and frequency.compatibility <= 0.5
    values = [0.0, 0.0, 0.05, 0.15, 0.80]
    payload = response_payload(question, label="excellent", values=values, confidence=0.93)
    judgment = validate_response(question, payload)
    assert judgment.label == "excellent" and judgment.confidence == 0.93
    assert {item.label: item.probability for item in judgment.probabilities} == \
        dict(zip(LABELS, values))
    source = DECISIONS_FILE.read_text(encoding="utf-8")
    assert "MEASURES" not in source and "band_" not in source and "spectral_" not in source
    assert "rank" not in source


def test_abstention_requires_the_single_stable_token_and_stores_no_free_text():
    question = build(CASE_BY_NAME["full_frequency"])
    for reason in ("not_sure", "unsure", "model abstained", "", "MODEL_ABSTAINED", None):
        with pytest.raises(JevResponseError) as error:
            validate_response(question, abstention_payload(question, reason=reason))
        assert error.value.code == "invalid_abstention"
    for probabilities, confidence in (([], 0.5), (response_payload(question)["probabilities"], None),
                                      (None, 0.5), ({}, None)):
        with pytest.raises(JevResponseError) as error:
            validate_response(question, abstention_payload(question, probabilities=probabilities,
                                                           confidence=confidence))
        assert error.value.code == "invalid_abstention"
    accepted = validate_response(question, abstention_payload(question))
    assert accepted.label is None and accepted.confidence is None and accepted.probabilities == ()
    assert accepted.unavailable_reason == MODEL_ABSTAINED
    assert "not_sure" not in accepted.to_json()
    empty = validate_response(question, abstention_payload(question, probabilities=[]))
    assert empty == accepted


def test_a_labeled_response_may_not_carry_an_abstention_reason():
    question = build(CASE_BY_NAME["full_frequency"])
    for reason in (MODEL_ABSTAINED, "", "not_sure", 7):
        with pytest.raises(JevResponseError) as error:
            validate_response(question, response_payload(question, reason=reason))
        assert error.value.code == "invalid_abstention"


def test_unsupported_labels_probabilities_and_confidence():
    question = build(CASE_BY_NAME["full_frequency"])
    for label in ("GOOD", "very_poor", "rating-good", "Good", "acceptable", 7, True, []):
        with pytest.raises(JevResponseError) as error:
            validate_response(question, response_payload(question, label=label))
        assert error.value.code in ("unsupported_label", "invalid_probabilities")
    for confidence in (-0.1, 1.1, "0.9", True, None, float("nan"), float("inf"), 10 ** 400):
        with pytest.raises(JevResponseError) as error:
            validate_response(question, response_payload(question, confidence=confidence))
        assert error.value.code == "invalid_confidence"
    for probability in (-0.01, 1.01, float("nan"), float("inf"), 10 ** 400):
        values = [0.05, 0.05, 0.10, probability, 0.20]
        with pytest.raises(JevResponseError) as error:
            validate_response(question, response_payload(question, values=values))
        assert error.value.code == "probability_out_of_range"
    for probability in ("0.6", True, None, [0.6]):
        values = [0.05, 0.05, 0.10, probability, 0.20]
        with pytest.raises(JevResponseError) as error:
            validate_response(question, response_payload(question, values=values))
        assert error.value.code == "invalid_probabilities"


def test_label_must_be_one_of_the_labels_attaining_the_highest_probability():
    question = build(CASE_BY_NAME["full_frequency"])
    with pytest.raises(JevResponseError) as error:
        validate_response(question, response_payload(question, label="poor"))
    assert error.value.code == "label_probability_mismatch"
    tied = validate_response(question, response_payload(question, label="neutral",
                                                        values=[0.1, 0.1, 0.4, 0.4, 0.0]))
    tied_other = validate_response(question, response_payload(question, label="good",
                                                              values=[0.1, 0.1, 0.4, 0.4, 0.0]))
    assert tied.label == "neutral" and tied_other.label == "good"


@pytest.mark.parametrize("model_version", ["synthetic-model-1", "other model", "0", " m2 "])
def test_any_nonblank_model_version_is_recorded(model_version):
    question = build(CASE_BY_NAME["full_frequency"])
    judgment = validate_response(question, response_payload(question, model_version=model_version))
    assert judgment.model_version == model_version
    assert judgment.prompt_version == PROMPT_VERSION


def test_a_refused_question_can_never_receive_an_accepted_judgment():
    unavailable = build(CASE_BY_NAME["rhythmic_song_context_absent"])
    assert type(unavailable) is UnavailableQuestion
    for argument in ({"question_id": "q-x"}, json.dumps({"question_id": "q-x"}), None, [1]):
        with pytest.raises(JevResponseError) as error:
            validate_response(unavailable, argument)
        assert error.value.code == "question_unavailable"
    for argument in (None, {}, "frequency", 7, [], JevQuestion):
        with pytest.raises(JevResponseError) as error:
            validate_response(argument, {"question_id": "q-x"})
        assert error.value.code == "invalid_question"


def test_a_consumer_can_distinguish_not_asked_abstained_and_judged():
    kick, candidate, song = pair(CASE_BY_NAME["full_frequency"])
    not_asked = build_question("rhythmic", kick, candidate, song=None)
    assert type(not_asked) is UnavailableQuestion and not isinstance(not_asked, JevQuestion)
    asked = build_question("frequency", kick, candidate, song=song)
    judged = validate_response(asked, response_payload(asked))
    abstained = validate_response(asked, abstention_payload(asked))
    assert type(judged) is JevJudgment and judged.label in get_args(Label)
    assert judged.unavailable_reason is None
    assert abstained.label is None and abstained.unavailable_reason == MODEL_ABSTAINED
    assert judged != abstained and not isinstance(not_asked, JevJudgment)


def test_validation_never_mutates_the_response_or_the_asked_question():
    asked = build(CASE_BY_NAME["full_frequency"])
    payload = response_payload(asked)
    before = json.dumps(payload, sort_keys=True)
    question_before = asked.to_json()
    validate_response(asked, payload)
    validate_response(asked, json.dumps(payload))
    validate_response(asked, abstention_payload(asked))
    assert json.dumps(payload, sort_keys=True) == before
    assert asked.to_json() == question_before
    assert build(CASE_BY_NAME["full_frequency"]) == asked


def test_each_dimension_validates_a_full_response():
    for dimension in DIMENSIONS:
        case = CASE_BY_NAME["full_" + dimension]
        asked = build(case)
        assert asked.dimension == dimension
        judgment = validate_response(asked, response_payload(asked, confidence=0.5))
        assert judgment.dimension == dimension and judgment.confidence == 0.5
        assert judgment.prompt_version == PROMPT_VERSION


def test_decisions_module_is_local_and_reconciles_nothing():
    source = DECISIONS_FILE.read_text(encoding="utf-8")
    for forbidden in ("local_path", "socket", "requests", "urllib", "http://", "https://", "open(",
                      "soundfile", "pathlib", "subprocess", "import os", "backend.intelligence.jev",
                      "backend.audio", "backend.palette"):
        assert forbidden not in source, forbidden
    assert source.count("def validate_response") == 1
    assert "MODEL_ABSTAINED = \"model_abstained\"" in source
    assert validate_response.__doc__ and "JevJudgment" in validate_response.__doc__


def test_document_records_the_response_shape_codes_and_abstention():
    text = DOCUMENT.read_text(encoding="utf-8")
    for field in RESPONSE_FIELDS:
        assert TICK + field + TICK in text
    assert {row[0].strip(TICK) for row in table_rows(text, "## Response error codes")
            if row[0].startswith(TICK)} == set(RESPONSE_ERROR_CODES)
    for code in RESPONSE_ERROR_CODES:
        assert TICK + code + TICK in text
    for literal in (MODEL_ABSTAINED, PROMPT_VERSION, "1e-6", "0.80", "CONFIDENCE_THRESHOLD",
                    "unexpected_field", "question_unavailable", "invalid_abstention", "#15"):
        assert literal in text
    assert "not asked" in text and "abstained" in text and "judged" in text
