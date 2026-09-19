"""Bounded Jev adapter proof; no service, socket, credential or wall-clock wait.

Every case here is synthetic and local. The adapter is driven through a recording transport and
a recording clock, the assumed HTTP mapping is driven through a fake opener object, and the
#13 fixtures are replayed to prove the not-asked and invalid-result loops. The batch scenarios,
including the absent-credentials run and every code space, come from
tests/fixtures/jev/adapter-cases.json; what the adapter actually sent comes from
tests/fixtures/jev/captured-requests.json.
"""

import json
import urllib.error
import urllib.request
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from backend.contracts import (AudioFeatures, AudioMetadata, JevJudgment, MEASURES, Measurement,
                               MusicalKey, Sample, SongContext)
from backend.intelligence import jev as jev_module
from backend.intelligence.decisions import (MODEL_ABSTAINED, RESPONSE_ERROR_CODES,
                                            JevResponseError, validate_response)
from backend.intelligence.jev import (ADAPTER_ERROR_CODES, ADAPTER_VERSION, BACKOFF_MULTIPLIER,
                                      DEFAULT_BACKOFF_S, DEFAULT_MAX_ATTEMPTS,
                                      DEFAULT_MAX_BATCH_SECONDS, DEFAULT_TIMEOUT_S, ENV_API_KEY,
                                      ENV_ENDPOINT, ENV_MAX_ATTEMPTS, ENV_MAX_BATCH_SECONDS,
                                      ENV_TIMEOUT_S, HttpJevTransport, JevAdapterConfig,
                                      JevAdapterError, JevCredentials, JevOutcome,
                                      JevScoringRequest, JevScoringRun, JevTransport,
                                      JevTransportError, MAX_BACKOFF_S, MAX_BATCH_SIZE,
                                      MAX_MAX_ATTEMPTS, MAX_MAX_BATCH_SECONDS, MAX_TIMEOUT_S,
                                      MIN_MAX_ATTEMPTS, MIN_MAX_BATCH_SECONDS, MIN_TIMEOUT_S,
                                      NoRedirectHandler, NOT_ATTEMPTED_CODES, OUTCOME_STATES,
                                      PROMPT_VERSION, RETRYABLE_TRANSPORT_CODES,
                                      TRANSPORT_ERROR_CODES, TRANSPORT_SOURCES, UNAVAILABLE_CODES,
                                      score_questions)
from backend.intelligence.jev_double import JevContractDouble
from backend.intelligence.questions import (DIMENSIONS, QUESTION_UNAVAILABLE_CODES, JevQuestion,
                                            UnavailableQuestion, build_question)

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "jev"
DOCUMENT = ROOT / "_docs" / "jev-adapter.md"
JEV_FILE = ROOT / "backend" / "intelligence" / "jev.py"
DOUBLE_FILE = ROOT / "backend" / "intelligence" / "jev_double.py"

TICK = chr(96)
RATE = 48000
FRAMES = 48000
ANALYSIS = "jev-adapter-test-1"
ENDPOINT = "https://jev.example.invalid/v1/messages"
API_KEY = "sk-synthetic-distinctive-4c1f"
PAYLOAD = '{"dimension": "frequency", "question_id": "q-synthetic", "prompt_version": "jev-questions-v1"}'

QUESTION_CASES = json.loads((FIXTURES / "question-cases.json").read_text(encoding="utf-8"))
CASE_BY_NAME = {case["name"]: case for case in QUESTION_CASES}
MALFORMED_CASES = json.loads((FIXTURES / "malformed-responses.json").read_text(encoding="utf-8"))
ADAPTER_CASES = json.loads((FIXTURES / "adapter-cases.json").read_text(encoding="utf-8"))
CAPTURED = json.loads((FIXTURES / "captured-requests.json").read_text(encoding="utf-8"))

# The 58 #13 unavailable cases; the focused run names every one of them.
UNAVAILABLE_CASES = [case for case in QUESTION_CASES
                     if case["expects"]["outcome"] == "unavailable"]
# The 53 #13 malformed-response cases; four of them (invalid_question and question_unavailable)
# cannot travel through a wire that only ever carries a real asked question.
WIRE_REACHABLE_MALFORMED = [case for case in MALFORMED_CASES
                           if "question_case" in case
                           and CASE_BY_NAME[case["question_case"]]["expects"]["outcome"]
                           == "question"]
UNREACHABLE_MALFORMED = [case for case in MALFORMED_CASES
                         if case not in WIRE_REACHABLE_MALFORMED]

IDENTITY_TOKENS = ("zzz-distinctive-9f8a", "silent-monolith-9f8a.wav", "zeta-distinctive-9f8a-id",
                   "13579", "local_path", "frame_count", ".wav")

# The constants the document has to agree with, derived from the module itself.
DOCUMENTED_CONSTANTS = ("ADAPTER_VERSION", "DEFAULT_TIMEOUT_S", "MIN_TIMEOUT_S", "MAX_TIMEOUT_S",
                        "DEFAULT_MAX_ATTEMPTS", "MIN_MAX_ATTEMPTS", "MAX_MAX_ATTEMPTS",
                        "DEFAULT_BACKOFF_S", "BACKOFF_MULTIPLIER", "MAX_BACKOFF_S",
                        "DEFAULT_MAX_BATCH_SECONDS", "MIN_MAX_BATCH_SECONDS",
                        "MAX_MAX_BATCH_SECONDS", "MAX_BATCH_SIZE", "OUTCOME_STATES",
                        "TRANSPORT_ERROR_CODES", "RETRYABLE_TRANSPORT_CODES",
                        "NOT_ATTEMPTED_CODES", "UNAVAILABLE_CODES", "ADAPTER_ERROR_CODES",
                        "TRANSPORT_SOURCES", "ENV_ENDPOINT", "ENV_API_KEY", "ENV_TIMEOUT_S",
                        "ENV_MAX_ATTEMPTS", "ENV_MAX_BATCH_SECONDS", "PROMPT_VERSION")
DOCUMENTED_DOUBLE_CONSTANTS = ("DOUBLE_INTERFACE_NAME", "DOUBLE_SOURCE", "DEFAULT_MODEL_VERSION",
                               "DEFAULT_PROBABILITIES", "DEFAULT_CONFIDENCE", "ENTRY_KINDS")


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
                                                          "C:/synthetic-adapter/side.wav"),
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
                       genre_unavailable_reason=None if spec.get("genre") is not None
                       else "not_provided")


def build(case):
    return build_question(case["dimension"],
                          side_sample(case["kick"], "kick-001", "kick"),
                          side_sample(case["candidate"], "bass-001", "bass"),
                          song=song_context(case["song"]))


class ScriptedClock:
    """A monotonic callable standing in for the wall clock; the last reading repeats."""

    def __init__(self, readings=(0.0,)):
        self.readings = list(readings)
        self.reads = 0

    def __call__(self):
        self.reads += 1
        if len(self.readings) > 1:
            return self.readings.pop(0)
        return self.readings[0]


class DelayRecorder:
    """A sleep callable that records the exact wait sequence without waiting."""

    def __init__(self):
        self.delays = []

    def __call__(self, seconds):
        self.delays.append(seconds)


class RecordingTransport(JevTransport):
    """A local recording transport: a scripted step list, then one answer for every send."""

    source = "double"
    interface_name = "recording-transport"

    def __init__(self, steps=None, answer=None, source="double",
                 interface_name="recording-transport"):
        self.calls = []
        self.steps = list(steps or [])
        self.answer = answer
        self.source = source
        self.interface_name = interface_name

    def send(self, payload_json, *, timeout_s):
        self.calls.append((payload_json, timeout_s))
        if self.steps:
            step = self.steps.pop(0)
            if isinstance(step, Exception):
                raise step
            if callable(step):
                return step(payload_json)
            return step
        if self.answer is None:
            return "{}"
        return self.answer(payload_json)

    @property
    def payloads(self):
        return [payload for payload, _ in self.calls]

    @property
    def timeouts(self):
        return [timeout for _, timeout in self.calls]


LABELS = ("very-poor", "poor", "neutral", "good", "excellent")


def judgment_text(payload_json, probabilities=(0.05, 0.05, 0.10, 0.60, 0.20), confidence=0.77,
                  model_version="synthetic-model-1"):
    payload = json.loads(payload_json)
    return json.dumps({
        "question_id": payload["question_id"], "dimension": payload["dimension"],
        "label": LABELS[probabilities.index(max(probabilities))], "confidence": confidence,
        "probabilities": [{"label": name, "probability": value}
                          for name, value in zip(LABELS, probabilities)],
        "model_version": model_version, "prompt_version": payload["prompt_version"],
        "unavailable_reason": None}, sort_keys=True)


def abstention_text(payload_json, model_version="synthetic-model-1"):
    payload = json.loads(payload_json)
    return json.dumps({"question_id": payload["question_id"], "dimension": payload["dimension"],
                       "label": None, "probabilities": None, "confidence": None,
                       "model_version": model_version,
                       "prompt_version": payload["prompt_version"],
                       "unavailable_reason": MODEL_ABSTAINED}, sort_keys=True)


def rejected_text(payload_json, model_version="synthetic-ignored"):
    payload = json.loads(payload_json)
    return json.dumps({"question_id": payload["question_id"], "dimension": payload["dimension"],
                       "label": "good", "model_version": model_version,
                       "prompt_version": payload["prompt_version"], "audio": "not accepted"},
                      sort_keys=True)


