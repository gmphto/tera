"""Drive the five weighted #13 questions through #14's adapter and into #15.

The fixture tests/fixtures/hybrid/flow-cases.json records the documented #14
outcome-to-evidence mapping: one case per value of backend.intelligence.jev.OUTCOME_STATES,
with the evidence and the codes #15 records for it. This module builds the five weighted
questions with #13's build_question, scores them with #14's score_questions and its
deterministic JevContractDouble, maps each outcome to HybridEvidence exactly as
_docs/hybrid-ranking.md documents it, and asserts that no failed, abstained, invalid,
cancelled, timed-out, not-asked or unavailable outcome ever becomes a judgment or a score.

The mapping implemented here is the caller's job (#28 owns the product loop); ranking.py
issues no Jev call and imports nothing from the intelligence package.
"""

import json
from pathlib import Path

import pytest

from backend.contracts import (AudioFeatures, AudioMetadata, MEASURES, Measurement, MusicalKey,
                               Sample)
from backend.intelligence.jev import (JevAdapterConfig, JevScoringRequest, OUTCOME_STATES,
                                      score_questions)
from backend.intelligence.jev_double import JevContractDouble
from backend.intelligence.questions import (PROMPT_VERSION, JevQuestion, UnavailableQuestion,
                                            build_question)
from backend.palette.ranking import (HYBRID_CODES, HYBRID_WEIGHTED_DIMENSIONS, JEV_ABSENT,
                                     JEV_LABEL_SCORES, MODE_DSP_ONLY, MODE_HYBRID, RankingPolicy,
                                     HybridEvidence, HybridPolicy, rank_candidates, rank_hybrid)

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "hybrid"
DOCUMENT = ROOT / "_docs" / "hybrid-ranking.md"
FLOW_CASES = json.loads((FIXTURES / "flow-cases.json").read_text(encoding="utf-8"))
CASE_BY_NAME = {case["name"]: case for case in FLOW_CASES}

RATE, FRAMES = 48000, 24000
ANALYSIS = "hybrid-flow-1"
KICK_ID = "kick-flow-001"
CANDIDATE_ID = "bass-flow-001"
TICK = chr(96)
SHORT_BANDS = {"sub": "band_sub", "bass": "band_bass", "low_mid": "band_low_mid",
               "mid": "band_mid", "high_mid": "band_high_mid", "high": "band_high"}
DEFAULT_ENTRY = {"judgment": {"probabilities": [0.05, 0.05, 0.10, 0.60, 0.20], "confidence": 0.9,
                              "model_version": "flow-double-default-1"}}


def measure(name, value=None, confidence=None):
    if value is None:
        return Measurement(name=name, value=None, unit=MEASURES[name][0], confidence=None,
                           unavailable_reason="not_implemented")
    return Measurement(name=name, value=value, unit=MEASURES[name][0], confidence=confidence)


def musical_key(tonic, confidence=0.90):
    return MusicalKey(tonic=tonic, mode="major", confidence=confidence)


def sample(sample_id, role, bands, *, position, fundamental, tonic, rolloff=440.0, centroid=220.0):
    values = {name: measure(name) for name in MEASURES}
    values["peak"] = measure("peak", 0.8)
    values["rms"] = measure("rms", 0.2)
    values["attack"] = measure("attack", 10.0)
    values["decay"] = measure("decay", 250.0)
    values["transient_strength"] = measure("transient_strength", 0.8)
    values["transient_position"] = measure("transient_position", position)
    values["fundamental"] = measure("fundamental", fundamental, 0.90)
    values["spectral_centroid"] = measure("spectral_centroid", centroid)
    values["spectral_rolloff"] = measure("spectral_rolloff", rolloff)
    for short, name in SHORT_BANDS.items():
        values[name] = measure(name, bands.get(short, 0.0))
    return Sample(sample_id=sample_id, role=role,
                  audio=AudioMetadata(local_path="C:/synthetic-hybrid/" + sample_id + ".wav",
                                      sample_rate_hz=RATE, channels=1, frame_count=FRAMES,
                                      duration_ms=FRAMES * 1000 / RATE),
                  features=AudioFeatures(measurements=tuple(values.values()),
                                         key=musical_key(tonic)),
                  analysis_version=ANALYSIS)


def kick_and_candidate(*, rolloff=440.0):
    kick = sample(KICK_ID, "kick", {"sub": 0.5, "bass": 0.3, "low_mid": 0.1, "mid": 0.05,
                                    "high_mid": 0.03, "high": 0.02},
                  position=0.0, fundamental=55.0, tonic="C", rolloff=rolloff, centroid=220.0)
    candidate = sample(CANDIDATE_ID, "bass", {"bass": 0.1, "low_mid": 0.3, "mid": 0.4,
                                             "high_mid": 0.15, "high": 0.05},
                       position=300.0, fundamental=110.0, tonic="C", rolloff=4000.0, centroid=1800.0)
    return kick, candidate


