"""One gated live check of the real TypeSafe Jev interface; see _docs/jev-adapter.md.

This module holds exactly one test. It runs only when TERA_JEV_ENDPOINT and TERA_JEV_API_KEY
are both non-blank, and it skips with UNVERIFIED otherwise, because no local evidence can
establish the real wire mapping, the real model versions or the real service timing. It sends
one question once, with max_attempts=1 so the live call is never retried, and asserts the
adapter contract rather than a musical answer.
"""

import json
import os
from pathlib import Path

import pytest

from backend.contracts import (AudioFeatures, AudioMetadata, MEASURES, Measurement, MusicalKey,
                               Sample, SongContext)
from backend.intelligence.jev import (ENV_API_KEY, ENV_ENDPOINT, JevAdapterConfig,
                                      JevScoringRequest, JevScoringRun, score_questions)
from backend.intelligence.questions import JevQuestion, build_question

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "jev"
CASE_NAME = "full_frequency"
REQUEST_ID = "candidate-live"
MAX_LIVE_TIMEOUT_S = 20.0
MAX_LIVE_BATCH_SECONDS = 30.0
UNVERIFIED = ("real TypeSafe Jev integration: UNVERIFIED - "
              "TERA_JEV_ENDPOINT/TERA_JEV_API_KEY are not set")
LIVE_STATES = ("judged", "abstained", "invalid_result", "service_error", "timed_out")

RATE = 48000
FRAMES = 48000
QUESTION_CASES = json.loads((FIXTURES / "question-cases.json").read_text(encoding="utf-8"))
CASE_BY_NAME = {case["name"]: case for case in QUESTION_CASES}


def configured():
    """Both variables are set to something non-blank."""
    return (bool(os.environ.get(ENV_ENDPOINT, "").strip())
            and bool(os.environ.get(ENV_API_KEY, "").strip()))


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
                                                          "C:/synthetic-live/side.wav"),
                                      sample_rate_hz=RATE, channels=1, frame_count=frames,
                                      duration_ms=frames * 1000 / RATE),
                  features=AudioFeatures(measurements=tuple(values.values()),
                                         key=musical_key(spec.get("key"))),
                  analysis_version=spec.get("analysis_version", "jev-live-test-1"))


def song_context(spec):
    if spec is None:
        return None
    return SongContext(tempo=measure("tempo", spec["tempo"].get("value"),
                                     spec["tempo"].get("confidence")),
                       key=musical_key(spec.get("key")), genre=spec.get("genre"),
                       genre_unavailable_reason=None if spec.get("genre") is not None
                       else "not_provided")


def live_question():
    case = CASE_BY_NAME[CASE_NAME]
    return build_question(case["dimension"],
                          side_sample(case["kick"], "kick-001", "kick"),
                          side_sample(case["candidate"], "bass-001", "bass"),
                          song=song_context(case["song"]))


@pytest.mark.skipif(not configured(), reason=UNVERIFIED)
def test_the_live_typesafe_jev_interface_is_checked_once():
    question = live_question()
    assert type(question) is JevQuestion
    config = JevAdapterConfig(timeout_s=MAX_LIVE_TIMEOUT_S, max_attempts=1,
                              max_batch_seconds=MAX_LIVE_BATCH_SECONDS)
    assert config.timeout_s <= 20.0
    assert config.max_attempts == 1
    assert config.max_batch_seconds <= 30.0
    requests = [JevScoringRequest(REQUEST_ID, question)]
    run = score_questions(requests, config=config)
    assert type(run) is JevScoringRun
    assert run.source == "interface"
    assert run.interface_name == "typesafe-jev-http"
    assert len(run.outcomes) == 1
    outcome = run.outcomes[0]
    assert outcome.request_id == REQUEST_ID
    assert outcome.question_id == question.question_id
    assert outcome.attempts == 1
    assert outcome.state in LIVE_STATES
    model_version = None if outcome.judgment is None else outcome.judgment.model_version
    if outcome.state in ("judged", "abstained"):
        assert type(model_version) is str and model_version.strip()
        assert model_version in run.model_versions
    text = run.to_json()
    endpoint = os.environ[ENV_ENDPOINT].strip()
    api_key = os.environ[ENV_API_KEY].strip()
    assert endpoint not in text and api_key not in text
    assert question.question_id in text and outcome.dimension == question.dimension
    print("live Jev evidence: adapter_version=" + run.adapter_version
          + " interface_name=" + str(run.interface_name)
          + " state=" + outcome.state
          + " code=" + str(outcome.code)
          + " model_version=" + str(model_version))