def substitute(value, question):
    if type(value) is str:
        return (value.replace("<question_id>", question.question_id)
                     .replace("<prompt_version>", question.prompt_version)
                     .replace("<dimension>", question.dimension))
    return json.loads(substitute(json.dumps(value), question))


def response_text_of(case, question):
    if "response_text" in case:
        return substitute(case["response_text"], question)
    if "response_value" in case:
        return json.dumps({"none": None, "list": [1, 2], "number": 7}[case["response_value"]])
    return json.dumps(substitute(case["response"], question), sort_keys=True)


def carries_non_finite(value):
    """True when a declared response value holds a non-finite number.

    The adapter places response text on the wire, and JSON text cannot carry a non-finite
    number as a number: #13 rejects the Infinity token at parse time with invalid_response
    before it ever compares a probability.
    """
    if type(value) is float:
        return value != value or value in (float("inf"), float("-inf"))
    if type(value) is dict:
        return any(carries_non_finite(item) for item in value.values())
    if type(value) is list:
        return any(carries_non_finite(item) for item in value)
    return False


def requests_for(case_names, prefix="req"):
    return [JevScoringRequest(request_id=prefix + "-" + str(index),
                              question=build(CASE_BY_NAME[name]))
            for index, name in enumerate(case_names)]


def recording_run(case_names=("full_frequency",), steps=None, answer=judgment_text,
                  config=None, cancelled=None):
    requests = requests_for(case_names)
    transport = RecordingTransport(steps=steps, answer=answer)
    clock = ScriptedClock([0.0])
    delays = DelayRecorder()
    run = score_questions(requests, transport=transport, config=config, cancelled=cancelled,
                          sleep=delays, monotonic=clock)
    return requests, transport, run, delays, clock


def table_rows(text, heading):
    lines = text.splitlines()
    rows = []
    for line in lines[lines.index(heading) + 1:]:
        if line.startswith("## "):
            break
        if line.startswith("|") and not set(line) <= set("|- "):
            rows.append([cell.strip() for cell in line.strip().strip("|").split("|")])
    return rows


def data_rows(text, heading, columns):
    return [row for row in table_rows(text, heading)
            if len(row) == columns and row[0].startswith(TICK)]


def rendered(value):
    if type(value) is str:
        return json.dumps(value)
    if type(value) is frozenset:
        return "frozenset({" + ", ".join(rendered(item) for item in sorted(value)) + "})"
    if type(value) is tuple:
        body = ", ".join(rendered(item) for item in value)
        return "(" + body + ("," if len(value) == 1 else "") + ")"
    return repr(value)


def test_the_published_constants_are_the_criteria_values():
    assert ADAPTER_VERSION == "jev-adapter-v1"
    assert (DEFAULT_TIMEOUT_S, MIN_TIMEOUT_S, MAX_TIMEOUT_S) == (20.0, 1.0, 120.0)
    assert (DEFAULT_MAX_ATTEMPTS, MIN_MAX_ATTEMPTS, MAX_MAX_ATTEMPTS) == (3, 1, 5)
    assert (DEFAULT_BACKOFF_S, BACKOFF_MULTIPLIER, MAX_BACKOFF_S) == (0.5, 2.0, 8.0)
    assert (DEFAULT_MAX_BATCH_SECONDS, MIN_MAX_BATCH_SECONDS, MAX_MAX_BATCH_SECONDS) \
        == (120.0, 1.0, 600.0)
    assert MAX_BATCH_SIZE == 100
    assert OUTCOME_STATES == ("judged", "abstained", "not_asked", "not_attempted",
                              "invalid_result", "service_error", "timed_out", "unavailable")
    assert TRANSPORT_ERROR_CODES == ("connection_failed", "service_unavailable", "service_error",
                                     "rate_limited", "timeout", "invalid_credentials")
    assert RETRYABLE_TRANSPORT_CODES == ("connection_failed", "service_unavailable",
                                         "service_error", "rate_limited", "timeout")
    assert NOT_ATTEMPTED_CODES == ("cancelled", "batch_deadline_exceeded")
    assert UNAVAILABLE_CODES == frozenset({"credentials_absent"})
    assert ADAPTER_ERROR_CODES == ("invalid_configuration", "batch_too_large",
                                   "duplicate_request_id", "invalid_requests")
    assert TRANSPORT_SOURCES == ("interface", "double")
    assert (ENV_ENDPOINT, ENV_API_KEY, ENV_TIMEOUT_S, ENV_MAX_ATTEMPTS, ENV_MAX_BATCH_SECONDS) \
        == ("TERA_JEV_ENDPOINT", "TERA_JEV_API_KEY", "TERA_JEV_TIMEOUT_S",
            "TERA_JEV_MAX_ATTEMPTS", "TERA_JEV_MAX_BATCH_SECONDS")
    assert PROMPT_VERSION == "jev-questions-v1"


ADAPTER_SURFACE = ("ADAPTER_VERSION", "DEFAULT_TIMEOUT_S", "MIN_TIMEOUT_S", "MAX_TIMEOUT_S",
                   "DEFAULT_MAX_ATTEMPTS", "MIN_MAX_ATTEMPTS", "MAX_MAX_ATTEMPTS",
                   "DEFAULT_BACKOFF_S", "BACKOFF_MULTIPLIER", "MAX_BACKOFF_S",
                   "DEFAULT_MAX_BATCH_SECONDS", "MIN_MAX_BATCH_SECONDS", "MAX_MAX_BATCH_SECONDS",
                   "MAX_BATCH_SIZE", "OUTCOME_STATES", "TRANSPORT_ERROR_CODES",
                   "RETRYABLE_TRANSPORT_CODES", "NOT_ATTEMPTED_CODES", "UNAVAILABLE_CODES",
                   "ADAPTER_ERROR_CODES", "TRANSPORT_SOURCES", "JevTransport", "JevTransportError",
                   "JevAdapterError", "JevCredentials", "JevAdapterConfig", "JevScoringRequest",
                   "JevOutcome", "JevScoringRun", "NoRedirectHandler", "HttpJevTransport",
                   "score_questions")


def test_the_adapter_exposes_the_documented_surface_with_the_documented_signatures():
    import inspect
    for name in ADAPTER_SURFACE:
        assert hasattr(jev_module, name), name
    signature = inspect.signature(score_questions)
    assert list(signature.parameters) == ["requests", "transport", "config", "credentials",
                                          "environ", "cancelled", "sleep", "monotonic"]
    assert signature.parameters["requests"].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
    for name in ("transport", "config", "credentials", "environ", "cancelled", "sleep",
                 "monotonic"):
        assert signature.parameters[name].kind is inspect.Parameter.KEYWORD_ONLY, name
        assert signature.parameters[name].default is None
    config_fields = list(inspect.signature(JevAdapterConfig).parameters)
    assert config_fields == ["timeout_s", "max_attempts", "backoff_s", "max_backoff_s",
                             "max_batch_seconds", "max_batch_size"]
    assert list(inspect.signature(JevCredentials).parameters) == ["endpoint", "api_key"]
    assert list(inspect.signature(JevScoringRequest).parameters) == ["request_id", "question"]
    assert list(inspect.signature(JevOutcome).parameters) == ["request_id", "question_id",
                                                              "dimension", "state", "code",
                                                              "attempts", "judgment", "elapsed_ms"]
    assert list(inspect.signature(JevScoringRun).parameters) == ["adapter_version", "source",
                                                                 "interface_name", "prompt_version",
                                                                 "model_versions", "cancelled",
                                                                 "outcomes", "elapsed_ms"]
    assert list(inspect.signature(JevContractDouble).parameters) == ["model_version", "default",
                                                                     "script"]
    assert list(inspect.signature(HttpJevTransport).parameters) == ["credentials", "opener"]
    assert inspect.signature(HttpJevTransport).parameters["opener"].kind \
        is inspect.Parameter.KEYWORD_ONLY
    for name in ("to_dict", "to_json", "from_dict", "from_json"):
        assert hasattr(JevScoringRun, name) and hasattr(JevOutcome, name), name


def test_the_default_configuration_is_the_published_defaults():
    config = JevAdapterConfig()
    assert config == JevAdapterConfig(DEFAULT_TIMEOUT_S, DEFAULT_MAX_ATTEMPTS, DEFAULT_BACKOFF_S,
                                      MAX_BACKOFF_S, DEFAULT_MAX_BATCH_SECONDS, MAX_BATCH_SIZE)


BAD_CONFIGURATIONS = [
    {"timeout_s": 0.0}, {"timeout_s": 0.5}, {"timeout_s": 120.5}, {"timeout_s": float("nan")},
    {"timeout_s": float("inf")}, {"timeout_s": "-20"}, {"timeout_s": True}, {"timeout_s": None},
    {"max_attempts": 0}, {"max_attempts": 6}, {"max_attempts": 2.5},
    {"max_attempts": True}, {"max_attempts": "3"}, {"backoff_s": 0}, {"backoff_s": -1.0},
    {"backoff_s": float("inf")}, {"backoff_s": "0.5"}, {"max_backoff_s": 0},
    {"max_backoff_s": float("nan")}, {"max_batch_seconds": 0.5}, {"max_batch_seconds": 601.0},
    {"max_batch_seconds": "120"}, {"max_batch_size": 0}, {"max_batch_size": MAX_BATCH_SIZE + 1},
    {"max_batch_size": 2.5}, {"max_batch_size": True},
]


@pytest.mark.parametrize("overrides", BAD_CONFIGURATIONS,
                         ids=lambda value: ",".join(map(str, value)))