def questions_for(kick, candidate):
    """The one #13 question per weighted dimension, with no song context supplied."""
    return {dimension: build_question(dimension, kick, candidate)
            for dimension in HYBRID_WEIGHTED_DIMENSIONS}


def clock_from(readings):
    """The documented monotonic readings, consumed in call order, repeating the last."""
    values = list(readings)

    def monotonic():
        return values.pop(0) if len(values) > 1 else values[0]

    return monotonic


def evidence_for(outcome):
    """The documented #14 outcome-to-#15 evidence mapping, implemented once."""
    assert outcome.state in OUTCOME_STATES
    if outcome.state in ("judged", "abstained"):
        return HybridEvidence(outcome.dimension, judgment=outcome.judgment)
    return HybridEvidence(outcome.dimension, unavailable_reason=outcome.code)


def case_evidence(run):
    return {CANDIDATE_ID: tuple(evidence_for(outcome) for outcome in run.outcomes)}


def drive(case):
    """Ask, score and rank one flow case; returns the run, the double and both results."""
    kick, candidate = kick_and_candidate(
        rolloff=None if case.get("question") == "unavailable" else 440.0)
    questions = questions_for(kick, candidate)
    question = questions[case["dimension"]]
    script = None
    if case.get("script") is not None:
        assert type(question) is JevQuestion
        script = {question.question_id: case["script"]}
    double = JevContractDouble(default=DEFAULT_ENTRY, script=script)
    cancel_after = case.get("cancel_after")
    cancelled = None if cancel_after is None else (lambda: len(double.calls) >= cancel_after)
    monotonic = clock_from(case["clock"]) if case.get("clock") else None
    transport = None if case.get("credentials") == "absent" else double
    requests = [JevScoringRequest(name, questions[name]) for name in HYBRID_WEIGHTED_DIMENSIONS]
    run = score_questions(requests, transport=transport, config=JevAdapterConfig(**case["config"]),
                          environ={}, cancelled=cancelled, monotonic=monotonic,
                          sleep=lambda seconds: None)
    baseline = rank_candidates(kick, [candidate], policy=RankingPolicy())
    result = rank_hybrid(baseline, case_evidence(run), policy=HybridPolicy())
    return run, double, questions, baseline, result


def dimensions_by_name(record):
    return {entry.dimension: entry for entry in record.hybrid_dimensions}


def warning_codes(warnings):
    return tuple(item.split(":", 1)[0] for item in warnings if item.split(":", 1)[0] in HYBRID_CODES)


def test_flow_fixture_covers_every_outcome_state_from_14():
    """The state set is read from #14, not re-declared in this fixture."""
    states = [case["state"] for case in FLOW_CASES]
    assert set(states) == set(OUTCOME_STATES)
    assert len(states) == len(OUTCOME_STATES) + 1 and len(set(states)) == len(OUTCOME_STATES)
    codes = {case["name"]: case["expects"].get("unavailable_reason") for case in FLOW_CASES}
    assert codes["not_attempted_cancelled"] == "cancelled"
    assert codes["not_attempted_deadline"] == "batch_deadline_exceeded"
    for case in FLOW_CASES:
        assert case["dimension"] in HYBRID_WEIGHTED_DIMENSIONS
        assert case["expects"]["mode"] in ("hybrid", "dsp-only")
        for code in case["expects"]["warning_codes"]:
            assert code in HYBRID_CODES


@pytest.mark.parametrize("case", FLOW_CASES, ids=[case["name"] for case in FLOW_CASES])
def test_outcome_to_evidence_mapping_is_the_documented_one(case):
    run, double, questions, baseline, result = drive(case)
    dimension = case["dimension"]
    question = questions[dimension]
    outcome = next(item for item in run.outcomes if item.dimension == dimension)
    assert outcome.state == case["state"]
    expected = case["expects"]
    evidence = case_evidence(run)[CANDIDATE_ID]
    recorded = next(item for item in evidence if item.dimension == dimension)
    if expected["evidence"] == "judgment":
        assert recorded.judgment is outcome.judgment
        assert recorded.unavailable_reason is None
    else:
        assert recorded.judgment is None
        assert recorded.unavailable_reason == outcome.code
    entry = dimensions_by_name(result.ranked[0])[dimension]
    assert entry.jev_label == expected["label"]
    assert entry.jev_score == (None if expected["label"] is None
                               else JEV_LABEL_SCORES[expected["label"]])
    expected_reason = expected["unavailable_reason"]
    if expected_reason == "question_code":
        assert type(question) is UnavailableQuestion
        expected_reason = question.code
    assert entry.jev_unavailable_reason == expected_reason
    assert list(warning_codes(result.ranked[0].warnings)) == expected["warning_codes"]
    assert result.mode == expected["mode"] and result.jev_status == expected["jev_status"]
    assert baseline.ranked and len(result.ranked) == 1
    if case.get("credentials") == "absent":
        assert not double.calls and outcome.attempts == 0
    else:
        assert double.calls  # the case really went through #14
        assert len(double.calls) <= len(HYBRID_WEIGHTED_DIMENSIONS)