def test_a_configuration_outside_its_bounds_is_rejected(overrides):
    with pytest.raises(JevAdapterError) as error:
        JevAdapterConfig(**overrides)
    assert error.value.code == "invalid_configuration"
    assert "invalid_configuration" in ADAPTER_ERROR_CODES


@pytest.mark.parametrize("overrides", [
    {"timeout_s": MIN_TIMEOUT_S}, {"timeout_s": MAX_TIMEOUT_S},
    {"max_attempts": MIN_MAX_ATTEMPTS}, {"max_attempts": MAX_MAX_ATTEMPTS},
    {"max_batch_seconds": MIN_MAX_BATCH_SECONDS}, {"max_batch_seconds": MAX_MAX_BATCH_SECONDS},
    {"max_batch_size": 1}, {"max_batch_size": MAX_BATCH_SIZE},
    {"backoff_s": 1e-9}, {"max_backoff_s": 1e9},
])
def test_every_published_bound_is_inclusive(overrides):
    assert JevAdapterConfig(**overrides) is not None


def test_config_from_env_reads_the_three_documented_variables():
    config = JevAdapterConfig.from_env({ENV_TIMEOUT_S: "5", ENV_MAX_ATTEMPTS: "2",
                                        ENV_MAX_BATCH_SECONDS: "30"})
    assert (config.timeout_s, config.max_attempts, config.max_batch_seconds) == (5.0, 2, 30.0)
    assert config.backoff_s == DEFAULT_BACKOFF_S and config.max_batch_size == MAX_BATCH_SIZE
    assert JevAdapterConfig.from_env({}) == JevAdapterConfig()


@pytest.mark.parametrize("name,bounds", [
    (ENV_TIMEOUT_S, "[1.0, 120.0]"), (ENV_MAX_ATTEMPTS, "[1, 5]"),
    (ENV_MAX_BATCH_SECONDS, "[1.0, 600.0]"),
])
@pytest.mark.parametrize("value", ["", "  ", "abc", "nan", "inf", "-1", "1e999"])
def test_config_from_env_rejects_a_bad_value_without_echoing_it(name, bounds, value):
    with pytest.raises(JevAdapterError) as error:
        JevAdapterConfig.from_env({name: value})
    assert error.value.code == "invalid_configuration"
    assert name in str(error.value) and bounds in str(error.value)
    if value.strip():
        assert value not in str(error.value)


@pytest.mark.parametrize("name,accepted,rejected", [
    (ENV_TIMEOUT_S, "20", "0.5"), (ENV_TIMEOUT_S, "1", "121"), (ENV_TIMEOUT_S, "120", "0.9"),
    (ENV_MAX_ATTEMPTS, "3", "0"), (ENV_MAX_ATTEMPTS, "1", "6"), (ENV_MAX_ATTEMPTS, "5", "1.5"),
    (ENV_MAX_BATCH_SECONDS, "120", "0.5"), (ENV_MAX_BATCH_SECONDS, "1", "601"),
    (ENV_MAX_BATCH_SECONDS, "600", "0.9"),
])
def test_config_from_env_applies_that_variable_bound(name, accepted, rejected):
    field = {ENV_TIMEOUT_S: "timeout_s", ENV_MAX_ATTEMPTS: "max_attempts",
             ENV_MAX_BATCH_SECONDS: "max_batch_seconds"}[name]
    got = getattr(JevAdapterConfig.from_env({name: accepted}), field)
    assert type(got) in (int, float)
    with pytest.raises(JevAdapterError) as error:
        JevAdapterConfig.from_env({name: rejected})
    assert error.value.code == "invalid_configuration"


def test_config_from_env_with_no_environ_reads_the_process_environment(monkeypatch):
    monkeypatch.setenv(ENV_TIMEOUT_S, "7")
    monkeypatch.delenv(ENV_MAX_ATTEMPTS, raising=False)
    monkeypatch.delenv(ENV_MAX_BATCH_SECONDS, raising=False)
    assert JevAdapterConfig.from_env().timeout_s == 7.0


def test_credentials_from_env_requires_both_values():
    assert JevCredentials.from_env({}) is None
    assert JevCredentials.from_env({ENV_ENDPOINT: "https://jev.example.invalid"}) is None
    assert JevCredentials.from_env({ENV_API_KEY: "synthetic-key"}) is None
    assert JevCredentials.from_env({ENV_ENDPOINT: "   ", ENV_API_KEY: "synthetic-key"}) is None
    assert JevCredentials.from_env({ENV_ENDPOINT: "https://jev.example.invalid",
                                    ENV_API_KEY: "  "}) is None
    credentials = JevCredentials.from_env({ENV_ENDPOINT: " https://jev.example.invalid/v1 ",
                                           ENV_API_KEY: " synthetic-key "})
    assert credentials.endpoint == "https://jev.example.invalid/v1"
    assert credentials.api_key == "synthetic-key"


@pytest.mark.parametrize("endpoint,accepted", [
    ("https://jev.example.invalid/v1", True),
    ("HTTPS://JEV.EXAMPLE.INVALID", True),
    ("https://127.0.0.1:8443/jev", True),
    ("http://127.0.0.1:8000", True),
    ("HTTP://localhost:8000/jev", True),
    ("http://localhost/jev", True),
    ("http://example.com", False),
    ("http://localhost.evil.example", False),
    ("http://[::1]:8000", False),
    ("ftp://jev.example.invalid", False),
    ("jev.example.invalid/v1", False),
    ("//jev.example.invalid/v1", False),
    ("https://", False),
    ("https://a b/v1", False),
    ("", False),
    ("   ", False),
])
def test_the_endpoint_scheme_rule_is_https_or_loopback(endpoint, accepted):
    if accepted:
        assert JevCredentials(endpoint=endpoint, api_key="synthetic-key").endpoint == endpoint
        return
    with pytest.raises(JevAdapterError) as error:
        JevCredentials(endpoint=endpoint, api_key="synthetic-key")
    assert error.value.code == "invalid_configuration"
    assert ENV_ENDPOINT in str(error.value)


@pytest.mark.parametrize("endpoint", ["https://jev.example.invalid", "http://127.0.0.1:8000"])
def test_credentials_render_the_key_and_endpoint_as_redacted(endpoint):
    credentials = JevCredentials(endpoint=endpoint, api_key=API_KEY)
    assert API_KEY not in repr(credentials) and API_KEY not in str(credentials)
    assert endpoint not in repr(credentials) and endpoint not in str(credentials)
    assert "<redacted>" in repr(credentials) and "<redacted>" in str(credentials)


def test_the_adapter_source_imports_only_the_allowed_modules_and_carries_no_identity():
    source = JEV_FILE.read_text(encoding="utf-8")
    for forbidden in ("local_path", "frame_count", "backend.audio", "backend.analysis", "soundfile",
                      "wave", "open(", "jev_double", "socket", "urlopen", "subprocess", "pathlib"):
        assert forbidden not in source, forbidden
    allowed = ("from __future__ import", "import json", "import os", "import time",
               "import urllib.error", "import urllib.request", "from dataclasses import",
               "from backend.contracts import", "from backend.intelligence.decisions import",
               "from backend.intelligence.questions import")
    imports = [line.strip() for line in source.splitlines()
               if line.startswith(("import ", "from "))]
    assert imports
    for line in imports:
        assert line.startswith(allowed), line
    assert score_questions.__doc__ and "JevScoringRun" in score_questions.__doc__
    assert "JevAdapterError" in score_questions.__doc__


def test_neither_adapter_module_changes_an_existing_module():
    for path in (JEV_FILE, DOUBLE_FILE):
        source = path.read_text(encoding="utf-8")
        for forbidden in ("setattr(", "sys.modules", "monkeypatch", "globals()", "exec("):
            assert forbidden not in source, (path.name, forbidden)
    import backend.contracts as contracts
    import backend.intelligence.decisions as decisions_module
    import backend.intelligence.questions as questions_module
    assert questions_module.PROMPT_VERSION == PROMPT_VERSION
    assert decisions_module.RESPONSE_ERROR_CODES == RESPONSE_ERROR_CODES
    assert contracts.SCHEMA_VERSION == "1.0"
    question = build(CASE_BY_NAME["full_frequency"])
    judgment = validate_response(question, judgment_text(question.to_json()))
    assert judgment.label == "good" and len(judgment.probabilities) == 5
    assert JevJudgment.from_json(judgment.to_json()) == judgment
    assert type(build_question) is not None and DIMENSIONS[0] == "frequency"


def test_absent_credentials_record_one_unavailable_outcome_per_request(monkeypatch):
    def forbidden(*arguments, **keywords):
        raise AssertionError("no transport may be constructed without credentials")

    monkeypatch.setattr(jev_module, "HttpJevTransport", forbidden)
    requests = requests_for(["full_frequency", "full_transient", "full_tonal", "full_rhythmic"])
    run = score_questions(requests, environ={})
    assert run.adapter_version == ADAPTER_VERSION
    assert run.source == "unavailable" and run.interface_name is None
    assert run.model_versions == () and run.cancelled is False and run.elapsed_ms == 0
    assert run.outcomes == tuple(run.outcomes) and len(run.outcomes) == len(requests)
    for request, outcome in zip(requests, run.outcomes):
        assert outcome.request_id == request.request_id
        assert outcome.question_id == request.question.question_id
        assert outcome.dimension == request.question.dimension
        assert (outcome.state, outcome.code) == ("unavailable", "credentials_absent")
        assert outcome.attempts == 0 and outcome.judgment is None
    assert JevScoringRun.from_json(run.to_json()) == run
    assert '"cancelled": false' in run.to_json()


def test_absent_credentials_without_environ_read_the_process_environment(monkeypatch):
    monkeypatch.delenv(ENV_ENDPOINT, raising=False)
    monkeypatch.delenv(ENV_API_KEY, raising=False)
    run = score_questions(requests_for(["full_frequency"]))
    assert run.source == "unavailable"
    assert [outcome.state for outcome in run.outcomes] == ["unavailable"]


def test_present_credentials_without_a_transport_build_the_http_transport(monkeypatch):
    seen = {}

    class Capturing(JevTransport):
        source = "interface"
        interface_name = "typesafe-jev-http"

        def __init__(self, credentials):
            seen["credentials"] = credentials
            self.calls = []

        def send(self, payload_json, *, timeout_s):
            self.calls.append((payload_json, timeout_s))
            return judgment_text(payload_json)

    monkeypatch.setattr(jev_module, "HttpJevTransport", Capturing)
    credentials = JevCredentials(endpoint=ENDPOINT, api_key=API_KEY)
    run = score_questions(requests_for(["full_frequency"]), credentials=credentials,
                          environ={ENV_ENDPOINT: "https://ignored.example.invalid",
                                   ENV_API_KEY: "ignored"})
    assert seen["credentials"] is credentials
    assert run.source == "interface" and run.interface_name == "typesafe-jev-http"
    assert [outcome.state for outcome in run.outcomes] == ["judged"]


def test_one_outcome_per_request_in_input_order_keyed_by_the_callers_id():
    requests, transport, run, delays, clock = recording_run(
        ["full_frequency", "full_transient", "full_tonal"])
    assert len(run.outcomes) == len(requests) == 3
    assert [outcome.request_id for outcome in run.outcomes] == [request.request_id
                                                                for request in requests]
    assert all(type(outcome.question_id) is str for outcome in run.outcomes)
    assert len(transport.calls) == 3 and all(payload == request.question.to_json()
                                            for payload, request in zip(transport.payloads,
                                                                        requests))


def test_a_repeated_question_id_is_sent_and_recorded_twice():
    question = build(CASE_BY_NAME["full_frequency"])
    requests = [JevScoringRequest("candidate-a", question),
                JevScoringRequest("candidate-b", question)]
    transport = RecordingTransport(answer=judgment_text)
    run = score_questions(requests, transport=transport, sleep=DelayRecorder(),
                          monotonic=ScriptedClock([0.0]))
    assert len(run.outcomes) == 2 and len(transport.calls) == 2
    assert run.outcomes[0].question_id == run.outcomes[1].question_id == question.question_id
    assert [outcome.request_id for outcome in run.outcomes] == ["candidate-a", "candidate-b"]
    assert [outcome.attempts for outcome in run.outcomes] == [1, 1]
    assert [outcome.state for outcome in run.outcomes] == ["judged", "judged"]


def test_the_ceiling_is_one_hundred_questions_per_call():
    question = build(CASE_BY_NAME["full_frequency"])
    requests = [JevScoringRequest("candidate-" + str(index), question) for index in range(100)]
    transport = RecordingTransport(answer=judgment_text)
    run = score_questions(requests, transport=transport, sleep=DelayRecorder(),
                          monotonic=ScriptedClock([0.0]))
    assert len(transport.calls) == len(run.outcomes) == 100
    assert len({outcome.question_id for outcome in run.outcomes}) == 1
    assert len({outcome.request_id for outcome in run.outcomes}) == 100
    over = RecordingTransport(answer=judgment_text)
    with pytest.raises(JevAdapterError) as error:
        score_questions(requests + [JevScoringRequest("candidate-100", question)],
                        transport=over, sleep=DelayRecorder(), monotonic=ScriptedClock([0.0]))
    assert error.value.code == "batch_too_large"
    assert over.calls == []


def test_a_rejected_batch_produces_no_request_and_no_outcome():
    question = build(CASE_BY_NAME["full_frequency"])
    bad_batches = [
        ([JevScoringRequest("same", question), JevScoringRequest("same", question)],
         "duplicate_request_id"),
        ([JevScoringRequest("", question)], "invalid_requests"),
        ([JevScoringRequest("   ", question)], "invalid_requests"),
        ([JevScoringRequest(None, question)], "invalid_requests"),
        ([JevScoringRequest("candidate", None)], "invalid_requests"),
        ([JevScoringRequest("candidate", "frequency")], "invalid_requests"),
        ([JevScoringRequest("candidate", {"question_id": "q"})], "invalid_requests"),
        (["not a request"], "invalid_requests"),
        ([JevScoringRequest("candidate", question), 7], "invalid_requests"),
        (None, "invalid_requests"),
        ("requests", "invalid_requests"),
        ({"requests": []}, "invalid_requests"),
        ((request for request in [JevScoringRequest("candidate", question)]), "invalid_requests"),
    ]
    for batch, code in bad_batches:
        transport = RecordingTransport(answer=judgment_text)
        with pytest.raises(JevAdapterError) as error:
            score_questions(batch, transport=transport, sleep=DelayRecorder(),
                            monotonic=ScriptedClock([0.0]))
        assert error.value.code == code, batch
        assert transport.calls == []


def test_a_mismatched_transport_or_injected_callable_is_rejected_before_any_send():
    question = build(CASE_BY_NAME["full_frequency"])
    requests = [JevScoringRequest("candidate", question)]
    for keywords in ({"transport": RecordingTransport(source="other")},
                     {"transport": RecordingTransport(interface_name=None, source=None)},
                     {"transport": object()},
                     {"transport": RecordingTransport(), "config": {}},
                     {"transport": RecordingTransport(), "cancelled": 3},
                     {"transport": RecordingTransport(), "sleep": "sleep"},
                     {"transport": RecordingTransport(), "monotonic": 0},
                     {"transport": RecordingTransport(), "credentials": {}}):
        transport = keywords.get("transport")
        with pytest.raises(JevAdapterError) as error:
            score_questions(requests, environ={}, **keywords)
        assert error.value.code == "invalid_configuration", keywords
        if isinstance(transport, RecordingTransport):
            assert transport.calls == []


@pytest.mark.parametrize("case", UNAVAILABLE_CASES, ids=lambda case: case["name"])
def test_every_question_this_product_refuses_is_never_sent(case):
    question = build(case)
    assert type(question) is UnavailableQuestion
    assert question.code == case["expects"]["code"]
    transport = RecordingTransport(answer=judgment_text)
    run = score_questions([JevScoringRequest("refused", question)], transport=transport,
                          sleep=DelayRecorder(), monotonic=ScriptedClock([0.0]))
    outcome = run.outcomes[0]
    assert (outcome.state, outcome.code, outcome.attempts) == \
        ("not_asked", case["expects"]["code"], 0)
    assert outcome.question_id is None
    assert outcome.dimension == case["dimension"] and outcome.judgment is None
    assert transport.calls == [] and run.model_versions == ()
    assert case["expects"]["code"] in QUESTION_UNAVAILABLE_CODES


def test_every_question_unavailable_code_is_covered_by_that_loop():
    declared = {case["expects"]["code"] for case in UNAVAILABLE_CASES}
    assert declared == set(QUESTION_UNAVAILABLE_CODES)
    assert len(UNAVAILABLE_CASES) == 58


def test_one_failure_never_aborts_the_batch():
    unavailable = build(CASE_BY_NAME["frequency_kick_band_sub_unknown"])
    requests = [JevScoringRequest("candidate-valid", build(CASE_BY_NAME["full_frequency"])),
                JevScoringRequest("candidate-refused", unavailable),
                JevScoringRequest("candidate-malformed", build(CASE_BY_NAME["full_transient"]))]
    transport = RecordingTransport(steps=[judgment_text, rejected_text])
    run = score_questions(requests, transport=transport, sleep=DelayRecorder(),
                          monotonic=ScriptedClock([0.0]))
    assert [outcome.state for outcome in run.outcomes] == ["judged", "not_asked", "invalid_result"]
    assert [outcome.code for outcome in run.outcomes] == [None, unavailable.code,
                                                          "unexpected_field"]
    assert len(transport.calls) == 2


@pytest.mark.parametrize("case", MALFORMED_CASES, ids=lambda case: case["name"])
def test_every_malformed_response_case_is_terminal_verbatim_and_never_retried(case):
    if case in UNREACHABLE_MALFORMED:
        # The adapter never hands #13 a bad question, so these four codes are asserted at the
        # adapter boundary: a bad question is refused before any send, a refused question is
        # recorded as not_asked, and neither makes a request.
        transport = RecordingTransport(answer=judgment_text)
        if "question_value" in case:
            batch = [JevScoringRequest("candidate", None)]
            with pytest.raises(JevAdapterError) as error:
                score_questions(batch, transport=transport, sleep=DelayRecorder(),
                                monotonic=ScriptedClock([0.0]))
            assert error.value.code == "invalid_requests"
        else:
            question = build(CASE_BY_NAME[case["question_case"]])
            assert type(question) is UnavailableQuestion
            run = score_questions([JevScoringRequest("candidate", question)], transport=transport,
                                  sleep=DelayRecorder(), monotonic=ScriptedClock([0.0]))
            assert (run.outcomes[0].state, run.outcomes[0].code, run.outcomes[0].attempts) == \
                ("not_asked", question.code, 0)
        assert transport.calls == []
        return
    question = build(CASE_BY_NAME[case["question_case"]])
    transport = RecordingTransport(steps=[response_text_of(case, question), judgment_text])
    delays = DelayRecorder()
    run = score_questions([JevScoringRequest("candidate", question)], transport=transport,
                          sleep=delays, monotonic=ScriptedClock([0.0]))
    outcome = run.outcomes[0]
    expected = case["expects"]["code"]
    if carries_non_finite(case.get("response")):
        expected = "invalid_response"
    assert (outcome.state, outcome.code, outcome.attempts) == \
        ("invalid_result", expected, 1), case["name"]
    assert outcome.code in RESPONSE_ERROR_CODES and outcome.judgment is None
    assert len(transport.calls) == 1 and delays.delays == []
    assert run.model_versions == ()