@pytest.mark.parametrize("case", FLOW_CASES, ids=[case["name"] for case in FLOW_CASES])
def test_no_failed_outcome_becomes_a_judgment_or_a_score(case):
    run, _, _, _, result = drive(case)
    entries = dimensions_by_name(result.ranked[0])
    for outcome in run.outcomes:
        entry = entries[outcome.dimension]
        if outcome.state == "judged":
            assert outcome.judgment is not None and outcome.judgment.label is not None
            assert entry.jev_label == outcome.judgment.label
            assert entry.jev_score == JEV_LABEL_SCORES[outcome.judgment.label]
            continue
        assert entry.jev_score is None
        assert entry.jev_label is None
        if outcome.state == "abstained":
            assert outcome.judgment.label is None
            assert entry.jev_confidence is None and entry.jev_probabilities == ()
            assert entry.jev_unavailable_reason == "jev_abstained"
        else:
            assert outcome.judgment is None
            assert entry.jev_confidence is None and entry.jev_probabilities == ()
            assert entry.jev_model_version is None and entry.jev_prompt_version is None
            assert entry.jev_unavailable_reason == outcome.code
            assert outcome.code and outcome.attempts >= 0


def test_the_five_weighted_questions_carry_no_sample_identity():
    run, double, questions, _, _ = drive(CASE_BY_NAME["judged"])
    assert tuple(questions) == HYBRID_WEIGHTED_DIMENSIONS
    for dimension, question in questions.items():
        assert question.dimension == dimension
        assert type(question) is JevQuestion
        assert question.prompt_version == PROMPT_VERSION
        payload = question.to_json()
        for secret in (KICK_ID, CANDIDATE_ID, ".wav", "C:", "synthetic-hybrid"):
            assert secret not in payload
    assert [item.dimension for item in run.outcomes] == list(HYBRID_WEIGHTED_DIMENSIONS)


def test_the_hybrid_path_issues_no_jev_call_and_needs_no_adapter():
    run, double, _, baseline, result = drive(CASE_BY_NAME["judged"])
    sends = len(double.calls)
    again = rank_hybrid(baseline, case_evidence(run), policy=HybridPolicy())
    assert len(double.calls) == sends
    assert again == result
    assert result.mode == MODE_HYBRID and result.jev_status == "jev_present"
    assert all(entry.jev_score is not None for entry in result.ranked[0].hybrid_dimensions
               if entry.weight > 0)


def test_total_transport_absence_gives_the_documented_dsp_only_fallback():
    run, double, _, baseline, result = drive(CASE_BY_NAME["unavailable"])
    assert not double.calls
    assert all(outcome.state == "unavailable" for outcome in run.outcomes)
    assert all(outcome.code == "credentials_absent" for outcome in run.outcomes)
    assert result.mode == MODE_DSP_ONLY and result.jev_status == JEV_ABSENT
    for record, ranked in zip(result.ranked, baseline.ranked):
        assert record.compatibility == ranked.compatibility
        assert record.confidence == ranked.confidence
        assert record.dsp_dimensions == ranked.dsp_dimensions
        assert record.reasons == ranked.reasons
        assert record.warnings == ranked.warnings
    for entry in result.ranked[0].hybrid_dimensions:
        if entry.dimension in ("frequency", "transient", "tonal"):
            assert entry.jev_unavailable_reason == "credentials_absent"
            assert entry.jev_score is None


def test_document_tables_every_outcome_state():
    text = DOCUMENT.read_text(encoding="utf-8")
    for state in OUTCOME_STATES:
        assert "| " + TICK + state + TICK + " |" in text, state
    for code in ("credentials_absent", "connection_failed", "rate_limited",
                 "invalid_credentials", "batch_deadline_exceeded", "cancelled", "timeout",
                 "service_unavailable", "invalid_response", "song_context_absent",
                 "kick_key_unknown", "model_abstained", "jev_abstained"):
        assert code in text, code