def test_every_response_error_code_is_covered_by_that_loop():
    declared = {case["expects"]["code"] for case in WIRE_REACHABLE_MALFORMED}
    assert declared == set(RESPONSE_ERROR_CODES) - {"invalid_question", "question_unavailable"}
    assert len(MALFORMED_CASES) == 53


def test_an_accepted_response_is_recorded_unchanged():
    case = CASE_BY_NAME["frequency_low_band_contradiction"]
    question = build(case)
    body = judgment_text(question.to_json(), probabilities=(0.0, 0.02, 0.08, 0.30, 0.60),
                         confidence=0.42, model_version="synthetic-model-contradiction")
    transport = RecordingTransport(steps=[body])
    run = score_questions([JevScoringRequest("candidate", question)], transport=transport,
                          sleep=DelayRecorder(), monotonic=ScriptedClock([0.0]))
    outcome = run.outcomes[0]
    assert outcome.state == "judged" and outcome.code is None and outcome.attempts == 1
    assert outcome.judgment == validate_response(question, body)
    assert outcome.judgment.label == "excellent"
    assert [item.probability for item in outcome.judgment.probabilities] == [0.0, 0.02, 0.08,
                                                                            0.30, 0.60]
    assert outcome.judgment.confidence == 0.42
    assert run.model_versions == ("synthetic-model-contradiction",)


def test_an_abstention_is_recorded_with_the_models_own_version():
    question = build(CASE_BY_NAME["full_tonal"])
    transport = RecordingTransport(steps=[abstention_text])
    run = score_questions([JevScoringRequest("candidate", question)], transport=transport,
                          sleep=DelayRecorder(), monotonic=ScriptedClock([0.0]))
    outcome = run.outcomes[0]
    assert outcome.state == "abstained" and outcome.code is None
    assert outcome.judgment.label is None
    assert outcome.judgment.unavailable_reason == MODEL_ABSTAINED
    assert run.model_versions == ("synthetic-model-1",)


def test_the_four_code_spaces_are_disjoint():
    spaces = {"transport": set(TRANSPORT_ERROR_CODES),
              "question_unavailable": set(QUESTION_UNAVAILABLE_CODES),
              "response": set(RESPONSE_ERROR_CODES),
              "not_attempted": set(NOT_ATTEMPTED_CODES),
              "unavailable": set(UNAVAILABLE_CODES)}
    names = list(spaces)
    for index, left in enumerate(names):
        for right in names[index + 1:]:
            assert not spaces[left] & spaces[right], (left, right)
    # service_error is deliberately both one state and one transport code; nothing else
    # in a code space is a state name.
    assert set(TRANSPORT_ERROR_CODES) & set(OUTCOME_STATES) == {"service_error"}
    assert not set(QUESTION_UNAVAILABLE_CODES) & set(OUTCOME_STATES)
    assert not set(RESPONSE_ERROR_CODES) & set(OUTCOME_STATES)
    assert not set(NOT_ATTEMPTED_CODES) & set(OUTCOME_STATES)
    assert not set(UNAVAILABLE_CODES) & set(OUTCOME_STATES)


def test_timeout_s_is_passed_and_reduced_near_the_deadline():
    request = requests_for(["full_frequency"])[0]
    transport = RecordingTransport(steps=[JevTransportError("timeout", "synthetic"),
                                          judgment_text])
    clock = ScriptedClock([0.0, 0.0, 105.0, 105.0])
    delays = DelayRecorder()
    run = score_questions([request], transport=transport,
                          config=JevAdapterConfig(timeout_s=DEFAULT_TIMEOUT_S,
                                                  max_batch_seconds=120.0),
                          sleep=delays, monotonic=clock)
    assert transport.timeouts == [20.0, 15.0]
    assert delays.delays == [DEFAULT_BACKOFF_S]
    assert run.outcomes[0].state == "judged" and run.outcomes[0].attempts == 2
    assert run.elapsed_ms == 105000 and run.outcomes[0].elapsed_ms == 105000


def test_a_timeout_is_retried_and_ends_as_timed_out():
    transport = RecordingTransport(steps=[JevTransportError("timeout", "synthetic")] * 3)
    delays = DelayRecorder()
    run = score_questions(requests_for(["full_frequency"]), transport=transport, sleep=delays,
                          monotonic=ScriptedClock([0.0]))
    outcome = run.outcomes[0]
    assert (outcome.state, outcome.code, outcome.attempts) == ("timed_out", "timeout", 3)
    assert len(transport.calls) == 3 and delays.delays == [0.5, 1.0]
    assert outcome.judgment is None


def test_the_final_outcome_reflects_the_last_attempt():
    transport = RecordingTransport(steps=[JevTransportError("timeout", "synthetic"),
                                          JevTransportError("service_error", "synthetic")])
    delays = DelayRecorder()
    run = score_questions(requests_for(["full_frequency"]), transport=transport,
                          config=JevAdapterConfig(max_attempts=2), sleep=delays,
                          monotonic=ScriptedClock([0.0]))
    assert (run.outcomes[0].state, run.outcomes[0].code, run.outcomes[0].attempts) == \
        ("service_error", "service_error", 2)
    assert delays.delays == [0.5] and len(transport.calls) == 2


def test_backoff_is_the_documented_formula_through_the_injected_sleep():
    failures = [JevTransportError("service_error", "synthetic"),
                JevTransportError("rate_limited", "synthetic")]
    transport = RecordingTransport(steps=failures + [judgment_text, judgment_text])
    delays = DelayRecorder()
    run = score_questions(requests_for(["full_frequency", "full_transient"]),
                          transport=transport, sleep=delays, monotonic=ScriptedClock([0.0]))
    assert len(transport.calls) == 4
    assert delays.delays == [0.5, 1.0]
    assert delays.delays == [DEFAULT_BACKOFF_S * BACKOFF_MULTIPLIER ** 0,
                             DEFAULT_BACKOFF_S * BACKOFF_MULTIPLIER ** 1]
    assert [outcome.state for outcome in run.outcomes] == ["judged", "judged"]
    assert [outcome.attempts for outcome in run.outcomes] == [3, 1]


def test_the_backoff_wait_is_clamped_by_max_backoff_s():
    transport = RecordingTransport(steps=[JevTransportError("connection_failed", "synthetic")] * 3)
    delays = DelayRecorder()
    run = score_questions(requests_for(["full_frequency"]), transport=transport,
                          config=JevAdapterConfig(backoff_s=5.0, max_backoff_s=6.0,
                                                  max_attempts=3),
                          sleep=delays, monotonic=ScriptedClock([0.0]))
    assert delays.delays == [5.0, 6.0]
    assert (run.outcomes[0].state, run.outcomes[0].code) == ("service_error", "connection_failed")
    assert run.outcomes[0].attempts == 3


def test_invalid_credentials_is_never_retried():
    transport = RecordingTransport(steps=[JevTransportError("invalid_credentials", "synthetic")])
    delays = DelayRecorder()
    run = score_questions(requests_for(["full_frequency"]), transport=transport, sleep=delays,
                          monotonic=ScriptedClock([0.0]))
    assert len(transport.calls) == 1 and delays.delays == []
    assert (run.outcomes[0].state, run.outcomes[0].code, run.outcomes[0].attempts) == \
        ("service_error", "invalid_credentials", 1)


def test_an_unknown_transport_code_is_recorded_as_a_service_error():
    transport = RecordingTransport(steps=[JevTransportError("synthetic_unknown", "synthetic")] * 3)
    delays = DelayRecorder()
    run = score_questions(requests_for(["full_frequency"]), transport=transport,
                          config=JevAdapterConfig(max_attempts=1), sleep=delays,
                          monotonic=ScriptedClock([0.0]))
    assert delays.delays == []
    assert (run.outcomes[0].state, run.outcomes[0].code, run.outcomes[0].attempts) == \
        ("service_error", "service_error", 1)
    assert len(transport.calls) == 1


def test_cancellation_after_the_first_response_keeps_every_outcome():
    requests = requests_for(["full_frequency", "full_transient", "full_tonal"])
    transport = RecordingTransport(answer=judgment_text)
    delays = DelayRecorder()
    run = score_questions(requests, transport=transport,
                          cancelled=lambda: len(transport.calls) >= 1, sleep=delays,
                          monotonic=ScriptedClock([0.0]))
    assert len(transport.calls) == 1 and delays.delays == []
    assert run.cancelled is True
    assert [outcome.state for outcome in run.outcomes] == ["judged", "not_attempted",
                                                           "not_attempted"]
    assert [outcome.code for outcome in run.outcomes] == [None, "cancelled", "cancelled"]
    assert [outcome.attempts for outcome in run.outcomes] == [1, 0, 0]
    assert run.outcomes[0].judgment is not None
    assert len(run.outcomes) == len(requests)


def test_cancellation_between_attempts_records_the_sends_already_made():
    transport = RecordingTransport(steps=[JevTransportError("service_error", "synthetic"),
                                          judgment_text])
    delays = DelayRecorder()
    run = score_questions(requests_for(["full_frequency"]), transport=transport,
                          cancelled=lambda: len(transport.calls) >= 1, sleep=delays,
                          monotonic=ScriptedClock([0.0]))
    assert len(transport.calls) == 1 and delays.delays == []
    assert [(outcome.state, outcome.code, outcome.attempts) for outcome in run.outcomes] == \
        [("not_attempted", "cancelled", 1)]
    assert run.cancelled is True


def test_cancellation_before_the_first_send_sends_nothing():
    requests = requests_for(["full_frequency", "full_transient"])
    transport = RecordingTransport(answer=judgment_text)
    run = score_questions(requests, transport=transport, cancelled=lambda: True,
                          sleep=DelayRecorder(), monotonic=ScriptedClock([0.0]))
    assert transport.calls == []
    assert [(outcome.state, outcome.code, outcome.attempts) for outcome in run.outcomes] == \
        [("not_attempted", "cancelled", 0), ("not_attempted", "cancelled", 0)]
    assert run.cancelled is True and run.model_versions == ()


def test_the_deadline_is_absolute_and_comes_from_the_injected_clock():
    requests = requests_for(["full_frequency", "full_transient", "full_tonal"])
    transport = RecordingTransport(answer=judgment_text)
    clock = ScriptedClock([0.0, 0.0, 1.0, 999.0, 999.0, 999.0, 999.0])
    delays = DelayRecorder()
    run = score_questions(requests, transport=transport,
                          config=JevAdapterConfig(max_batch_seconds=10.0), sleep=delays,
                          monotonic=clock)
    assert [outcome.state for outcome in run.outcomes] == ["judged", "not_attempted",
                                                           "not_attempted"]
    assert [outcome.code for outcome in run.outcomes] == [None, "batch_deadline_exceeded",
                                                          "batch_deadline_exceeded"]
    assert [outcome.attempts for outcome in run.outcomes] == [1, 0, 0]
    assert len(transport.calls) == 1 and transport.timeouts == [10.0]
    assert delays.delays == []
    assert run.elapsed_ms == 999000
    assert [outcome.elapsed_ms for outcome in run.outcomes] == [1000, 999000, 999000]
    assert run.cancelled is False


def test_the_deadline_keeps_the_attempts_already_made():
    transport = RecordingTransport(steps=[JevTransportError("service_error", "synthetic"),
                                          judgment_text])
    clock = ScriptedClock([0.0, 0.0, 11.0])
    delays = DelayRecorder()
    run = score_questions(requests_for(["full_frequency", "full_transient"]), transport=transport,
                          config=JevAdapterConfig(max_batch_seconds=10.0), sleep=delays,
                          monotonic=clock)
    assert len(transport.calls) == 1
    assert delays.delays == [0.5]
    assert (run.outcomes[0].state, run.outcomes[0].code, run.outcomes[0].attempts) == \
        ("not_attempted", "batch_deadline_exceeded", 1)
    assert (run.outcomes[1].state, run.outcomes[1].code, run.outcomes[1].attempts) == \
        ("not_attempted", "batch_deadline_exceeded", 0)


def test_the_run_records_the_adapter_contract():
    requests, transport, run, delays, clock = recording_run(["full_frequency", "full_transient"])
    assert run.adapter_version == ADAPTER_VERSION
    assert run.source == "double" and run.interface_name == "recording-transport"
    assert run.prompt_version == PROMPT_VERSION
    assert run.model_versions == ("synthetic-model-1",)
    assert run.cancelled is False
    assert run.elapsed_ms >= 0 and isinstance(run.elapsed_ms, int)
    assert [outcome.request_id for outcome in run.outcomes] == [request.request_id
                                                                for request in requests]
    assert all(outcome.elapsed_ms >= 0 for outcome in run.outcomes)


def test_model_versions_come_from_accepted_judgments_and_abstentions_only():
    question = build(CASE_BY_NAME["full_frequency"])
    transport = RecordingTransport(steps=[
        lambda payload: judgment_text(payload, model_version="synthetic-zeta"),
        lambda payload: abstention_text(payload, model_version="synthetic-alpha"),
        lambda payload: judgment_text(payload, model_version="synthetic-zeta"),
        lambda payload: rejected_text(payload, model_version="synthetic-ignored"),
    ])
    run = score_questions([JevScoringRequest("candidate-" + str(index), question)
                           for index in range(4)],
                          transport=transport, sleep=DelayRecorder(),
                          monotonic=ScriptedClock([0.0]))
    assert [outcome.state for outcome in run.outcomes] == ["judged", "abstained", "judged",
                                                           "invalid_result"]
    assert run.model_versions == ("synthetic-alpha", "synthetic-zeta")


def test_records_round_trip_and_carry_no_credential_endpoint_or_identity():
    credentials = JevCredentials(endpoint=ENDPOINT, api_key=API_KEY)
    opener = FakeOpener([FakeResponse(b"{}")])
    transport = HttpJevTransport(credentials, opener=opener)
    transport.send(PAYLOAD, timeout_s=1.0)
    question = build(CASE_BY_NAME["full_frequency"])
    transport = RecordingTransport(steps=[judgment_text])
    run = score_questions([JevScoringRequest("candidate", question)], transport=transport,
                          sleep=DelayRecorder(), monotonic=ScriptedClock([0.0]))
    text = run.to_json()
    assert list(json.loads(text)) == sorted(json.loads(text))
    assert JevScoringRun.from_json(text).to_json() == text
    for outcome in run.outcomes:
        assert JevOutcome.from_json(outcome.to_json()).to_json() == outcome.to_json()
        assert list(json.loads(outcome.to_json())) == sorted(json.loads(outcome.to_json()))
    for forbidden in (API_KEY, ENDPOINT, "Authorization", "Bearer", "local_path", ".wav",
                      "frame_count", "synthetic-adapter/side.wav"):
        assert forbidden not in text
    assert set(json.loads(text)) == {"adapter_version", "source", "interface_name",
                                     "prompt_version", "model_versions", "cancelled", "outcomes",
                                     "elapsed_ms"}


def test_records_are_frozen_and_reject_malformed_payloads():
    requests, transport, run, delays, clock = recording_run(["full_frequency"])
    with pytest.raises(FrozenInstanceError):
        run.source = "double"
    with pytest.raises(FrozenInstanceError):
        run.outcomes[0].state = "unavailable"
    for payload in ({}, {"adapter_version": ADAPTER_VERSION}, {"outcomes": []},
                    {"adapter_version": ADAPTER_VERSION, "source": "double"}):
        with pytest.raises(JevAdapterError) as error:
            JevScoringRun.from_dict(payload)
        assert error.value.code == "invalid_configuration"
    with pytest.raises(JevAdapterError):
        JevScoringRun.from_json("not json")
    with pytest.raises(JevAdapterError):
        JevOutcome.from_dict({"request_id": "candidate"})
    assert run.to_dict()["outcomes"] == [outcome.to_dict() for outcome in run.outcomes]


def test_a_run_record_rejects_an_out_of_contract_state_and_judgment():
    good = JevOutcome(request_id="candidate", question_id="q-synthetic", dimension="frequency",
                      state="timed_out", code="timeout", attempts=1, judgment=None, elapsed_ms=0)
    assert JevOutcome.from_dict(good.to_dict()) == good
    for broken in ({"state": "synthetic"}, {"state": "judged", "code": "timeout"},
                   {"state": "not_asked", "code": "credentials_absent"},
                   {"state": "timed_out", "code": "service_error"},
                   {"state": "unavailable", "code": "cancelled"},
                   {"state": "service_error", "code": "timeout"},
                   {"state": "not_attempted", "code": "cancelled", "attempts": -1}):
        payload = good.to_dict()
        payload.update(broken)
        with pytest.raises(JevAdapterError) as error:
            JevOutcome.from_dict(payload)
        assert error.value.code == "invalid_configuration", broken


class FakeResponse:
    """A local stand-in for an HTTP response object."""

    def __init__(self, body, error=None):
        self.body = body
        self.error = error
        self.closed = False

    def read(self):
        if self.error is not None:
            raise self.error
        return self.body

    def close(self):
        self.closed = True


class FakeOpener:
    """A local opener stand-in: records every call and replays scripted answers."""

    def __init__(self, answers=()):
        self.answers = list(answers)
        self.calls = []

    def open(self, request, timeout=None):
        self.calls.append((request, timeout))
        assert self.answers, "unexpected opener call"
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


def test_the_http_transport_posts_the_payload_with_bearer_credentials():
    credentials = JevCredentials(endpoint=ENDPOINT, api_key=API_KEY)
    response = FakeResponse(b'{"synthetic": "response"}')
    opener = FakeOpener([response])
    transport = HttpJevTransport(credentials, opener=opener)
    assert transport.interface_name == "typesafe-jev-http" and transport.source == "interface"
    assert isinstance(transport, JevTransport)
    text = transport.send(PAYLOAD, timeout_s=7.5)
    assert text == '{"synthetic": "response"}'
    assert response.closed is True
    ((request, timeout),) = opener.calls
    assert request.get_method() == "POST"
    assert request.full_url == ENDPOINT
    assert request.data == PAYLOAD.encode("utf-8")
    assert request.headers == {"Content-Type": "application/json",
                              "Authorization": "Bearer " + API_KEY}
    assert request.get_header("Content-Type") == "application/json"
    assert request.get_header("Authorization") == "Bearer " + API_KEY
    assert timeout == 7.5


def test_the_adapter_passes_the_exact_timeout_to_each_http_attempt():
    credentials = JevCredentials(endpoint=ENDPOINT, api_key=API_KEY)
    opener = FakeOpener([urllib.error.URLError("synthetic"), FakeResponse(b"{}")])
    transport = HttpJevTransport(credentials, opener=opener)
    run = score_questions(requests_for(["full_frequency"]), transport=transport,
                          sleep=DelayRecorder(), monotonic=ScriptedClock([0.0, 0.0, 100.0, 100.0]))
    assert [timeout for _, timeout in opener.calls] == [20.0, 20.0]
    assert transport.source == "interface" and run.source == "interface"
    assert run.interface_name == "typesafe-jev-http"


@pytest.mark.parametrize("status,code", [
    (401, "invalid_credentials"), (403, "invalid_credentials"), (429, "rate_limited"),
    (503, "service_unavailable"), (504, "service_unavailable"), (302, "service_error"),
    (301, "service_error"), (304, "service_error"), (400, "service_error"),
    (404, "service_error"), (409, "service_error"), (418, "service_error"),
    (500, "service_error"), (502, "service_error"), (505, "service_error"),
])
def test_every_status_maps_to_its_documented_transport_code(status, code):
    credentials = JevCredentials(endpoint=ENDPOINT, api_key=API_KEY)
    opener = FakeOpener([urllib.error.HTTPError(ENDPOINT, status, "synthetic", {}, None)])
    transport = HttpJevTransport(credentials, opener=opener)
    with pytest.raises(JevTransportError) as error:
        transport.send(PAYLOAD, timeout_s=1.0)
    assert error.value.code == code
    assert error.value.code in TRANSPORT_ERROR_CODES
    assert API_KEY not in str(error.value) and ENDPOINT not in str(error.value)


@pytest.mark.parametrize("failure,code", [
    (TimeoutError("synthetic timeout"), "timeout"),
    (urllib.error.URLError(TimeoutError("synthetic timeout")), "timeout"),
    (urllib.error.URLError("synthetic failure"), "connection_failed"),
    (urllib.error.URLError(OSError("synthetic failure")), "connection_failed"),
    (OSError("synthetic failure"), "connection_failed"),
    (ConnectionResetError("synthetic reset"), "connection_failed"),
])
def test_every_failure_type_maps_to_its_documented_transport_code(failure, code):
    credentials = JevCredentials(endpoint=ENDPOINT, api_key=API_KEY)
    transport = HttpJevTransport(credentials, opener=FakeOpener([failure]))
    with pytest.raises(JevTransportError) as error:
        transport.send(PAYLOAD, timeout_s=1.0)
    assert error.value.code == code


def test_an_undecodable_body_is_a_service_error_and_a_2xx_body_is_returned():
    credentials = JevCredentials(endpoint=ENDPOINT, api_key=API_KEY)
    transport = HttpJevTransport(credentials, opener=FakeOpener([FakeResponse(b"\xff\xfe")]))
    with pytest.raises(JevTransportError) as error:
        transport.send(PAYLOAD, timeout_s=1.0)
    assert error.value.code == "service_error"
    transport = HttpJevTransport(credentials, opener=FakeOpener([FakeResponse(b"not json")]))
    assert transport.send(PAYLOAD, timeout_s=1.0) == "not json"
    transport = HttpJevTransport(credentials, opener=FakeOpener(
        [FakeResponse(b"{}", error=TimeoutError("synthetic"))]))
    with pytest.raises(JevTransportError) as error:
        transport.send(PAYLOAD, timeout_s=1.0)
    assert error.value.code == "timeout"


def test_redirects_cannot_move_the_payload():
    assert issubclass(NoRedirectHandler, urllib.request.HTTPRedirectHandler)
    handler = NoRedirectHandler()
    assert handler.redirect_request(None, None, 302, "Found", {},
                                    "https://elsewhere.example.invalid") is None
    opener = urllib.request.build_opener(NoRedirectHandler)
    assert any(type(item) is NoRedirectHandler for item in opener.handlers)
    credentials = JevCredentials(endpoint=ENDPOINT, api_key=API_KEY)
    transport = HttpJevTransport(credentials)
    assert any(type(item) is NoRedirectHandler for item in transport.opener.handlers)


def test_no_test_contacts_a_service_or_opens_a_socket():
    source = JEV_FILE.read_text(encoding="utf-8")
    assert "urlopen" not in source and "socket" not in source
    credentials = JevCredentials(endpoint="http://127.0.0.1:1/jev", api_key=API_KEY)
    opener = FakeOpener([FakeResponse(b"local only")])
    transport = HttpJevTransport(credentials, opener=opener)
    assert transport.send(PAYLOAD, timeout_s=1.0) == "local only"
    assert len(opener.calls) == 1


def test_the_fixture_transport_never_reaches_the_network():
    double = JevContractDouble()
    body = double.send(PAYLOAD, timeout_s=3.0)
    assert json.loads(body)["question_id"] == "q-synthetic"
    assert double.calls == [(PAYLOAD, 3.0)]


def test_the_captured_requests_are_exactly_what_the_adapter_sends():
    assert len(CAPTURED) >= 3
    names = [record["name"] for record in CAPTURED]
    assert len(names) == len(set(names))
    assert "frequency_payload_carries_no_sample_identity" in names
    requests = [JevScoringRequest(request_id=record["request_id"],
                                  question=build(CASE_BY_NAME[record["question_case"]]))
                for record in CAPTURED]
    transport = RecordingTransport(answer=abstention_text)
    run = score_questions(requests, transport=transport, sleep=DelayRecorder(),
                          monotonic=ScriptedClock([0.0]))
    assert len(transport.calls) == len(CAPTURED)
    samples = set()
    for record, request, (sent, timeout) in zip(CAPTURED, requests, transport.calls):
        case = CASE_BY_NAME[record["question_case"]]
        samples.add(case["kick"]["sample_id"])
        samples.add(case["candidate"]["sample_id"])
        assert sent == request.question.to_json() == record["payload_json"]
        assert set(json.loads(sent)) == {"question_id", "dimension", "prompt_version",
                                         "instruction", "evidence", "withheld"}
        assert json.loads(sent)["prompt_version"] == PROMPT_VERSION
        assert request.question.question_id == json.loads(sent)["question_id"]
    everything = json.dumps(CAPTURED) + json.dumps(transport.payloads) + run.to_json()
    for token in IDENTITY_TOKENS + tuple(sorted(samples)):
        assert token not in everything, token
    assert "\x00" not in everything and "base64" not in everything \
        and "data:audio" not in everything


def batch_requests(entry_batch):
    requests = []
    for entry in entry_batch:
        if "question_case" in entry:
            question = build(CASE_BY_NAME[entry["question_case"]])
        else:
            code = entry["unavailable"]
            case = next(case for case in UNAVAILABLE_CASES if case["expects"]["code"] == code)
            question = build(case)
        requests.append(JevScoringRequest(request_id=entry.get("request_id"), question=question))
    return requests


def double_script(requests, case):
    scripted = case.get("script") or {}
    script = {}
    for request in requests:
        entries = scripted.get(request.request_id)
        if entries is None:
            continue
        question = request.question
        assert type(question) is JevQuestion, request.request_id
        script.setdefault(question.question_id, []).extend(
            entries if type(entries) is list else [entries])
    return script


def run_adapter_case(case):
    requests = batch_requests(case["batch"])
    double = JevContractDouble(script=double_script(requests, case))
    cancel_after = case.get("cancel_after")
    cancelled = (None if cancel_after is None
                 else (lambda: len(double.calls) >= cancel_after))
    delays = DelayRecorder()
    clock = ScriptedClock(case.get("clock", [0.0]))
    arguments = {"config": None, "cancelled": cancelled, "sleep": delays, "monotonic": clock}
    try:
        config = JevAdapterConfig(**case.get("config", {}))
        arguments["config"] = config
        if case.get("credentials") == "absent":
            run = score_questions(requests, transport=None, credentials=None, environ={},
                                  **arguments)
        else:
            run = score_questions(requests, transport=double, **arguments)
    except JevAdapterError as error:
        return requests, double, None, error
    return requests, double, run, None


@pytest.mark.parametrize("case", ADAPTER_CASES, ids=lambda case: case["name"])
def test_every_adapter_fixture_case(case):
    requests, double, run, error = run_adapter_case(case)
    expects = case["expects"]
    if "error" in expects:
        assert error is not None and error.code == expects["error"], case["name"]
        assert double.calls == []
        return
    assert error is None, case["name"]
    assert run.adapter_version == ADAPTER_VERSION
    assert run.prompt_version == PROMPT_VERSION
    assert run.source == expects["source"]
    assert run.source in TRANSPORT_SOURCES or run.source == "unavailable"
    assert run.interface_name == expects.get("interface_name")
    assert len(run.outcomes) == len(case["batch"])
    assert [(outcome.state, outcome.code, outcome.attempts) for outcome in run.outcomes] == \
        [(expected["state"], expected["code"], expected["attempts"])
         for expected in expects["outcomes"]]
    assert len(double.calls) == expects["sends"]
    assert run.cancelled == expects.get("cancelled", False)
    if "model_versions" in expects:
        assert list(run.model_versions) == expects["model_versions"]
    if "elapsed_ms" in expects:
        assert run.elapsed_ms == expects["elapsed_ms"]
    sendable = [request for request in requests if type(request.question) is JevQuestion]
    for request, (sent, timeout) in zip(sendable, double.calls):
        assert sent == request.question.to_json()
        assert timeout <= DEFAULT_TIMEOUT_S
    assert JevScoringRun.from_json(run.to_json()) == run
    for outcome in run.outcomes:
        assert JevOutcome.from_json(outcome.to_json()) == outcome
        assert outcome.request_id is not None and outcome.dimension


def test_the_adapter_fixture_uses_only_the_documented_entry_forms():
    for case in ADAPTER_CASES:
        assert case["name"] and case["expects"]
        for entry in case["batch"]:
            assert set(entry) <= {"request_id", "question_case", "unavailable"}
            assert ("question_case" in entry) != ("unavailable" in entry)
            if "unavailable" in entry:
                assert entry["unavailable"] in QUESTION_UNAVAILABLE_CODES
            else:
                assert entry["question_case"] in CASE_BY_NAME
        for entries in (case.get("script") or {}).values():
            for entry in (entries if type(entries) is list else [entries]):
                assert len(entry) == 1 and next(iter(entry)) in ("judgment", "abstain", "text",
                                                                 "error")


def test_the_adapter_fixture_exercises_every_code_and_state():
    states, transport, not_attempted, unavailable, adapter_errors = set(), set(), set(), set(), set()
    for case in ADAPTER_CASES:
        if "error" in case["expects"]:
            adapter_errors.add(case["expects"]["error"])
            continue
        for outcome in case["expects"]["outcomes"]:
            states.add(outcome["state"])
            if outcome["state"] in ("service_error", "timed_out"):
                transport.add(outcome["code"])
            elif outcome["state"] == "not_attempted":
                not_attempted.add(outcome["code"])
            elif outcome["state"] == "unavailable":
                unavailable.add(outcome["code"])
    assert states == set(OUTCOME_STATES)
    assert transport == set(TRANSPORT_ERROR_CODES)
    assert not_attempted == set(NOT_ATTEMPTED_CODES)
    assert unavailable == set(UNAVAILABLE_CODES)
    assert adapter_errors == set(ADAPTER_ERROR_CODES)


def check_outcome_code_space(outcome):
    if outcome.state in ("judged", "abstained"):
        assert outcome.code is None and type(outcome.judgment) is JevJudgment
        assert outcome.attempts >= 1
    elif outcome.state == "not_asked":
        assert outcome.code in QUESTION_UNAVAILABLE_CODES and outcome.attempts == 0
        assert outcome.question_id is None and outcome.judgment is None
    elif outcome.state == "not_attempted":
        assert outcome.code in NOT_ATTEMPTED_CODES and outcome.judgment is None
    elif outcome.state == "invalid_result":
        assert outcome.code in RESPONSE_ERROR_CODES and outcome.attempts >= 1
        assert outcome.judgment is None
    elif outcome.state == "service_error":
        assert outcome.code in TRANSPORT_ERROR_CODES and outcome.code != "timeout"
        assert outcome.attempts >= 1 and outcome.judgment is None
    elif outcome.state == "timed_out":
        assert outcome.code == "timeout" and outcome.attempts >= 1 and outcome.judgment is None
    else:
        assert outcome.code in UNAVAILABLE_CODES and outcome.attempts == 0
        assert outcome.judgment is None


def test_every_fixture_outcome_obeys_its_state_invariants():
    seen = {}
    for case in ADAPTER_CASES:
        requests, double, run, error = run_adapter_case(case)
        if run is None:
            continue
        for outcome in run.outcomes:
            check_outcome_code_space(outcome)
            seen.setdefault(outcome.state, set()).add(outcome.code)
    assert set(seen) == set(OUTCOME_STATES)
    assert seen["timed_out"] == {"timeout"}
    assert seen["unavailable"] == set(UNAVAILABLE_CODES)
    assert seen["not_attempted"] == set(NOT_ATTEMPTED_CODES)


def test_the_live_integration_check_is_exactly_one_gated_test():
    source = (ROOT / "tests" / "test_jev_integration.py").read_text(encoding="utf-8")
    assert source.count("def test_") == 1
    assert "skipif" in source and "UNVERIFIED" in source
    assert ENV_ENDPOINT in source and ENV_API_KEY in source
    assert "max_attempts=1" in source
    assert "timeout_s=MAX_LIVE_TIMEOUT_S" in source and "max_batch_seconds=MAX_LIVE_BATCH_SECONDS" \
        in source
    assert "TERA_JEV_ENDPOINT/TERA_JEV_API_KEY are not set" in source
    assert 'run.source == "interface"' in source


def test_the_document_agrees_with_every_constant_and_bound():
    text = DOCUMENT.read_text(encoding="utf-8")
    rows = data_rows(text, "## Constants", 3)
    documented = {row[0].strip(TICK): row[1].strip(TICK) for row in rows}
    for name in DOCUMENTED_CONSTANTS:
        assert name in documented, name
        assert documented[name] == rendered(getattr(jev_module, name)), name
    import backend.intelligence.jev_double as double_module
    for name in DOCUMENTED_DOUBLE_CONSTANTS:
        assert name in documented, name
        assert documented[name] == rendered(getattr(double_module, name)), name
    bounds = {row[0].strip(TICK): row for row in data_rows(text, "## Bounds", 4)}
    for field in ("timeout_s", "max_attempts", "backoff_s", "max_backoff_s", "max_batch_seconds",
                  "max_batch_size"):
        assert field in bounds, field
        row = [cell.strip(TICK) for cell in bounds[field]]
        default = getattr(JevAdapterConfig(), field)
        assert row[1] == rendered(default) or row[1] == str(default), (field, row)
    for name in ("MIN_TIMEOUT_S", "MAX_TIMEOUT_S", "MIN_MAX_ATTEMPTS", "MAX_MAX_ATTEMPTS",
                 "MIN_MAX_BATCH_SECONDS", "MAX_MAX_BATCH_SECONDS"):
        assert name in text


def test_the_document_records_the_state_and_code_tables():
    text = DOCUMENT.read_text(encoding="utf-8")
    states = {row[0].strip(TICK) for row in data_rows(text, "## Outcome states", 3)}
    assert states == set(OUTCOME_STATES)
    assert all(row[2].strip() for row in data_rows(text, "## Outcome states", 3))
    for heading, codes, state in (("## Transport error codes", TRANSPORT_ERROR_CODES, None),
                                  ("## Not-attempted codes", NOT_ATTEMPTED_CODES, "not_attempted"),
                                  ("## Unavailable codes", UNAVAILABLE_CODES, "unavailable")):
        rows = data_rows(text, heading, 3)
        assert {row[0].strip(TICK) for row in rows} == set(codes), heading
        assert all(row[2].strip() for row in rows), heading
        if state is not None:
            if heading == "## Transport error codes":
                assert {row[0].strip(TICK): row[1].strip(TICK) for row in rows}["timeout"] \
                    == "timed_out"
                assert all(row[1].strip(TICK) in ("service_error", "timed_out") for row in rows)
            else:
                assert all(row[1].strip(TICK) == state for row in rows), heading
    adapter = data_rows(text, "## Adapter error codes", 3)
    assert {row[0].strip(TICK) for row in adapter} == set(ADAPTER_ERROR_CODES)
    assert all(row[1].strip() and row[2].strip() for row in adapter)
    refused = data_rows(text, "## Question-unavailable codes", 3)
    assert {row[0].strip(TICK) for row in refused} == set(QUESTION_UNAVAILABLE_CODES)
    assert all(row[1].strip(TICK) == "not_asked" and row[2].strip() for row in refused)
    response = data_rows(text, "## Response error codes", 3)
    assert {row[0].strip(TICK) for row in response} == set(RESPONSE_ERROR_CODES)
    assert all(row[1].strip(TICK) == "invalid_result" and row[2].strip() for row in response)


def test_the_document_records_the_names_and_rules_it_must():
    text = DOCUMENT.read_text(encoding="utf-8")
    for name in (ENV_ENDPOINT, ENV_API_KEY, ENV_TIMEOUT_S, ENV_MAX_ATTEMPTS, ENV_MAX_BATCH_SECONDS):
        assert name in text
    for name in ("tests/fixtures/jev/adapter-cases.json", "tests/fixtures/jev/captured-requests.json",
                 "tests/fixtures/jev/question-cases.json", "tests/fixtures/jev/response-cases.json",
                 "tests/fixtures/jev/malformed-responses.json"):
        assert name in text
    for phrase in ("assumed", "<redacted>", "MAX_BATCH_SIZE", "questions per call",
                   "max_batch_seconds + timeout_s", "140 s", "MAX_TIMEOUT_S = 120.0",
                   "contract evidence", "must never be reported as live Jev lift",
                   "without asking the service anything", "score_questions(", "JevContractDouble(",
                   "backend.intelligence.jev_double", "request_id", "question_id",
                   "zzz-distinctive-9f8a",
                   "real TypeSafe Jev integration: UNVERIFIED - "
                   "TERA_JEV_ENDPOINT/TERA_JEV_API_KEY are not set",
                   "uv run pytest tests/test_jev_adapter.py tests/test_jev_double.py "
                   "--basetemp .pytest_cache/jev-adapter",
                   "uv run pytest tests/test_jev_integration.py "
                   "--basetemp .pytest_cache/jev-integration",
                   "uv run pytest --basetemp .pytest_cache/jev-full",
                   "NoRedirectHandler", "invalid_configuration", "batch_too_large",
                   "duplicate_request_id", "invalid_requests", "credentials_absent",
                   "cancelled", "batch_deadline_exceeded"):
        assert phrase in text, phrase
    assert "0.5" in text and "1.0" in text and "8.0" in text
    assert "test_batch.py" in text and "test_evaluation_manifest.py" in text
