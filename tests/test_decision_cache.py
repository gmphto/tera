"""The versioned decision cache (issue #26); every fixture and id is synthetic.

No real library path, sample name, content fingerprint, credential, endpoint or
audio byte appears here. Every database lives in tmp_path and every sample,
question and decision is built from the landed synthetic fixtures
(tests/fixtures/jev/question-cases.json, tests/fixtures/jev/adapter-cases.json,
tests/fixtures/contracts/hybrid.json and tests/fixtures/hybrid/hybrid-cases.json).
The cache is driven by tests/fixtures/cache/cache-cases.json and the key
component matrix by tests/fixtures/cache/key-matrix.json; both expectations are
written by hand and the tests load them rather than restating them.
"""

from __future__ import annotations

import ast
import json
import sqlite3
from dataclasses import fields
from pathlib import Path

import pytest

from backend.analysis import batch
from backend.analysis.batch import canonical, digest
from backend.contracts import (AudioFeatures, AudioMetadata, Dimension, JevJudgment,
                               LabelProbability, MEASURES, Measurement, MusicalKey,
                               RecommendationBatch, Sample, SongContext)
from backend.intelligence import decisions as decisions_module
from backend.intelligence import jev as jev_module
from backend.intelligence.decisions import MODEL_ABSTAINED, validate_response
from backend.intelligence.jev import (ADAPTER_VERSION, NOT_ATTEMPTED_CODES, OUTCOME_STATES,
                                      TRANSPORT_ERROR_CODES, TRANSPORT_SOURCES, UNAVAILABLE_CODES,
                                      JevAdapterConfig, JevOutcome, JevScoringRequest,
                                      JevScoringRun, score_questions)
from backend.intelligence.jev_double import JevContractDouble
from backend.intelligence.questions import (DIMENSIONS, PROMPT_VERSION, QUESTION_UNAVAILABLE_CODES,
                                            JevQuestion, UnavailableQuestion, build_question)
from backend.library import decision_cache as cache
from backend.library.decision_cache import (CACHE_MISS_REASONS, CACHE_ORIGIN,
                                            CACHEABLE_OUTCOME_STATES, DECISION_KINDS, CacheLookup,
                                            CacheStats, CachedCandidateDecision,
                                            CachedDimensionDecision, CachedJudgment,
                                            CandidateDecisionIdentity, DecisionCacheKey,
                                            JudgmentIdentity, ModelVersionPin, PruneReport,
                                            StoreResult)
from backend.library.errors import (CacheKeyInvalid, CachePayloadInvalid, DatabaseLocked,
                                    DecisionNotCacheable, InvalidCacheBound, LibraryError,
                                    WriteFailed)
from backend.library.repository import LibraryRepository
from backend.library.schema import open_database
from backend.palette import ranking
from backend.palette.model import PALETTE_HASH_VERSION, palette_hash


ROOT = Path(__file__).resolve().parents[1]
CACHE_FIXTURES = ROOT / "tests" / "fixtures" / "cache"
JEV_FIXTURES = ROOT / "tests" / "fixtures" / "jev"
CONTRACT_FIXTURES = ROOT / "tests" / "fixtures" / "contracts"
DOCUMENT = ROOT / "_docs" / "decision-cache.md"
MODULE_FILE = ROOT / "backend" / "library" / "decision_cache.py"

CASES = json.loads((CACHE_FIXTURES / "cache-cases.json").read_text(encoding="utf-8"))
CASE_BY_NAME = {case["name"]: case for case in CASES}
KEY_MATRIX = json.loads((CACHE_FIXTURES / "key-matrix.json").read_text(encoding="utf-8"))
QUESTION_CASES = json.loads((JEV_FIXTURES / "question-cases.json").read_text(encoding="utf-8"))
QUESTION_CASE_BY_NAME = {case["name"]: case for case in QUESTION_CASES}
ADAPTER_CASES = json.loads((JEV_FIXTURES / "adapter-cases.json").read_text(encoding="utf-8"))
ADAPTER_CASE_BY_NAME = {case["name"]: case for case in ADAPTER_CASES}
HYBRID_CASES = json.loads((ROOT / "tests" / "fixtures" / "hybrid" / "hybrid-cases.json")
                          .read_text(encoding="utf-8"))
HYBRID_CASE_BY_NAME = {case["name"]: case for case in HYBRID_CASES}

RATE, FRAMES = 48000, 48000
ANALYSIS = "cache-fixture-1"
TICK = chr(96)
PAIR_QUESTIONS = ("full_frequency", "full_texture")

# The #13 privacy tokens; they appear only in the assertion that they appear nowhere.
PRIVACY_TOKENS = ("zzz-distinctive-9f8a", "silent-monolith-9f8a.wav", "zeta-distinctive-9f8a-id",
                  "13579")

# The key record's exact fields, in the documented order (acceptance criterion 3),
# transcribed by hand rather than read back from the implementation.
KEY_FIELDS = ("cache_key_version", "decision_kind", "source", "interface_name", "adapter_version",
              "prompt_version", "model_version", "palette_hash", "palette_hash_version",
              "candidate_id", "candidate_content_fingerprint", "candidate_analysis_version",
              "dimension", "question_id", "kick_id", "kick_content_fingerprint",
              "kick_analysis_version", "questions_digest", "ranking_version", "weight_table_id",
              "baseline_ranking_version", "baseline_weight_table_id")

# The record field lists the issue body pins exactly, transcribed by hand.
CACHED_JUDGMENT_FIELDS = ("payload_version", "dimension", "question_id", "response_json", "origin",
                          "origin_source", "origin_interface_name", "adapter_version", "attempts",
                          "origin_elapsed_ms", "created_at")
CACHED_DIMENSION_FIELDS = ("dimension", "dsp_compatibility", "dsp_unavailable_reason", "judgment",
                           "unavailable_reason", "combined_compatibility")
CACHED_DECISION_FIELDS = ("payload_version", "candidate_id", "analysis_version", "ranking_version",
                          "weight_table_id", "mode", "jev_status", "compatibility", "confidence",
                          "uncertain", "dimensions", "warnings")

ENTRY_POINTS = ("judgment_key", "candidate_decision_key", "cache_key_digest", "response_document",
                "pinned_model_version", "record_model_version", "lookup_judgment", "store_judgment",
                "lookup_candidate_decision", "store_candidate_decision", "invalidate_entry",
                "invalidate_candidate", "prune_cache", "cache_stats")

EVIDENCE_CASE = "all_judged"
EVIDENCE_REQUEST = "candidate-001"
PIN_CASE = "judged_then_malformed"

# The base identities the key matrix is built over; every value is synthetic.
BASE_PALETTE_HASH = "1f2e3d4c5b6a7988a9bacbdcedfe0f101f2e3d4c5b6a7988a9bacbdcedfe0f10"
BASE_BASS_FINGERPRINT = "b0a1c2d3e4f5061728394a5b6c7d8e9f" "b0a1c2d3e4f5061728394a5b6c7d8e9f"
BASE_KICK_FINGERPRINT = "c1b2a3d4e5f60718293a4b5c6d7e8f90" "c1b2a3d4e5f60718293a4b5c6d7e8f90"
BASE_JUDGMENT_IDENTITY = JudgmentIdentity(
    source="double", interface_name="jev-contract-double", adapter_version=ADAPTER_VERSION,
    prompt_version=PROMPT_VERSION, model_version="synthetic-model-1",
    palette_hash=BASE_PALETTE_HASH, palette_hash_version=PALETTE_HASH_VERSION,
    candidate_id="bass-001", candidate_content_fingerprint=BASE_BASS_FINGERPRINT,
    candidate_analysis_version="dsp-fixture-1")
BASE_CANDIDATE_IDENTITY = CandidateDecisionIdentity(
    source="double", interface_name="jev-contract-double", adapter_version=ADAPTER_VERSION,
    prompt_version=PROMPT_VERSION, model_version="synthetic-model-1",
    palette_hash=BASE_PALETTE_HASH, palette_hash_version=PALETTE_HASH_VERSION,
    candidate_id="bass-001", candidate_content_fingerprint=BASE_BASS_FINGERPRINT,
    candidate_analysis_version="dsp-fixture-1", kick_id="kick-001",
    kick_content_fingerprint=BASE_KICK_FINGERPRINT, kick_analysis_version="dsp-fixture-1",
    ranking_version="hybrid-ranking-v1", weight_table_id="hybrid-weights-1",
    baseline_ranking_version="dsp-baseline-v1",
    baseline_weight_table_id="dsp-baseline-weights-1")


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
                                                          "C:/synthetic-cache/side.wav"),
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


def build(question_case_name):
    """The #13 question one named landed question case builds."""

    case = QUESTION_CASE_BY_NAME[question_case_name]
    return build_question(case["dimension"],
                          side_sample(case["kick"], "kick-001", "kick"),
                          side_sample(case["candidate"], "bass-001", "bass"),
                          song=song_context(case["song"]))


def questions_named(names):
    return tuple(build(name) for name in names)


def adapter_requests(case):
    requests = []
    for entry in case["batch"]:
        if "question_case" in entry:
            question = build(entry["question_case"])
        else:
            code = entry["unavailable"]
            named = next(item for item in QUESTION_CASES if item["expects"].get("code") == code)
            question = build(named["name"])
        requests.append(JevScoringRequest(request_id=entry.get("request_id"), question=question))
    return requests


def adapter_script(requests, case):
    scripted = case.get("script") or {}
    script = {}
    for request in requests:
        entries = scripted.get(request.request_id)
        if entries is None:
            continue
        assert type(request.question) is JevQuestion, request.request_id
        script.setdefault(request.question.question_id, []).extend(
            entries if type(entries) is list else [entries])
    return script


def run_adapter_case(case):
    """The requests, the contract double and the recorded #14 run of one adapter case."""

    requests = adapter_requests(case)
    double = JevContractDouble(script=adapter_script(requests, case))
    config = JevAdapterConfig(**case.get("config", {}))
    run = score_questions(requests, transport=double, config=config, sleep=lambda seconds: None,
                          monotonic=lambda: 0.0)
    return requests, double, run


def outcome_named(case_name, request_id):
    _, _, run = run_adapter_case(ADAPTER_CASE_BY_NAME[case_name])
    return next(item for item in run.outcomes if item.request_id == request_id)


def evidence_question():
    return build("full_frequency")


def evidence_outcome():
    return outcome_named(EVIDENCE_CASE, EVIDENCE_REQUEST)


def cached_judgment_for(question, judgment, *, attempts=1, elapsed_ms=0, source="double",
                        interface_name="jev-contract-double", adapter_version=ADAPTER_VERSION,
                        created_at="2024-01-01T00:00:00Z"):
    """One stored CachedJudgment for a judgment, built the way the caller would."""

    return CachedJudgment(payload_version=cache.CACHE_PAYLOAD_VERSION,
                          dimension=question.dimension, question_id=question.question_id,
                          response_json=canonical(cache.response_document(question, judgment)),
                          origin=CACHE_ORIGIN, origin_source=source,
                          origin_interface_name=interface_name, adapter_version=adapter_version,
                          attempts=attempts, origin_elapsed_ms=elapsed_ms, created_at=created_at)


def cached_judgment_of(question, outcome, **keywords):
    """One stored CachedJudgment for a real #14 outcome."""

    return cached_judgment_for(question, outcome.judgment, attempts=outcome.attempts,
                               elapsed_ms=outcome.elapsed_ms, **keywords)


DECISION_SPEC = {
    "mode": "hybrid", "jev_status": "jev_partial", "compatibility": 0.71, "confidence": 0.62,
    "uncertain": False, "warnings": ["low_coverage"],
    "dimensions": {
        "frequency": {"request": "candidate-001", "dsp_compatibility": 0.55,
                      "dsp_unavailable_reason": None, "combined_compatibility": 0.7},
        "transient": {"request": "candidate-002", "dsp_compatibility": 0.4,
                      "dsp_unavailable_reason": None, "combined_compatibility": 0.6},
        "tonal": {"request": "candidate-003", "dsp_compatibility": 0.5,
                  "dsp_unavailable_reason": None, "combined_compatibility": 0.5},
        "rhythmic": {"unavailable_reason": "candidate_tempo_unknown", "dsp_compatibility": None,
                     "dsp_unavailable_reason": "tempo_unknown", "combined_compatibility": None},
        "texture": {"unavailable_reason": "candidate_spectral_centroid_unknown",
                    "dsp_compatibility": 0.45, "dsp_unavailable_reason": None,
                    "combined_compatibility": 0.45},
        "arrangement": {"unavailable_reason": "song_context_absent", "dsp_compatibility": None,
                        "dsp_unavailable_reason": "role_evidence_missing",
                        "combined_compatibility": None},
    },
}


def decision_from_spec(questions, run, spec, *, candidate_id="bass-001",
                       analysis_version="dsp-fixture-1", ranking_version="hybrid-ranking-v1",
                       weight_table_id="hybrid-weights-1"):
    """One CachedCandidateDecision built from a declared spec and a #14 run."""

    by_request = {item.request_id: item for item in run.outcomes}
    by_dimension = {question.dimension: question for question in questions}
    entries = []
    for dimension in DIMENSIONS:
        item = spec["dimensions"][dimension]
        if "request" in item:
            outcome = by_request[item["request"]]
            question = by_dimension[dimension]
            judgment = outcome.judgment
            assert judgment is not None and judgment.dimension == dimension
            stored = cached_judgment_of(question, outcome, source=run.source,
                                        interface_name=run.interface_name,
                                        adapter_version=run.adapter_version)
            entries.append(CachedDimensionDecision(
                dimension=dimension, dsp_compatibility=item.get("dsp_compatibility"),
                dsp_unavailable_reason=item.get("dsp_unavailable_reason"), judgment=stored,
                unavailable_reason=None,
                combined_compatibility=item.get("combined_compatibility")))
        else:
            entries.append(CachedDimensionDecision(
                dimension=dimension, dsp_compatibility=item.get("dsp_compatibility"),
                dsp_unavailable_reason=item.get("dsp_unavailable_reason"), judgment=None,
                unavailable_reason=item["unavailable_reason"],
                combined_compatibility=item.get("combined_compatibility")))
    return CachedCandidateDecision(payload_version=cache.CACHE_PAYLOAD_VERSION,
                                   candidate_id=candidate_id, analysis_version=analysis_version,
                                   ranking_version=ranking_version,
                                   weight_table_id=weight_table_id, mode=spec["mode"],
                                   jev_status=spec["jev_status"], compatibility=spec["compatibility"],
                                   confidence=spec["confidence"], uncertain=spec["uncertain"],
                                   dimensions=tuple(entries), warnings=tuple(spec["warnings"]))


def plain_decision(*, candidate_id="bass-001", analysis_version="dsp-fixture-1", **overrides):
    """A shape-valid decision with no embedded judgment, for store-path tests."""

    values = {"mode": "hybrid", "jev_status": "jev_partial", "compatibility": 0.5,
              "confidence": 0.5, "uncertain": False, "warnings": []}
    values.update(overrides)
    dimensions = tuple(CachedDimensionDecision(
        dimension=dimension, dsp_compatibility=None, dsp_unavailable_reason="not_measured",
        judgment=None, unavailable_reason="song_context_absent", combined_compatibility=None)
        for dimension in DIMENSIONS)
    return CachedCandidateDecision(payload_version=cache.CACHE_PAYLOAD_VERSION,
                                   candidate_id=candidate_id, analysis_version=analysis_version,
                                   ranking_version="hybrid-ranking-v1",
                                   weight_table_id="hybrid-weights-1", mode=values["mode"],
                                   jev_status=values["jev_status"],
                                   compatibility=values["compatibility"],
                                   confidence=values["confidence"],
                                   uncertain=values["uncertain"], dimensions=dimensions,
                                   warnings=tuple(values["warnings"]))


class TickingClock:
    """A deterministic utc_now stand-in: one distinct second per call.

    utc_now() has one-second resolution, so rows written inside one second would
    otherwise be ordered by cache_key alone. The documented pruning order is
    (created_at ASC, cache_key ASC), so a store-per-second clock makes the
    hand-written surviving sets in the fixtures exact and reproducible.
    """

    def __init__(self):
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return "2024-01-01T%02d:%02d:%02dZ" % (self.calls // 3600, (self.calls // 60) % 60,
                                               self.calls % 60)


def boom(*arguments, **keywords):
    raise AssertionError("no cache path may call a ranking function")


@pytest.fixture
def library(tmp_path):
    connection = open_database(tmp_path / "library.sqlite3")
    try:
        yield connection
    finally:
        connection.close()


def stored_keys(connection):
    rows = connection.execute("SELECT cache_key FROM decision_cache").fetchall()
    return {row["cache_key"] for row in rows}


# ---------------------------------------------------------------------------
# the module surface: constants, records, entry points, imports, no statement
# ---------------------------------------------------------------------------


def test_the_module_publishes_the_documented_constants():
    assert cache.CACHE_KEY_VERSION == "decision-cache-v1"
    assert cache.CACHE_PAYLOAD_VERSION == "decision-cache-payload-v1"
    assert cache.DECISION_KINDS == ("judgment", "candidate_decision")
    assert cache.CACHE_ORIGIN == "cache"
    assert cache.CACHEABLE_OUTCOME_STATES == ("judged", "abstained")
    assert cache.CACHE_MISS_REASONS == ("absent", "version_unsupported", "cached_response_invalid",
                                        "cache_corrupt")
    assert cache.DEFAULT_CACHE_MAX_ENTRIES == 50000
    assert cache.MIN_CACHE_MAX_ENTRIES == 1
    assert cache.MAX_CACHE_MAX_ENTRIES == 1000000
    assert cache.DEFAULT_CACHE_MAX_BYTES == 67108864
    assert cache.MIN_CACHE_MAX_BYTES == 4096
    assert cache.MAX_CACHE_MAX_BYTES == 1073741824
    assert cache.MAX_PAYLOAD_BYTES == 65536
    assert set(cache.CACHEABLE_OUTCOME_STATES) <= set(OUTCOME_STATES)
    assert set(cache.DECISION_KINDS) == {"judgment", "candidate_decision"}
    assert set(cache.CACHE_MISS_REASONS) == {"absent", "version_unsupported",
                                             "cached_response_invalid", "cache_corrupt"}


def test_the_records_carry_exactly_their_documented_fields():
    assert [field.name for field in fields(DecisionCacheKey)] == list(KEY_FIELDS)
    assert [field.name for field in fields(CachedJudgment)] == list(CACHED_JUDGMENT_FIELDS)
    assert [field.name for field in fields(CachedDimensionDecision)] == list(CACHED_DIMENSION_FIELDS)
    assert [field.name for field in fields(CachedCandidateDecision)] == list(CACHED_DECISION_FIELDS)
    for record in (JudgmentIdentity, CandidateDecisionIdentity, DecisionCacheKey, CachedJudgment,
                   CachedDimensionDecision, CachedCandidateDecision, ModelVersionPin, CacheLookup,
                   StoreResult, PruneReport, CacheStats):
        assert callable(record.to_dict), record.__name__
    for record in (DecisionCacheKey, CachedJudgment, CachedDimensionDecision,
                   CachedCandidateDecision, ModelVersionPin):
        assert callable(record.from_dict) and callable(record.to_json) \
            and callable(record.from_json), record.__name__
    for name in ENTRY_POINTS:
        assert callable(getattr(cache, name)), name


def test_the_module_imports_only_the_documented_modules():
    tree = ast.parse(MODULE_FILE.read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module)
    allowed = {"__future__", "dataclasses", "json", "sqlite3", "typing", "backend.analysis.batch",
               "backend.contracts", "backend.intelligence.decisions", "backend.intelligence.jev",
               "backend.intelligence.questions", "backend.library.errors",
               "backend.library.repository", "backend.library.schema", "backend.palette.ranking"}
    assert imported <= allowed, sorted(imported - allowed)
    assert "backend.intelligence.jev_double" not in imported


def test_the_module_carries_no_statement_of_its_own():
    source = MODULE_FILE.read_text(encoding="utf-8")
    for token in ("SELECT ", "INSERT INTO", "DELETE FROM", "UPDATE ", "CREATE TABLE",
                  "CREATE INDEX", "PRAGMA "):
        assert token not in source, token


def test_no_cache_path_calls_a_ranking_function(library, monkeypatch):
    monkeypatch.setattr(ranking, "rank_hybrid", boom)
    monkeypatch.setattr(ranking, "rank_candidates", boom)
    question = evidence_question()
    key = cache.judgment_key(BASE_JUDGMENT_IDENTITY, question)
    assert cache.store_judgment(library, key, outcome=evidence_outcome()).stored is True
    assert cache.lookup_judgment(library, key, question=question).status == "hit"
    assert cache.cache_stats(library).entries == 1


# ---------------------------------------------------------------------------
# the key: one canonical record, one component matrix, #24's hash verbatim
# ---------------------------------------------------------------------------


def base_keys():
    judgment = cache.judgment_key(BASE_JUDGMENT_IDENTITY, build("full_frequency"))
    candidate = cache.candidate_decision_key(BASE_CANDIDATE_IDENTITY,
                                             questions_named(PAIR_QUESTIONS))
    return {"judgment": judgment, "candidate_decision": candidate}


def changed_identity(kind, field, value):
    base = BASE_JUDGMENT_IDENTITY if kind == "judgment" else BASE_CANDIDATE_IDENTITY
    return type(base)(**{**{item.name: getattr(base, item.name) for item in fields(base)},
                         field: value})


def matrix_key(kind, entry):
    """The key and the questions one matrix entry builds for one kind.

    Construction raises CacheKeyInvalid for the one entry whose change makes the
    key contradict the table's CHECKs (the decision_kind flip).
    """

    source = entry["source"]
    if source == "kind":
        assert entry["changed"] in DECISION_KINDS
        values = base_keys()[kind].to_dict()
        values["decision_kind"] = ("candidate_decision" if kind == "judgment" else "judgment")
        return DecisionCacheKey(**values), tuple()
    if source == "key":
        values = base_keys()[kind].to_dict()
        values[entry["field"]] = entry["changed"]
        return DecisionCacheKey(**values), tuple()
    if source == "question":
        question = JevQuestion(question_id=base_keys()["judgment"].question_id,
                               dimension=entry["changed"], prompt_version=PROMPT_VERSION,
                               instruction="", evidence=(), withheld=())
        return cache.judgment_key(BASE_JUDGMENT_IDENTITY, question), (question,)
    if source == "question_id":
        question = JevQuestion(question_id=entry["changed"], dimension="frequency",
                               prompt_version=PROMPT_VERSION, instruction="", evidence=(),
                               withheld=())
        return cache.judgment_key(BASE_JUDGMENT_IDENTITY, question), (question,)
    if source == "questions":
        questions = questions_named(entry["changed"])
        return cache.candidate_decision_key(BASE_CANDIDATE_IDENTITY, questions), questions
    identity = changed_identity(kind, entry["field"], entry["changed"])
    if kind == "judgment":
        question = build("full_frequency")
        return cache.judgment_key(identity, question), (question,)
    return cache.candidate_decision_key(identity, questions_named(PAIR_QUESTIONS)), \
        questions_named(PAIR_QUESTIONS)


def applies_to(kind, entry, field_sets):
    """True when one matrix entry changes the key of that kind at all.

    A question-derived field exists only in a judgment key, a question set only
    in a candidate-decision key, and an identity field only in the identities
    that carry it.
    """

    if entry["source"] == "key":
        return True
    if entry["source"] in ("question", "question_id"):
        return kind == "judgment"
    if entry["source"] == "questions":
        return kind == "candidate_decision"
    return entry["field"] in field_sets[kind]


def test_the_key_matrix_pins_every_field(library):
    bases = base_keys()
    question = build("full_frequency")
    assert cache.store_judgment(library, bases["judgment"],
                                outcome=evidence_outcome()).stored is True
    assert cache.store_candidate_decision(
        library, bases["candidate_decision"], decision=plain_decision()).stored is True
    assert len(KEY_MATRIX) == len(KEY_FIELDS)
    assert {entry["field"] for entry in KEY_MATRIX} == set(KEY_FIELDS)
    field_sets = {"judgment": {item.name for item in fields(JudgmentIdentity)},
                  "candidate_decision": {item.name for item in fields(CandidateDecisionIdentity)}}
    for entry in KEY_MATRIX:
        for kind in ("judgment", "candidate_decision"):
            expected = entry["expects"][kind]
            differs = entry["digest_changes"][kind]
            if expected == "cache_key_invalid":
                with pytest.raises(CacheKeyInvalid) as caught:
                    matrix_key(kind, entry)
                assert caught.value.code == "cache_key_invalid", entry
                assert differs is False, entry
                continue
            applies = applies_to(kind, entry, field_sets)
            if applies:
                key, questions = matrix_key(kind, entry)
                assert (cache.cache_key_digest(key)
                        != cache.cache_key_digest(bases[kind])) is differs, (entry, kind)
            else:
                key, questions = bases[kind], questions_named(PAIR_QUESTIONS)
                assert cache.cache_key_digest(key) == cache.cache_key_digest(bases[kind]), entry
                assert differs is False, entry
            if kind == "judgment":
                # The probe question matches the key's own three identity fields, so
                # the lookup reaches the row (or its absence) rather than refusing a
                # caller bug; on a hit #13 re-validates the stored document.
                probe = JevQuestion(question_id=key.question_id, dimension=key.dimension,
                                    prompt_version=key.prompt_version, instruction="",
                                    evidence=(), withheld=())
                found = cache.lookup_judgment(library, key, question=probe)
            else:
                found = cache.lookup_candidate_decision(library, key,
                                                        questions=questions or
                                                        questions_named(PAIR_QUESTIONS))
            assert found.status == expected, (entry["name"], kind)
            if expected == "miss":
                assert found.reason == "absent", (entry["name"], kind)
                assert found.stale_found is False
    # The base keys are unchanged by the matrix and both rows are still there.
    assert stored_keys(library) == {cache.cache_key_digest(key) for key in bases.values()}


def test_the_key_is_canonical_and_path_free(library):
    key = base_keys()["judgment"]
    digest_value = cache.cache_key_digest(key)
    assert len(digest_value) == 64 and digest_value == digest_value.lower()
    assert set(digest_value) <= set("0123456789abcdef")
    assert digest_value == digest(key.to_dict())
    shuffled = DecisionCacheKey(**{name: key.to_dict()[name] for name in reversed(KEY_FIELDS)})
    assert cache.cache_key_digest(shuffled) == digest_value
    assert DecisionCacheKey.from_dict(key.to_dict()) == key
    assert DecisionCacheKey.from_json(key.to_json()) == key
    assert key.to_json() == canonical(key.to_dict())
    assert DecisionCacheKey.from_dict(key.to_dict()).to_json() == key.to_json()
    text = key.to_json() + digest_value
    for token in (":\\", "/", ".wav", "tera-fixtures", "pack"):
        assert token not in text, token
    with pytest.raises(CacheKeyInvalid):
        DecisionCacheKey.from_dict({**key.to_dict(), "extra": "x"})
    with pytest.raises(CacheKeyInvalid):
        DecisionCacheKey.from_json("{not json}")


def test_a_blank_field_or_a_wrong_kind_is_refused():
    values = base_keys()["judgment"].to_dict()
    for name in KEY_FIELDS[:12]:
        blanked = dict(values)
        blanked[name] = "   "
        with pytest.raises(CacheKeyInvalid) as caught:
            DecisionCacheKey(**blanked)
        assert caught.value.code == "cache_key_invalid", name
    wrong_kind = dict(values)
    wrong_kind["decision_kind"] = "verdict"
    with pytest.raises(CacheKeyInvalid):
        DecisionCacheKey(**wrong_kind)
    wrong_source = dict(values)
    wrong_source["source"] = "synthetic-source"
    with pytest.raises(CacheKeyInvalid):
        DecisionCacheKey(**wrong_source)
    judgment_with_kick = dict(values)
    judgment_with_kick["kick_id"] = "kick-001"
    with pytest.raises(CacheKeyInvalid):
        DecisionCacheKey(**judgment_with_kick)
    candidate = base_keys()["candidate_decision"].to_dict()
    candidate_with_dimension = dict(candidate)
    candidate_with_dimension["dimension"] = "frequency"
    with pytest.raises(CacheKeyInvalid):
        DecisionCacheKey(**candidate_with_dimension)
    candidate_without_kick = dict(candidate)
    candidate_without_kick["kick_id"] = None
    with pytest.raises(CacheKeyInvalid):
        DecisionCacheKey(**candidate_without_kick)


def test_judgment_key_refuses_a_question_that_was_never_asked():
    unavailable = build("frequency_kick_band_sub_unknown")
    assert type(unavailable) is UnavailableQuestion
    with pytest.raises(CacheKeyInvalid):
        cache.judgment_key(BASE_JUDGMENT_IDENTITY, unavailable)
    with pytest.raises(CacheKeyInvalid):
        cache.judgment_key(BASE_JUDGMENT_IDENTITY, "not a question")
    with pytest.raises(CacheKeyInvalid):
        cache.candidate_decision_key(BASE_JUDGMENT_IDENTITY, (build("full_frequency"),))


def test_the_palette_hash_is_24s_and_history_never_keys_an_entry(library, monkeypatch):
    from dataclasses import replace as replace_record

    monkeypatch.setattr(cache, "utc_now", TickingClock())
    repository = LibraryRepository(library)
    version = repository.register_analysis_version(batch.analysis_descriptor())
    kick, bass = RecommendationBatch.from_json(
        (CONTRACT_FIXTURES / "hybrid.json").read_text(encoding="utf-8")).samples
    for sample_id, role, source in (("kick-001", "kick", kick), ("kick-002", "kick", kick),
                                    ("bass-001", "bass", bass), ("bass-002", "bass", bass)):
        sample = replace_record(source, sample_id=sample_id, role=role,
                                analysis_version=version,
                                audio=replace_record(source.audio,
                                                     local_path="C:/tera-fixtures/%s.wav"
                                                     % sample_id))
        repository.import_sample(sample, content_sha256=digest(sample_id))
    project = repository.create_project("Track A")
    mutation = repository.set_palette_item(project.palette_id, "kick", "kick-001",
                                           expected_revision=0)
    mutation = repository.set_palette_item(project.palette_id, "bass", "bass-001",
                                           expected_revision=mutation.revision)
    record = repository.load_palette(project.palette_id)
    content_hash = palette_hash(record)
    assert palette_hash(repository.load_palette(project.palette_id)) == content_hash

    question = build("full_frequency")
    outcome = evidence_outcome()

    def identities(palette_hash_value, candidate_id="bass-001"):
        judgment = replace_record(BASE_JUDGMENT_IDENTITY, palette_hash=palette_hash_value,
                                  palette_hash_version=PALETTE_HASH_VERSION,
                                  candidate_id=candidate_id,
                                  candidate_content_fingerprint=digest(candidate_id),
                                  candidate_analysis_version=version)
        candidate = replace_record(BASE_CANDIDATE_IDENTITY, palette_hash=palette_hash_value,
                                   palette_hash_version=PALETTE_HASH_VERSION,
                                   candidate_id=candidate_id,
                                   candidate_content_fingerprint=digest(candidate_id),
                                   candidate_analysis_version=version,
                                   kick_content_fingerprint=digest("kick-001"),
                                   kick_analysis_version=version)
        return judgment, candidate

    judgment_identity, candidate_identity = identities(content_hash)

    def keys(palette_hash_value, candidate_id="bass-001"):
        judgment, candidate = identities(palette_hash_value, candidate_id)
        return (cache.judgment_key(judgment, question),
                cache.candidate_decision_key(candidate, (question,)))

    judgment_key, candidate_key = keys(content_hash)
    assert cache.store_judgment(library, judgment_key, outcome=outcome).stored is True
    assert cache.store_candidate_decision(
        library, candidate_key,
        decision=plain_decision(analysis_version=version)).stored is True
    assert cache.lookup_judgment(library, judgment_key, question=question).status == "hit"
    assert cache.lookup_candidate_decision(library, candidate_key,
                                           questions=(question,)).status == "hit"
    before = cache.cache_stats(library)

    # A revision advance with unchanged content keeps the hash: remove then re-add
    # the same bass, which bumps the revision twice and leaves one active item.
    removed = repository.remove_palette_item(project.palette_id, "bass",
                                             expected_revision=mutation.revision)
    added = repository.set_palette_item(project.palette_id, "bass", "bass-001",
                                        expected_revision=removed.revision)
    bumped = repository.load_palette(project.palette_id)
    assert added.changed is True and bumped.revision > record.revision
    assert palette_hash(bumped) == content_hash
    assert cache.lookup_judgment(library, judgment_key, question=question).status == "hit"
    assert cache.lookup_candidate_decision(library, candidate_key,
                                           questions=(question,)).status == "hit"

    # A no-op mutation writes nothing and leaves the revision alone.
    noop = repository.set_palette_item(project.palette_id, "bass", "bass-001",
                                       expected_revision=bumped.revision)
    assert noop.changed is False and noop.revision == bumped.revision
    assert palette_hash(repository.load_palette(project.palette_id)) == content_hash

    # Changing the bass slot changes the hash and every entry for that palette stops
    # being addressable, but nothing is deleted by the miss.
    swapped = repository.set_palette_item(project.palette_id, "bass", "bass-002",
                                          expected_revision=noop.revision)
    other_hash = palette_hash(repository.load_palette(project.palette_id))
    assert other_hash != content_hash
    changed_judgment, changed_candidate = keys(other_hash)
    assert_miss(cache.lookup_judgment(library, changed_judgment, question=question), "absent", False)
    assert_miss(cache.lookup_candidate_decision(library, changed_candidate,
                                                questions=(question,)), "absent", False)
    assert cache.cache_stats(library) == before

    # Changing the kick's content changes the hash too, and the miss still
    # deletes nothing.
    switched = repository.set_palette_item(project.palette_id, "kick", "kick-002",
                                          expected_revision=swapped.revision)
    assert switched.changed is True
    third_hash = palette_hash(repository.load_palette(project.palette_id))
    assert third_hash not in (content_hash, other_hash)
    switched_judgment, switched_candidate = keys(third_hash)
    assert_miss(cache.lookup_judgment(library, switched_judgment, question=question),
                "absent", False)
    assert_miss(cache.lookup_candidate_decision(library, switched_candidate,
                                                questions=(question,)), "absent", False)
    # The entries written under the earlier hash are still addressable under that
    # hash, and no palette edit ever deleted one.
    assert cache.lookup_judgment(library, judgment_key, question=question).status == "hit"
    assert cache.lookup_candidate_decision(library, candidate_key,
                                           questions=(question,)).status == "hit"
    assert cache.cache_stats(library) == before


# -- decision helper used above and below -----------------------------------


def assert_miss(found, reason, stale_found):
    assert found.status == "miss"
    assert found.reason == reason
    assert found.stale_found is stale_found
    assert found.judgment is None and found.decision is None
    assert found.entry_created_at is None

# ---------------------------------------------------------------------------
# a candidate decision round-trips a real #15 result
# ---------------------------------------------------------------------------

SHORT_BANDS = {"sub": "band_sub", "bass": "band_bass", "low_mid": "band_low_mid",
               "mid": "band_mid", "high_mid": "band_high_mid", "high": "band_high"}
DEFAULT_PROBABILITIES = {
    "very-poor": {"very-poor": 0.60, "poor": 0.20, "neutral": 0.10, "good": 0.05,
                  "excellent": 0.05},
    "poor": {"very-poor": 0.20, "poor": 0.60, "neutral": 0.10, "good": 0.05, "excellent": 0.05},
    "neutral": {"very-poor": 0.05, "poor": 0.10, "neutral": 0.70, "good": 0.10, "excellent": 0.05},
    "good": {"very-poor": 0.05, "poor": 0.05, "neutral": 0.10, "good": 0.60, "excellent": 0.20},
    "excellent": {"very-poor": 0.05, "poor": 0.05, "neutral": 0.05, "good": 0.25,
                  "excellent": 0.60},
}


def hybrid_key_of(spec):
    if spec.get("tonic") is None:
        return MusicalKey(tonic=None, mode=None, confidence=None,
                          unavailable_reason="insufficient_key_context")
    return MusicalKey(tonic=spec["tonic"], mode=spec.get("mode", "major"),
                      confidence=spec.get("key_confidence", 0.90))


def hybrid_sample(spec, default_role="bass", default_id="sample-001"):
    values = {name: measure(name) for name in MEASURES}
    values["peak"] = measure("peak", 0.8)
    values["rms"] = measure("rms", 0.2)
    for short, name in SHORT_BANDS.items():
        values[name] = measure(name, spec.get("bands", {}).get(short, 0.0))
    for short, name in (("attack", "attack"), ("decay", "decay"),
                        ("strength", "transient_strength"), ("position", "transient_position")):
        if short in spec:
            values[name] = measure(name, spec[short])
    if "fundamental" in spec:
        values["fundamental"] = measure("fundamental", spec["fundamental"],
                                        spec.get("fundamental_confidence"))
    for name in ("spectral_centroid", "spectral_rolloff"):
        if name in spec:
            values[name] = measure(name, spec[name])
    sample_id = spec.get("id", default_id)
    return Sample(sample_id=sample_id, role=spec.get("role", default_role),
                  audio=AudioMetadata(local_path="C:/synthetic-hybrid/" + sample_id + ".wav",
                                      sample_rate_hz=RATE, channels=1, frame_count=FRAMES,
                                      duration_ms=FRAMES * 1000 / RATE),
                  features=AudioFeatures(measurements=tuple(values.values()),
                                         key=hybrid_key_of(spec)),
                  analysis_version=spec.get("analysis_version", "hybrid-fixture-1"))


def hybrid_judgment(entry, dimension):
    if entry.get("abstain"):
        return JevJudgment(dimension=dimension, label=None, confidence=None, probabilities=(),
                           model_version=entry.get("model_version", "hybrid-abstain-1"),
                           prompt_version=entry.get("prompt_version", PROMPT_VERSION),
                           unavailable_reason=MODEL_ABSTAINED)
    label = entry["label"]
    probabilities = entry.get("probabilities", DEFAULT_PROBABILITIES[label])
    return JevJudgment(dimension=dimension, label=label,
                       confidence=entry.get("confidence", 0.90),
                       probabilities=tuple(LabelProbability(label=name, probability=value)
                                           for name, value in probabilities.items()),
                       model_version=entry.get("model_version", "hybrid-fixture-model-1"),
                       prompt_version=entry.get("prompt_version", PROMPT_VERSION))


def hybrid_evidence(case):
    evidence = {}
    for candidate_id, entries in case.get("evidence", {}).items():
        built = []
        for entry in entries:
            dimension = entry["dimension"]
            if "label" in entry or entry.get("abstain"):
                built.append(ranking.HybridEvidence(dimension,
                                                    judgment=hybrid_judgment(entry, dimension)))
            else:
                built.append(ranking.HybridEvidence(dimension,
                                                    unavailable_reason=entry["unavailable_reason"]))
        evidence[candidate_id] = tuple(built)
    return evidence


def hybrid_source(case_name, candidate_id):
    """A real #12 baseline and #15 hybrid result from the landed hybrid fixture."""

    case = HYBRID_CASE_BY_NAME[case_name]
    kick = hybrid_sample(case["kick"], "kick", "kick-001")
    candidates = [hybrid_sample(spec) for spec in case["candidates"]]
    baseline = ranking.rank_candidates(kick, candidates, policy=ranking.RankingPolicy())
    result = ranking.rank_hybrid(baseline, hybrid_evidence(case), policy=ranking.HybridPolicy())
    record = next(item for item in list(result.ranked) + list(result.unscored)
                  if item.candidate_id == candidate_id)
    candidate = next(item for item in candidates if item.sample_id == candidate_id)
    return baseline, result, kick, candidate, record


def test_a_candidate_decision_round_trips_a_real_hybrid_result(library, monkeypatch):
    monkeypatch.setattr(cache, "utc_now", TickingClock())
    baseline, result, kick, candidate, record = hybrid_source("all_judged_certain",
                                                              "bass-complement")
    judgments = {item.dimension: item for item in record.jev_judgments}
    sources = {item.dimension: item for item in record.hybrid_dimensions}
    questions = []
    entries = []
    embedded = set()
    for dimension in DIMENSIONS:
        question = build_question(dimension, kick, candidate)
        questions.append(question)
        source = sources[dimension]
        if dimension in judgments and type(question) is JevQuestion:
            embedded.add(dimension)
            entries.append(CachedDimensionDecision(
                dimension=dimension, dsp_compatibility=source.dsp_compatibility,
                dsp_unavailable_reason=source.dsp_unavailable_reason,
                judgment=cached_judgment_for(question, judgments[dimension]),
                unavailable_reason=None,
                combined_compatibility=source.compatibility))
        else:
            # The payload's reason is the Jev-side evidence reason: the source's
            # jev_unavailable_reason, or the #13 code of a question that could not
            # be asked. #15's combined no_evidence code is not an evidence code, and
            # a judgment the fixture supplies for a dimension no #13 question can ask
            # is never storable, because a read would have to re-validate it.
            reason = getattr(question, "code", None) or source.jev_unavailable_reason
            assert reason in (set(QUESTION_UNAVAILABLE_CODES) | set(TRANSPORT_ERROR_CODES)
                              | set(NOT_ATTEMPTED_CODES) | set(UNAVAILABLE_CODES)), dimension
            entries.append(CachedDimensionDecision(
                dimension=dimension, dsp_compatibility=source.dsp_compatibility,
                dsp_unavailable_reason=source.dsp_unavailable_reason, judgment=None,
                unavailable_reason=reason, combined_compatibility=source.compatibility))
    decision = CachedCandidateDecision(payload_version=cache.CACHE_PAYLOAD_VERSION,
                                       candidate_id=record.candidate_id,
                                       analysis_version=candidate.analysis_version,
                                       ranking_version=result.ranking_version,
                                       weight_table_id=result.weight_table_id, mode=result.mode,
                                       jev_status=result.jev_status,
                                       compatibility=record.compatibility,
                                       confidence=record.confidence, uncertain=record.uncertain,
                                       dimensions=tuple(entries), warnings=tuple(record.warnings))
    identity = CandidateDecisionIdentity(
        source="double", interface_name="jev-contract-double", adapter_version=ADAPTER_VERSION,
        prompt_version=PROMPT_VERSION, model_version="synthetic-model-1",
        palette_hash=BASE_PALETTE_HASH, palette_hash_version=PALETTE_HASH_VERSION,
        candidate_id=record.candidate_id,
        candidate_content_fingerprint=digest(record.candidate_id),
        candidate_analysis_version=candidate.analysis_version, kick_id=kick.sample_id,
        kick_content_fingerprint=digest(kick.sample_id),
        kick_analysis_version=kick.analysis_version, ranking_version=result.ranking_version,
        weight_table_id=result.weight_table_id,
        baseline_ranking_version=baseline.ranking_version,
        baseline_weight_table_id=baseline.weight_table_id)
    key = cache.candidate_decision_key(identity, tuple(questions))
    assert cache.store_candidate_decision(library, key, decision=decision).stored is True
    hit = cache.lookup_candidate_decision(library, key, questions=tuple(questions))
    assert hit.status == "hit" and hit.stale_found is False and hit.judgment is None
    stored = hit.decision
    assert stored.compatibility == record.compatibility
    assert stored.confidence == record.confidence
    assert stored.uncertain == record.uncertain
    assert stored.warnings == tuple(record.warnings)
    assert stored.mode == result.mode and stored.jev_status == result.jev_status
    assert [item.dimension for item in stored.dimensions] == list(DIMENSIONS)
    by_question = {question.dimension: question for question in questions}
    for entry in stored.dimensions:
        source = sources[entry.dimension]
        assert entry.dsp_compatibility == source.dsp_compatibility, entry.dimension
        assert entry.dsp_unavailable_reason == source.dsp_unavailable_reason, entry.dimension
        assert entry.combined_compatibility == source.compatibility, entry.dimension
        if entry.judgment is None:
            assert entry.unavailable_reason == (getattr(by_question[entry.dimension], "code", None)
                                                or source.jev_unavailable_reason)
            continue
        rederived = validate_response(by_question[entry.dimension],
                                      entry.judgment.response_json)
        assert rederived == judgments[entry.dimension], entry.dimension
        assert entry.judgment.origin == CACHE_ORIGIN
        assert entry.judgment.dimension == entry.dimension
        assert entry.judgment.attempts == 1
    # Four dimensions' judgments re-derive exactly; the fixture also declares a
    # texture judgment, but no #13 question can ask texture for this pair, so the
    # dimension carries the question's own code instead (see _docs/decision-cache.md).
    assert embedded == {"frequency", "transient", "tonal", "arrangement"}
    text = decision.to_json()
    for absent in ('"rank"', '"alternatives"', '"reasons"', '"reason"', '"live"'):
        assert absent not in text, absent
    assert CachedCandidateDecision.from_json(text) == decision
    assert text == canonical(decision.to_dict())
    assert json.loads(json.dumps(decision.to_dict(), allow_nan=False)) == json.loads(text)
    assert cached_judgment_for(questions[0], judgments["frequency"]).origin_source == "double"


def test_a_payload_that_disagrees_with_its_own_judgment_is_refused(library, monkeypatch):
    monkeypatch.setattr(cache, "utc_now", TickingClock())
    questions = questions_named(PAIR_QUESTIONS)
    key = cache.candidate_decision_key(BASE_CANDIDATE_IDENTITY, questions)
    good = plain_decision()
    from dataclasses import replace
    mismatched = replace(good, dimensions=tuple(
        replace(item, judgment=cached_judgment_for(build("full_frequency"),
                                                   evidence_outcome().judgment),
                unavailable_reason=None)
        if item.dimension == "frequency" else item for item in good.dimensions))
    assert cache.store_candidate_decision(library, key, decision=mismatched).stored is True
    # The stored judgment is re-validated against the question supplied for its
    # dimension: a question that no longer matches is a miss, not a value.
    wrong = questions_named(("full_transient", "full_texture"))
    with pytest.raises(CacheKeyInvalid):
        cache.lookup_candidate_decision(library, key, questions=wrong)
    hit = cache.lookup_candidate_decision(library, key, questions=questions)
    assert hit.status == "hit"
    row = payload_row(library, key)
    payload = json.loads(row["payload_json"])
    document = json.loads(payload["dimensions"][0]["judgment"]["response_json"])
    document["label"] = "very-poor"
    payload["dimensions"][0]["judgment"]["response_json"] = canonical(document)
    library.execute("UPDATE decision_cache SET payload_json = ? WHERE cache_key = ?",
                    (canonical(payload), cache.cache_key_digest(key)))
    stale = cache.lookup_candidate_decision(library, key, questions=questions)
    assert_miss(stale, "cached_response_invalid", True)
    assert payload_row(library, key) is not None


# ---------------------------------------------------------------------------
# a cache hit is never a live outcome
# ---------------------------------------------------------------------------


def test_a_cache_hit_is_never_a_live_outcome(library, monkeypatch):
    from dataclasses import replace

    monkeypatch.setattr(cache, "utc_now", TickingClock())
    question = build("full_frequency")
    outcome = evidence_outcome()
    key = cache.judgment_key(BASE_JUDGMENT_IDENTITY, question)
    assert cache.store_judgment(library, key, outcome=outcome).stored is True
    hit = cache.lookup_judgment(library, key, question=question)
    payload = json.loads(payload_row(library, key)["payload_json"])
    assert payload["origin"] == CACHE_ORIGIN == "cache"
    assert payload["origin_source"] == outcome_state_source()
    assert payload["origin_interface_name"] == "jev-contract-double"
    assert payload["adapter_version"] == ADAPTER_VERSION
    assert payload["attempts"] == outcome.attempts
    assert payload["origin_elapsed_ms"] == outcome.elapsed_ms
    assert payload["created_at"] == hit.entry_created_at
    pin = cache.record_model_version(library, run_adapter_case(ADAPTER_CASE_BY_NAME[PIN_CASE])[2])
    for text in (json.dumps(hit.to_dict()), json.dumps(payload), json.dumps(pin.to_dict()),
                 hit.judgment.to_json()):
        assert "live" not in text
    for record in (CachedJudgment, CachedDimensionDecision, CachedCandidateDecision, CacheLookup):
        names = {item.name for item in fields(record)}
        assert "run" not in names and "outcomes" not in names and "live" not in names
        assert "source" not in names, record.__name__
    # An interface entry and a double entry both report origin cache, and only
    # their copied origin_source values differ.
    interface_identity = JudgmentIdentity(**{**BASE_JUDGMENT_IDENTITY.to_dict(),
                                            "source": "interface",
                                            "interface_name": "synthetic-interface-2",
                                            "palette_hash": "8" * 64})
    interface_key = cache.judgment_key(interface_identity, question)
    live_outcome = score_questions(
        [JevScoringRequest("candidate-001", question)],
        transport=JevContractDouble(model_version=BASE_JUDGMENT_IDENTITY.model_version),
        sleep=lambda seconds: None, monotonic=lambda: 0.0).outcomes[0]
    assert cache.store_judgment(library, interface_key, outcome=live_outcome).stored is True
    interface_payload = json.loads(payload_row(library, interface_key)["payload_json"])
    assert interface_payload["origin"] == payload["origin"] == CACHE_ORIGIN
    assert interface_payload["origin_source"] == "interface"
    assert interface_payload["origin_source"] != payload["origin_source"]
    assert interface_payload["attempts"] == live_outcome.attempts
    interface_hit = cache.lookup_judgment(library, interface_key, question=question)
    assert interface_hit.status == "hit" and interface_hit.stale_found is False
    assert interface_hit.judgment is not None and interface_hit.decision is None


def outcome_state_source():
    _, _, run = run_adapter_case(ADAPTER_CASE_BY_NAME[EVIDENCE_CASE])
    return run.source


# ---------------------------------------------------------------------------
# privacy, fixtures and the document
# ---------------------------------------------------------------------------


def test_the_privacy_tokens_never_reach_a_stored_payload_or_an_error(library, monkeypatch):
    from dataclasses import replace

    monkeypatch.setattr(cache, "utc_now", TickingClock())
    case = QUESTION_CASE_BY_NAME["full_frequency"]
    kick = side_sample(case["kick"], "zeta-distinctive-9f8a-id", "kick")
    candidate = replace(side_sample(case["candidate"], "13579", "bass"),
                        audio=replace(side_sample(case["candidate"], "13579", "bass").audio,
                                      local_path="C:\\Users\\synthetic-producer\\Secret "
                                                 "Library\\zzz-distinctive-9f8a\\"
                                                 "silent-monolith-9f8a.wav"))
    question = build_question("frequency", kick, candidate,
                              song=song_context(case["song"]))
    assert type(question) is JevQuestion
    judgment = evidence_outcome().judgment
    key = cache.judgment_key(BASE_JUDGMENT_IDENTITY, question)
    outcome = JevOutcome(request_id="candidate-001", question_id=question.question_id,
                         dimension="frequency", state="judged", code=None, attempts=1,
                         judgment=judgment, elapsed_ms=0)
    assert cache.store_judgment(library, key, outcome=outcome).stored is True
    message = ""
    try:
        cache.store_candidate_decision(library, key, decision=plain_decision())
    except LibraryError as error:
        assert error.code == "cache_key_invalid"
        message = str(error)
    rows = library.execute("SELECT * FROM decision_cache").fetchall()
    dump = json.dumps([dict(row) for row in rows], default=str)
    dump += cache.lookup_judgment(library, key, question=question).to_dict().__repr__()
    dump += json.dumps([case for case in CASES]) + json.dumps(KEY_MATRIX)
    for token in PRIVACY_TOKENS:
        assert token not in dump, token
        assert token not in message, token
    for token in (".wav", "local_path", "C:\\"):
        assert token not in dump, token


DOCUMENTED_CONSTANTS = ("CACHE_KEY_VERSION", "CACHE_PAYLOAD_VERSION", "DECISION_KINDS",
                        "CACHE_ORIGIN", "CACHEABLE_OUTCOME_STATES", "CACHE_MISS_REASONS",
                        "DEFAULT_CACHE_MAX_ENTRIES", "MIN_CACHE_MAX_ENTRIES",
                        "MAX_CACHE_MAX_ENTRIES", "DEFAULT_CACHE_MAX_BYTES", "MIN_CACHE_MAX_BYTES",
                        "MAX_CACHE_MAX_BYTES", "MAX_PAYLOAD_BYTES")

DOCUMENTED_RECORDS = (JudgmentIdentity, CandidateDecisionIdentity, DecisionCacheKey, CachedJudgment,
                      CachedDimensionDecision, CachedCandidateDecision, ModelVersionPin, CacheLookup,
                      StoreResult, PruneReport, CacheStats)

DOCUMENTED_ERROR_CODES = ("cache_key_invalid", "cache_payload_invalid", "decision_not_cacheable",
                          "invalid_cache_bound")

# Every column of both cache tables, transcribed by hand.
DOCUMENTED_COLUMNS = (KEY_FIELDS + ("payload_json", "created_at", "first_observed_at", "observed_at",
                                    "observation_count"))


def test_the_document_is_the_contract():
    text = DOCUMENT.read_text(encoding="utf-8")
    for name in DOCUMENTED_CONSTANTS:
        assert name in text, name
        assert str(getattr(cache, name)) in text, name
    for name in ENTRY_POINTS:
        assert name in text, name
    for record in DOCUMENTED_RECORDS:
        assert record.__name__ in text, record.__name__
        for field in fields(record):
            assert field.name in text, (record.__name__, field.name)
    for table in ("decision_cache", "decision_model_versions"):
        assert table in text, table
    for column in DOCUMENTED_COLUMNS:
        assert column in text, column
    for index in ("idx_decision_cache_created", "idx_decision_cache_candidate"):
        assert index in text, index
    for code in DOCUMENTED_ERROR_CODES:
        assert code in text, code
    for value in (CACHE_MISS_REASONS + (cache.CACHE_KEY_VERSION, cache.CACHE_PAYLOAD_VERSION,
                                        cache.PALETTE_HASH_VERSION if hasattr(cache,
                                                                             "PALETTE_HASH_VERSION")
                                        else PALETTE_HASH_VERSION)):
        assert value in text, value
    assert "a cache hit is reused evidence, never a live outcome" in text
    assert "no foreign key" in text and "samples" in text


def identity_of(case):
    if case["kind"] == "judgment":
        return JudgmentIdentity(**case["identity"])
    return CandidateDecisionIdentity(**case["identity"])


def case_key(case):
    identity = identity_of(case)
    questions = questions_named(case.get("questions", []))
    if case["kind"] == "judgment":
        return cache.judgment_key(identity, questions[0]), questions
    return cache.candidate_decision_key(identity, questions), questions


def test_the_hand_written_cache_fixture_cases_replay(library, monkeypatch):
    monkeypatch.setattr(cache, "utc_now", TickingClock())
    digests = {}
    for case in CASES:
        if "identity" in case:
            key, _ = case_key(case)
            digests[case["name"]] = cache.cache_key_digest(key)
    runs = {}
    for case in CASES:
        action = case["action"]
        if "outcome" in case and action in ("store", "pin"):
            runs[case["name"]] = run_adapter_case(ADAPTER_CASE_BY_NAME[case["outcome"]])[2]
        key = questions = None
        if "identity" in case:
            key, questions = case_key(case)
        bounds = {"max_entries": case.get("max_entries", cache.DEFAULT_CACHE_MAX_ENTRIES),
                  "max_bytes": case.get("max_bytes", cache.DEFAULT_CACHE_MAX_BYTES)}
        expects = case["expects"]
        if action == "store":
            if case["kind"] == "judgment":
                outcome = next(item for item in runs[case["name"]].outcomes
                               if item.request_id == case["request"])
                result = cache.store_judgment(library, key, outcome=outcome, **bounds)
            else:
                run = runs[case["name"]]
                decision = decision_from_spec(questions, run, case["decision"],
                                              candidate_id=case["identity"]["candidate_id"],
                                              analysis_version=case["identity"]
                                              ["candidate_analysis_version"],
                                              ranking_version=case["identity"]
                                              ["ranking_version"],
                                              weight_table_id=case["identity"]
                                              ["weight_table_id"])
                result = cache.store_candidate_decision(library, key, decision=decision, **bounds)
            assert result.stored is expects["stored"], case["name"]
            assert result.pruned == expects["pruned"], case["name"]
            assert result.created_at is not None, case["name"]
        elif action == "lookup":
            if case["kind"] == "judgment":
                found = cache.lookup_judgment(library, key, question=questions[0])
            else:
                found = cache.lookup_candidate_decision(library, key, questions=questions)
            assert found.status == expects["status"], case["name"]
            assert found.reason == expects["reason"], case["name"]
            assert found.stale_found is expects["stale_found"], case["name"]
            if found.status == "hit":
                assert found.entry_created_at is not None, case["name"]
                if case["kind"] == "judgment":
                    assert type(found.judgment) is JevJudgment
                else:
                    assert found.decision is not None
        elif action == "prune":
            report = cache.prune_cache(library, **bounds)
            assert report.deleted == expects["pruned"], case["name"]
            assert report.max_entries == bounds["max_entries"], case["name"]
            assert report.max_bytes == bounds["max_bytes"], case["name"]
        elif action == "invalidate_entry":
            assert cache.invalidate_entry(library, key) is expects["removed"], case["name"]
        elif action == "invalidate_candidate":
            removed = cache.invalidate_candidate(library, candidate_id=case["candidate_id"])
            assert removed == expects["removed"], case["name"]
        elif action == "pin":
            run = runs[case["name"]]
            pin = cache.record_model_version(library, run)
            assert (pin is not None) is case["writes_pin"], case["name"]
            query_source = case.get("query_source", "double")
            found = cache.pinned_model_version(
                library, interface_name="jev-contract-double", source=query_source,
                adapter_version=ADAPTER_VERSION, prompt_version=PROMPT_VERSION)
            if expects["pin"] is None:
                assert found is None, case["name"]
            else:
                assert found is not None, case["name"]
                for name, value in expects["pin"].items():
                    assert getattr(found, name) == value, (case["name"], name)
        else:
            raise AssertionError(action)
        if "surviving" in expects:
            assert stored_keys(library) == {digests[name] for name in expects["surviving"]}, \
                (case["name"], sorted(stored_keys(library)))
        stats = cache.cache_stats(library)
        assert {"entries": stats.entries, "judgments": stats.judgments,
                "candidate_decisions": stats.candidate_decisions} == expects["stats"], case["name"]
    # The fixture's own order and final counts are the recorded cache_stats after the run.
    final = cache.cache_stats(library)
    assert final.entries == 1 and final.judgments == 1 and final.candidate_decisions == 0
    assert final.oldest_created_at == final.newest_created_at
    # The recorded cache_stats after the fixture run: one judgment row of 870
    # canonical payload bytes, written by the fifth store of the replay.
    assert final.bytes == 870


# ---------------------------------------------------------------------------
# a change misses only what it touches and nothing is deleted by a miss
# ---------------------------------------------------------------------------


def identity_key_for(question, *, candidate_id="bass-001", fingerprint=None,
                     analysis_version="dsp-fixture-1", palette=BASE_PALETTE_HASH,
                     prompt_version=PROMPT_VERSION, model_version="synthetic-model-1"):
    identity = JudgmentIdentity(
        source="double", interface_name="jev-contract-double", adapter_version=ADAPTER_VERSION,
        prompt_version=prompt_version, model_version=model_version, palette_hash=palette,
        palette_hash_version=PALETTE_HASH_VERSION, candidate_id=candidate_id,
        candidate_content_fingerprint=fingerprint or digest(candidate_id),
        candidate_analysis_version=analysis_version)
    return cache.judgment_key(identity, question)


def test_a_touch_misses_only_what_it_touches(library, monkeypatch):
    monkeypatch.setattr(cache, "utc_now", TickingClock())
    frequency = build("full_frequency")
    transient = build("full_transient")
    frequency_outcome = outcome_named("all_judged", "candidate-001")
    transient_outcome = outcome_named("all_judged", "candidate-002")

    stored = {
        "a-frequency": identity_key_for(frequency),
        "a-transient": identity_key_for(transient,
                                        model_version=transient_outcome.judgment.model_version),
        "b-frequency": identity_key_for(frequency, candidate_id="bass-002"),
    }
    assert cache.store_judgment(library, stored["a-frequency"],
                                outcome=frequency_outcome).stored is True
    assert cache.store_judgment(library, stored["a-transient"],
                                outcome=transient_outcome).stored is True
    assert cache.store_judgment(library, stored["b-frequency"],
                                outcome=frequency_outcome).stored is True
    before = cache.cache_stats(library)

    # A candidate whose content identity and id changed misses; every other
    # candidate and dimension under the same palette hash still hits.
    moved = identity_key_for(frequency, candidate_id="bass-009")
    assert_miss(cache.lookup_judgment(library, moved, question=frequency), "absent", False)
    assert_miss(cache.lookup_judgment(library,
                                      identity_key_for(frequency, fingerprint="f" * 64),
                                      question=frequency), "absent", False)
    # The same candidate re-analysed under another analysis version misses.
    assert_miss(cache.lookup_judgment(library,
                                      identity_key_for(frequency,
                                                       analysis_version="dsp-fixture-2"),
                                      question=frequency), "absent", False)
    # A different palette misses every entry for that palette and no other.
    assert_miss(cache.lookup_judgment(library,
                                      identity_key_for(frequency, palette="9" * 64),
                                      question=frequency), "absent", False)
    # A deployment carrying a different PROMPT_VERSION changes the key's
    # prompt_version and every question_id.
    other_prompt = "jev-questions-v2"
    other_question = JevQuestion(question_id="q-" + "a" * 64, dimension="frequency",
                                 prompt_version=other_prompt, instruction="", evidence=(),
                                 withheld=())
    assert_miss(cache.lookup_judgment(library,
                                      identity_key_for(other_question,
                                                       prompt_version=other_prompt),
                                      question=other_question), "absent", False)
    for name in ("a-frequency", "a-transient", "b-frequency"):
        question = transient if name == "a-transient" else frequency
        assert cache.lookup_judgment(library, stored[name], question=question).status == "hit", name

    # The kick's identity and content live only in a candidate-decision key.
    def candidate_key(kick_fingerprint):
        identity = CandidateDecisionIdentity(
            source="double", interface_name="jev-contract-double", adapter_version=ADAPTER_VERSION,
            prompt_version=PROMPT_VERSION, model_version="synthetic-model-1",
            palette_hash=BASE_PALETTE_HASH, palette_hash_version=PALETTE_HASH_VERSION,
            candidate_id="bass-001", candidate_content_fingerprint=BASE_BASS_FINGERPRINT,
            candidate_analysis_version="dsp-fixture-1", kick_id="kick-001",
            kick_content_fingerprint=kick_fingerprint, kick_analysis_version="dsp-fixture-1",
            ranking_version="hybrid-ranking-v1", weight_table_id="hybrid-weights-1",
            baseline_ranking_version="dsp-baseline-v1",
            baseline_weight_table_id="dsp-baseline-weights-1")
        return cache.candidate_decision_key(identity, (frequency,))

    assert cache.store_candidate_decision(library, candidate_key(BASE_KICK_FINGERPRINT),
                                          decision=plain_decision()).stored is True
    after_store = cache.cache_stats(library)
    assert after_store.entries == before.entries + 1
    assert_miss(cache.lookup_candidate_decision(library, candidate_key(digest("kick-002")),
                                                questions=(frequency,)), "absent", False)
    assert_miss(cache.lookup_candidate_decision(library, candidate_key("e" * 64),
                                                questions=(frequency,)), "absent", False)
    assert cache.lookup_candidate_decision(library, candidate_key(BASE_KICK_FINGERPRINT),
                                           questions=(frequency,)).status == "hit"
    # No lookup deleted anything: the only count change is the store above.
    assert cache.cache_stats(library) == after_store
    assert cache.cache_stats(library).entries == 4



# ---------------------------------------------------------------------------
# a repeat request hits without a Jev call; a miss is the caller's signal
# ---------------------------------------------------------------------------


def test_a_repeat_request_hits_without_a_jev_call(library, monkeypatch):
    monkeypatch.setattr(cache, "utc_now", TickingClock())
    question = build("full_frequency")
    requests = [JevScoringRequest("candidate-001", question)]
    double = JevContractDouble(model_version=BASE_JUDGMENT_IDENTITY.model_version)
    first = score_questions(requests, transport=double, sleep=lambda seconds: None,
                            monotonic=lambda: 0.0)
    assert first.outcomes[0].state == "judged" and len(double.calls) == 1
    key = cache.judgment_key(BASE_JUDGMENT_IDENTITY, question)

    miss = cache.lookup_judgment(library, key, question=question)
    assert_miss(miss, "absent", False)
    second = score_questions(requests, transport=double, sleep=lambda seconds: None,
                             monotonic=lambda: 0.0)
    assert len(double.calls) == 2
    assert cache.store_judgment(library, key, outcome=second.outcomes[0]).stored is True

    sends = len(double.calls)
    monkeypatch.setattr(jev_module, "score_questions", boom)
    hit = cache.lookup_judgment(library, key, question=question)
    assert hit.status == "hit" and hit.reason is None and hit.stale_found is False
    assert hit.entry_created_at is not None and hit.decision is None
    assert len(double.calls) == sends, "a hit must issue zero sends"
    assert type(hit.judgment) is JevJudgment
    for name in ("dimension", "label", "confidence", "probabilities", "model_version",
                 "prompt_version", "unavailable_reason"):
        assert getattr(hit.judgment, name) == getattr(second.outcomes[0].judgment, name), name


def outcome_of_state(state, question):
    """One valid #14 outcome of the requested state for one asked question."""

    if state == "judged":
        return evidence_outcome()
    if state == "abstained":
        return outcome_named("all_judged", "candidate-003")
    digest_value = question.question_id
    if state == "not_asked":
        return JevOutcome(request_id="r-not-asked", question_id=None, dimension="frequency",
                          state=state, code="song_context_absent", attempts=0, judgment=None,
                          elapsed_ms=0)
    if state == "not_attempted":
        return JevOutcome(request_id="r-not-attempted", question_id=digest_value,
                          dimension="frequency", state=state, code="cancelled", attempts=0,
                          judgment=None, elapsed_ms=0)
    if state == "invalid_result":
        return JevOutcome(request_id="r-invalid", question_id=digest_value, dimension="frequency",
                          state=state, code="invalid_probabilities", attempts=1, judgment=None,
                          elapsed_ms=0)
    if state == "service_error":
        return JevOutcome(request_id="r-service", question_id=digest_value, dimension="frequency",
                          state=state, code="connection_failed", attempts=1, judgment=None,
                          elapsed_ms=0)
    if state == "timed_out":
        return JevOutcome(request_id="r-timeout", question_id=digest_value, dimension="frequency",
                          state=state, code="timeout", attempts=1, judgment=None, elapsed_ms=0)
    return JevOutcome(request_id="r-unavailable", question_id=None, dimension="frequency",
                      state=state, code="credentials_absent", attempts=0, judgment=None,
                      elapsed_ms=0)


def test_only_the_validated_terminal_states_are_storable(library, monkeypatch):
    monkeypatch.setattr(cache, "utc_now", TickingClock())
    storable = set()
    for state in OUTCOME_STATES:
        # The abstention's only landed outcome answers the tonal question.
        question = build("full_tonal") if state == "abstained" else build("full_frequency")
        outcome = outcome_of_state(state, question)
        assert outcome.state == state
        identity = JudgmentIdentity(**{**BASE_JUDGMENT_IDENTITY.to_dict(),
                                       "model_version": outcome.judgment.model_version
                                       if outcome.judgment is not None
                                       else BASE_JUDGMENT_IDENTITY.model_version})
        key = cache.judgment_key(identity, question)
        before = cache.cache_stats(library).entries
        if state in CACHEABLE_OUTCOME_STATES:
            result = cache.store_judgment(library, key, outcome=outcome)
            assert result.stored is True, state
            assert cache.cache_stats(library).entries == before + 1, state
            storable.add(state)
        else:
            with pytest.raises(DecisionNotCacheable) as caught:
                cache.store_judgment(library, key, outcome=outcome)
            assert caught.value.code == "decision_not_cacheable", state
            assert cache.cache_stats(library).entries == before, state
    assert storable == set(CACHEABLE_OUTCOME_STATES)
    assert storable < set(OUTCOME_STATES)


def test_store_judgment_refuses_a_key_whose_version_differs(library, monkeypatch):
    from dataclasses import replace

    monkeypatch.setattr(cache, "utc_now", TickingClock())
    question = build("full_frequency")
    outcome = evidence_outcome()
    other = JudgmentIdentity(**{**BASE_JUDGMENT_IDENTITY.to_dict(),
                                "model_version": "synthetic-model-2"})
    key = cache.judgment_key(other, question)
    with pytest.raises(CacheKeyInvalid) as caught:
        cache.store_judgment(library, key, outcome=outcome)
    assert caught.value.code == "cache_key_invalid"
    assert cache.cache_stats(library).entries == 0
    with pytest.raises(DecisionNotCacheable):
        cache.store_judgment(library, key, outcome="not an outcome")
    assert cache.cache_stats(library).entries == 0
    wrong_version = replace(outcome, judgment=replace(outcome.judgment,
                                                      prompt_version="jev-questions-v0"))
    with pytest.raises(CachePayloadInvalid) as caught:
        cache.store_judgment(library, cache.judgment_key(BASE_JUDGMENT_IDENTITY, question),
                             outcome=wrong_version)
    assert caught.value.code == "cache_payload_invalid"
    wrong_dimension = replace(outcome, judgment=replace(outcome.judgment, dimension="transient"))
    with pytest.raises(CachePayloadInvalid):
        cache.store_judgment(library, cache.judgment_key(BASE_JUDGMENT_IDENTITY, question),
                             outcome=wrong_dimension)
    wrong_label = replace(outcome, judgment=replace(outcome.judgment, label="poor"))
    with pytest.raises(CachePayloadInvalid):
        cache.store_judgment(library, cache.judgment_key(BASE_JUDGMENT_IDENTITY, question),
                             outcome=wrong_label)
    assert cache.cache_stats(library).entries == 0
    assert cache.store_judgment(library, cache.judgment_key(BASE_JUDGMENT_IDENTITY, question),
                                outcome=outcome).stored is True


def test_response_document_is_the_eight_field_contract_object():
    question = build("full_frequency")
    judgment = evidence_outcome().judgment
    document = cache.response_document(question, judgment)
    assert list(document) == ["question_id", "dimension", "label", "probabilities", "confidence",
                              "model_version", "prompt_version", "unavailable_reason"]
    assert [item["label"] for item in document["probabilities"]] == list(
        decisions_module.PROBABILITY_LABELS)
    assert validate_response(question, document) == judgment
    abstention = outcome_named("all_judged", "candidate-003").judgment
    refused = cache.response_document(question, abstention)
    assert refused["label"] is None and refused["probabilities"] is None
    assert refused["confidence"] is None and refused["unavailable_reason"] == MODEL_ABSTAINED
    with pytest.raises(CachePayloadInvalid):
        cache.response_document("not a question", judgment)


def test_store_candidate_decision_refuses_every_shape_it_documents(library, monkeypatch):
    from dataclasses import replace

    monkeypatch.setattr(cache, "utc_now", TickingClock())
    questions = questions_named(PAIR_QUESTIONS)
    key = cache.candidate_decision_key(BASE_CANDIDATE_IDENTITY, questions)
    good = plain_decision()
    assert cache.store_candidate_decision(library, key, decision=good).stored is True
    assert cache.cache_stats(library).entries == 1
    refusals = {
        "not_the_record": {"payload_version": cache.CACHE_PAYLOAD_VERSION},
        "payload_version": replace(good, payload_version="decision-cache-payload-v2"),
        "compatibility": replace(good, compatibility=1.5),
        "confidence": replace(good, confidence=-0.1),
        "candidate_id": replace(good, candidate_id="bass-009"),
        "analysis_version": replace(good, analysis_version="dsp-fixture-9"),
        "mode": replace(good, mode="synthetic-mode"),
        "jev_status": replace(good, jev_status="synthetic-status"),
        "dimension_order": replace(good, dimensions=tuple(reversed(good.dimensions))),
        "unknown_reason": replace(good, dimensions=tuple(
            replace(item, unavailable_reason="not an evidence code")
            if item.dimension == "frequency" else item for item in good.dimensions)),
        "both_set": replace(good, dimensions=tuple(
            replace(item, judgment=cached_judgment_for(build("full_frequency"),
                                                       evidence_outcome().judgment))
            if item.dimension == "frequency" else item for item in good.dimensions)),
        "neither_set": replace(good, dimensions=tuple(
            replace(item, unavailable_reason=None) if item.dimension == "frequency" else item
            for item in good.dimensions)),
        "warning_code": replace(good, warnings=("synthetic_warning",)),
        "judgment_dimension": replace(good, dimensions=tuple(
            replace(item, judgment=cached_judgment_for(
                build("full_transient"), outcome_named("all_judged", "candidate-002").judgment))
            if item.dimension == "frequency" else item for item in good.dimensions)),
        "payload_size": replace(good, dimensions=tuple(
            replace(item, dsp_unavailable_reason="x" * (cache.MAX_PAYLOAD_BYTES + 1))
            if item.dimension == "frequency" else item for item in good.dimensions)),
    }
    for name, bad in refusals.items():
        with pytest.raises(CachePayloadInvalid) as caught:
            cache.store_candidate_decision(library, key, decision=bad)
        assert caught.value.code == "cache_payload_invalid", name
        assert cache.cache_stats(library).entries == 1, name
    with pytest.raises(CachePayloadInvalid) as caught:
        cache.store_candidate_decision(library, key, decision={})
    assert caught.value.code == "cache_payload_invalid"
    assert good.to_dict() == plain_decision().to_dict()


# ---------------------------------------------------------------------------
# every read re-validates through #13; a stale document is a miss, never a value
# ---------------------------------------------------------------------------


def payload_row(connection, key):
    return connection.execute("SELECT * FROM decision_cache WHERE cache_key = ?",
                              (cache.cache_key_digest(key),)).fetchone()


def rewrite_response(connection, key, mutate):
    """Edit the stored #13 document of one row, the way a corrupted row would look."""

    row = payload_row(connection, key)
    payload = json.loads(row["payload_json"])
    document = json.loads(payload["response_json"])
    mutate(document)
    payload["response_json"] = canonical(document)
    connection.execute("UPDATE decision_cache SET payload_json = ? WHERE cache_key = ?",
                       (canonical(payload), cache.cache_key_digest(key)))


@pytest.mark.parametrize("edit", ["probability_sum", "prompt_version", "label"])
def test_a_stored_document_that_no_longer_validates_is_a_miss(library, monkeypatch, edit):
    monkeypatch.setattr(cache, "utc_now", TickingClock())
    question = build("full_frequency")
    key = cache.judgment_key(BASE_JUDGMENT_IDENTITY, question)
    assert cache.store_judgment(library, key, outcome=evidence_outcome()).stored is True
    if edit == "probability_sum":
        rewrite_response(library, key,
                         lambda document: document["probabilities"].__setitem__(
                             0, {"label": "very-poor", "probability": 0.5}))
    elif edit == "prompt_version":
        rewrite_response(library, key,
                         lambda document: document.__setitem__("prompt_version",
                                                                "jev-questions-v0"))
    else:
        rewrite_response(library, key, lambda document: document.__setitem__("label",
                                                                             "very-poor"))
    found = cache.lookup_judgment(library, key, question=question)
    assert_miss(found, "cached_response_invalid", True)
    assert found.judgment is None, "no stored judgment may ever be returned"
    assert payload_row(library, key) is not None, "a miss never deletes"
    assert cache.invalidate_entry(library, key) is True
    assert cache.lookup_judgment(library, key, question=question).reason == "absent"
    assert payload_row(library, key) is None
    assert cache.store_judgment(library, key, outcome=evidence_outcome()).stored is True


def test_a_stored_abstention_hits_and_keeps_its_reason(library, monkeypatch):
    monkeypatch.setattr(cache, "utc_now", TickingClock())
    question = build("full_tonal")
    outcome = outcome_named("all_judged", "candidate-003")
    assert outcome.state == "abstained"
    identity = JudgmentIdentity(**{**BASE_JUDGMENT_IDENTITY.to_dict(),
                                   "model_version": outcome.judgment.model_version})
    key = cache.judgment_key(identity, question)
    assert cache.store_judgment(library, key, outcome=outcome).stored is True
    hit = cache.lookup_judgment(library, key, question=question)
    assert hit.status == "hit"
    assert hit.judgment.label is None
    assert hit.judgment.unavailable_reason == MODEL_ABSTAINED
    assert hit.judgment.confidence is None and hit.judgment.probabilities == ()


def test_a_caller_whose_key_and_question_disagree_is_refused(library, monkeypatch):
    monkeypatch.setattr(cache, "utc_now", TickingClock())
    question = build("full_frequency")
    key = cache.judgment_key(BASE_JUDGMENT_IDENTITY, question)
    assert cache.store_judgment(library, key, outcome=evidence_outcome()).stored is True
    with pytest.raises(CacheKeyInvalid) as caught:
        cache.lookup_judgment(library, key, question=build("full_transient"))
    assert caught.value.code == "cache_key_invalid"
    with pytest.raises(CacheKeyInvalid):
        cache.lookup_judgment(library, key, question="not a question")
    questions = questions_named(PAIR_QUESTIONS)
    candidate_key = cache.candidate_decision_key(BASE_CANDIDATE_IDENTITY, questions)
    with pytest.raises(CacheKeyInvalid):
        cache.lookup_candidate_decision(library, candidate_key,
                                        questions=questions_named(("full_transient",
                                                                   "full_texture")))
    with pytest.raises(CacheKeyInvalid):
        cache.lookup_candidate_decision(library, candidate_key, questions=(question,))
    with pytest.raises(CacheKeyInvalid):
        cache.lookup_candidate_decision(library, candidate_key, questions="not a sequence")


# ---------------------------------------------------------------------------
# corrupt, partial and unsupported entries are misses, never exceptions or resets
# ---------------------------------------------------------------------------


def row_count(connection):
    return connection.execute("SELECT COUNT(*) FROM decision_cache").fetchone()[0]


def test_corrupt_partial_and_unsupported_entries_are_kept_misses(library, monkeypatch):
    monkeypatch.setattr(cache, "utc_now", TickingClock())
    question = build("full_frequency")
    outcome = evidence_outcome()
    keys = [cache.judgment_key(JudgmentIdentity(**{**BASE_JUDGMENT_IDENTITY.to_dict(),
                                                   "palette_hash": "%064x" % (index + 1)}),
                               question) for index in range(4)]
    for key in keys:
        assert cache.store_judgment(library, key, outcome=outcome).stored is True
    first, second, third, fourth = keys

    library.execute("PRAGMA ignore_check_constraints = ON")
    library.execute("UPDATE decision_cache SET payload_json = ? WHERE cache_key = ?",
                    ('{"payload_version": "decision-cache-payload-v1", "dimension":',
                     cache.cache_key_digest(first)))
    library.execute("PRAGMA ignore_check_constraints = OFF")
    payload = json.loads(payload_row(library, second)["payload_json"])
    payload.pop("attempts")
    payload["unexpected"] = 1
    library.execute("UPDATE decision_cache SET payload_json = ? WHERE cache_key = ?",
                    (canonical(payload), cache.cache_key_digest(second)))
    library.execute("PRAGMA ignore_check_constraints = ON")
    library.execute("UPDATE decision_cache SET cache_key_version = ? WHERE cache_key = ?",
                    ("decision-cache-v2", cache.cache_key_digest(third)))
    library.execute("PRAGMA ignore_check_constraints = OFF")
    payload = json.loads(payload_row(library, fourth)["payload_json"])
    payload["payload_version"] = "decision-cache-payload-v2"
    library.execute("UPDATE decision_cache SET payload_json = ? WHERE cache_key = ?",
                    (canonical(payload), cache.cache_key_digest(fourth)))

    before = cache.cache_stats(library)
    assert before.entries == 4
    for key, reason in ((first, "cache_corrupt"), (second, "cache_corrupt"),
                        (third, "version_unsupported"), (fourth, "version_unsupported")):
        found = cache.lookup_judgment(library, key, question=question)
        assert_miss(found, reason, True)
        assert payload_row(library, key) is not None, "an unsupported or corrupt row is kept"
    assert cache.cache_stats(library) == before
    assert row_count(library) == 4

    assert cache.invalidate_entry(library, first) is True
    assert cache.store_judgment(library, first, outcome=outcome).stored is True
    assert cache.lookup_judgment(library, first, question=question).status == "hit"
    assert cache.cache_stats(library).entries == 4


def test_the_table_checks_reject_a_row_that_contradicts_its_kind(library):
    values = {"cache_key": "0" * 64, "cache_key_version": "decision-cache-v1",
              "decision_kind": "judgment", "source": "double",
              "interface_name": "jev-contract-double", "adapter_version": ADAPTER_VERSION,
              "prompt_version": PROMPT_VERSION, "model_version": "synthetic-model-1",
              "palette_hash": BASE_PALETTE_HASH, "palette_hash_version": PALETTE_HASH_VERSION,
              "candidate_id": "bass-001", "candidate_content_fingerprint": BASE_BASS_FINGERPRINT,
              "candidate_analysis_version": "dsp-fixture-1", "dimension": "frequency",
              "question_id": "q-" + "0" * 64, "kick_id": "kick-001",
              "kick_content_fingerprint": BASE_KICK_FINGERPRINT,
              "kick_analysis_version": "dsp-fixture-1", "questions_digest": None,
              "ranking_version": None, "weight_table_id": None, "baseline_ranking_version": None,
              "baseline_weight_table_id": None, "payload_json": "{}",
              "created_at": "2024-01-01T00:00:00Z"}
    names = ", ".join(values)
    marks = ", ".join("?" for _ in values)
    with pytest.raises(sqlite3.IntegrityError):
        library.execute("INSERT INTO decision_cache (%s) VALUES (%s)" % (names, marks),
                        tuple(values.values()))
    assert row_count(library) == 0


# ---------------------------------------------------------------------------
# concurrent writers, one transaction per operation, recoverable write failure
# ---------------------------------------------------------------------------


def test_two_connections_cannot_duplicate_or_half_write_a_record(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "utc_now", TickingClock())
    path = tmp_path / "library.sqlite3"
    first = open_database(path)
    second = open_database(path)
    try:
        question = build("full_frequency")
        outcome = evidence_outcome()
        key = cache.judgment_key(BASE_JUDGMENT_IDENTITY, question)
        other_key = key_with_palette_hash("5" * 64)
        repository = LibraryRepository(first)

        left = cache.store_judgment(first, key, outcome=outcome)
        right = cache.store_judgment(second, key, outcome=outcome)
        assert sorted([left.stored, right.stored]) == [False, True]
        assert left.created_at == right.created_at
        assert row_count(first) == 1
        one = cache.lookup_judgment(first, key, question=question)
        two = cache.lookup_judgment(second, key, question=question)
        assert one.judgment == two.judgment and one.entry_created_at == two.entry_created_at

        first.execute("BEGIN IMMEDIATE")
        first.execute("UPDATE decision_cache SET created_at = created_at")
        during = cache.lookup_judgment(second, other_key, question=question)
        assert_miss(during, "absent", False)
        first.execute("COMMIT")
        assert second.in_transaction is False
        assert second.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"

        first.execute("BEGIN IMMEDIATE")
        first.execute("UPDATE decision_cache SET created_at = created_at")
        before = cache.cache_stats(first)
        with pytest.raises(DatabaseLocked) as caught:
            cache.store_judgment(second, other_key, outcome=outcome)
        assert caught.value.code == "database_locked"
        first.execute("COMMIT")
        assert cache.cache_stats(second).entries == before.entries
        assert cache.store_judgment(second, other_key, outcome=outcome).stored is True

        third_key = key_with_palette_hash("6" * 64)
        values = dict(third_key.to_dict())
        values["cache_key"] = cache.cache_key_digest(third_key)
        values["payload_json"] = canonical(plain_decision().to_dict())
        values["created_at"] = "2024-01-01T00:00:00Z"
        first.execute("BEGIN IMMEDIATE")
        repository.insert_decision_cache_entry(values)
        with pytest.raises(DatabaseLocked):
            cache.invalidate_candidate(second, candidate_id="bass-001")
        first.execute("COMMIT")
        assert payload_row(second, third_key) is not None
        assert cache.invalidate_candidate(second, candidate_id="bass-001") == 3
        assert cache.cache_stats(second).entries == 0
    finally:
        first.close()
        second.close()


def test_a_failed_store_leaves_every_entry_and_the_decision_untouched(library, monkeypatch):
    monkeypatch.setattr(cache, "utc_now", TickingClock())
    question = build("full_frequency")
    outcome = evidence_outcome()
    key = cache.judgment_key(BASE_JUDGMENT_IDENTITY, question)
    other_key = key_with_palette_hash("7" * 64)
    assert cache.store_judgment(library, key, outcome=outcome).stored is True
    before = cache.cache_stats(library)
    library.execute("CREATE TRIGGER synthetic_refusal BEFORE INSERT ON decision_cache "
                    "BEGIN SELECT RAISE(ABORT, 'synthetic refusal'); END")
    with pytest.raises(WriteFailed) as caught:
        cache.store_judgment(library, other_key, outcome=outcome)
    assert caught.value.code == "write_failed"
    assert isinstance(caught.value, LibraryError)
    assert cache.cache_stats(library) == before
    assert payload_row(library, other_key) is None
    assert cache.lookup_judgment(library, key, question=question).status == "hit"
    library.execute("DROP TRIGGER synthetic_refusal")
    result = cache.store_judgment(library, other_key, outcome=outcome)
    assert result.stored is True and result.pruned == 0
    assert cache.lookup_judgment(library, other_key, question=question).status == "hit"


# ---------------------------------------------------------------------------
# bounds and pruning are explicit, exact and deterministic
# ---------------------------------------------------------------------------


def key_with_palette_hash(value, identity=BASE_JUDGMENT_IDENTITY):
    return cache.judgment_key(JudgmentIdentity(**{**identity.to_dict(), "palette_hash": value}),
                              build("full_frequency"))


def test_the_entry_bound_prunes_the_oldest_and_never_the_new_row(library, monkeypatch):
    monkeypatch.setattr(cache, "utc_now", TickingClock())
    outcome = evidence_outcome()
    keys = [key_with_palette_hash("%064x" % (index + 1)) for index in range(5)]
    for index, key in enumerate(keys):
        expected = 1 if index >= 3 else 0
        result = cache.store_judgment(library, key, outcome=outcome, max_entries=3,
                                      max_bytes=4096)
        assert result.stored is True and result.pruned == expected, index
        assert cache.cache_stats(library).entries <= 3, index
    assert stored_keys(library) == {cache.cache_key_digest(key) for key in keys[2:]}
    repeat = cache.store_judgment(library, keys[4], outcome=outcome, max_entries=1,
                                  max_bytes=4096)
    assert repeat.stored is False and repeat.pruned == 0
    assert repeat.created_at == cache.cache_stats(library).newest_created_at
    newest = key_with_palette_hash("f" * 64)
    result = cache.store_judgment(library, newest, outcome=outcome, max_entries=1,
                                  max_bytes=4096)
    assert result.stored is True and result.pruned == 3
    assert stored_keys(library) == {cache.cache_key_digest(newest)}
    before = stored_keys(library)
    cache.lookup_judgment(library, newest, question=build("full_frequency"))
    cache.cache_stats(library)
    assert stored_keys(library) == before


def test_the_byte_bound_evicts_oldest_first_and_is_never_exceeded(library, monkeypatch):
    monkeypatch.setattr(cache, "utc_now", TickingClock())
    question = build("full_frequency")
    outcome = evidence_outcome()
    keys = [key_with_palette_hash("%064x" % (index + 1)) for index in range(6)]
    for key in keys:
        cache.store_judgment(library, key, outcome=outcome, max_entries=50, max_bytes=4096)
        stats = cache.cache_stats(library)
        assert stats.bytes <= 4096 and stats.entries <= 50
    assert stored_keys(library) < {cache.cache_key_digest(key) for key in keys}
    assert cache.cache_key_digest(keys[-1]) in stored_keys(library)
    report = cache.prune_cache(library, max_entries=1, max_bytes=4096)
    assert report.deleted > 0 and report.remaining_entries == 1
    assert report.remaining_bytes <= 4096
    assert report.max_entries == 1 and report.max_bytes == 4096
    assert stored_keys(library) == {cache.cache_key_digest(keys[-1])}
    again = cache.prune_cache(library, max_entries=1, max_bytes=4096)
    assert again.deleted == 0 and again.remaining_entries == 1
    assert again.remaining_bytes == report.remaining_bytes


def test_bounds_outside_the_published_range_are_refused(library, monkeypatch):
    monkeypatch.setattr(cache, "utc_now", TickingClock())
    key = key_with_palette_hash("a" * 64)
    outcome = evidence_outcome()
    for entries, size in ((0, 4096), (1000001, 4096), (True, 4096), (3, 4095),
                          (3, 1073741825), (3, True), (3, None), ("3", 4096)):
        with pytest.raises(InvalidCacheBound) as caught:
            cache.store_judgment(library, key, outcome=outcome, max_entries=entries,
                                 max_bytes=size)
        assert caught.value.code == "invalid_cache_bound"
    for entries, size in ((0, 4096), (3, 4095), (None, 4096)):
        with pytest.raises(InvalidCacheBound):
            cache.prune_cache(library, max_entries=entries, max_bytes=size)
    assert cache.cache_stats(library).entries == 0


# ---------------------------------------------------------------------------
# invalidation is explicit, targeted and idempotent
# ---------------------------------------------------------------------------


def test_invalidation_is_explicit_targeted_and_idempotent(library, monkeypatch):
    monkeypatch.setattr(cache, "utc_now", TickingClock())
    question = build("full_frequency")
    outcome = evidence_outcome()
    keys = {}
    for candidate in ("bass-001", "bass-002"):
        identity = JudgmentIdentity(**{**BASE_JUDGMENT_IDENTITY.to_dict(),
                                       "candidate_id": candidate,
                                       "candidate_content_fingerprint": digest(candidate)})
        keys[candidate] = cache.judgment_key(identity, question)
        assert cache.store_judgment(library, keys[candidate], outcome=outcome).stored is True
        candidate_identity = CandidateDecisionIdentity(
            **{**BASE_CANDIDATE_IDENTITY.to_dict(), "candidate_id": candidate,
               "candidate_content_fingerprint": digest(candidate)})
        keys[candidate + "-decision"] = cache.candidate_decision_key(candidate_identity,
                                                                     (question,))
        assert cache.store_candidate_decision(
            library, keys[candidate + "-decision"],
            decision=plain_decision(candidate_id=candidate)).stored is True
    assert cache.cache_stats(library).entries == 4
    assert cache.invalidate_entry(library, keys["bass-001"]) is True
    assert cache.invalidate_entry(library, keys["bass-001"]) is False
    assert cache.lookup_judgment(library, keys["bass-002"], question=question).status == "hit"
    assert cache.lookup_candidate_decision(library, keys["bass-002-decision"],
                                           questions=(question,)).status == "hit"
    assert cache.invalidate_candidate(library, candidate_id="bass-002") == 2
    assert cache.invalidate_candidate(library, candidate_id="bass-002") == 0
    assert cache.invalidate_candidate(library, candidate_id="bass-001") == 1
    assert cache.invalidate_candidate(library, candidate_id="bass-001") == 0
    assert cache.cache_stats(library).entries == 0
    with pytest.raises(CacheKeyInvalid):
        cache.invalidate_candidate(library, candidate_id="   ")


def test_a_cache_row_survives_its_samples_row_and_needs_explicit_invalidation(library,
                                                                             monkeypatch):
    from dataclasses import replace

    monkeypatch.setattr(cache, "utc_now", TickingClock())
    repository = LibraryRepository(library)
    version = repository.register_analysis_version(batch.analysis_descriptor())
    _, bass = RecommendationBatch.from_json(
        (CONTRACT_FIXTURES / "hybrid.json").read_text(encoding="utf-8")).samples
    sample = replace(bass, sample_id="bass-003", analysis_version=version,
                     audio=replace(bass.audio, local_path="C:/tera-fixtures/bass-003.wav"))
    repository.import_sample(sample, content_sha256=digest("bass-003"))
    question = build("full_frequency")
    identity = JudgmentIdentity(**{**BASE_JUDGMENT_IDENTITY.to_dict(), "candidate_id": "bass-003",
                                   "candidate_analysis_version": version,
                                   "candidate_content_fingerprint": digest("bass-003")})
    key = cache.judgment_key(identity, question)
    assert cache.store_judgment(library, key, outcome=evidence_outcome()).stored is True
    pin = cache.record_model_version(library, run_adapter_case(ADAPTER_CASE_BY_NAME[PIN_CASE])[2])
    assert pin is not None
    assert library.execute("PRAGMA foreign_key_check").fetchall() == []
    assert library.execute("DELETE FROM samples WHERE sample_id = ?",
                           ("bass-003",)).rowcount == 1
    assert library.execute("SELECT 1 FROM decision_cache WHERE candidate_id = ?",
                           ("bass-003",)).fetchone() is not None
    assert cache.invalidate_candidate(library, candidate_id="bass-003") == 1
    assert library.execute("SELECT 1 FROM decision_cache WHERE candidate_id = ?",
                           ("bass-003",)).fetchone() is None
    assert cache.pinned_model_version(library, interface_name=pin.interface_name,
                                      source=pin.source, adapter_version=pin.adapter_version,
                                      prompt_version=pin.prompt_version) == pin


# ---------------------------------------------------------------------------
# the decision-model version is pinned per interface
# ---------------------------------------------------------------------------


def test_the_model_version_pin_follows_the_observed_run(library, monkeypatch):
    from dataclasses import replace

    monkeypatch.setattr(cache, "utc_now", TickingClock())
    _, _, run = run_adapter_case(ADAPTER_CASE_BY_NAME[PIN_CASE])
    pin = cache.record_model_version(library, run)
    assert pin.model_version == "synthetic-model-1" and pin.observation_count == 1
    assert (pin.interface_name, pin.source) == ("jev-contract-double", "double")
    assert pin.prompt_version == PROMPT_VERSION
    assert cache.pinned_model_version(
        library, interface_name=pin.interface_name, source="interface",
        adapter_version=pin.adapter_version, prompt_version=pin.prompt_version) is None
    assert cache.pinned_model_version(
        library, interface_name=pin.interface_name, source=pin.source,
        adapter_version=pin.adapter_version, prompt_version=pin.prompt_version) == pin
    again = cache.record_model_version(library, run)
    assert again.observation_count == 2
    assert again.first_observed_at == pin.first_observed_at
    assert again.observed_at != pin.observed_at
    many = cache.record_model_version(
        library, run_adapter_case(ADAPTER_CASE_BY_NAME["all_judged"])[2])
    assert many is None
    empty = cache.record_model_version(
        library, run_adapter_case(ADAPTER_CASE_BY_NAME["empty_batch"])[2])
    assert empty is None
    assert cache.record_model_version(library, replace(run, cancelled=True)) is None
    unavailable = JevScoringRun(adapter_version=ADAPTER_VERSION, source="unavailable",
                                interface_name=None, prompt_version=PROMPT_VERSION,
                                model_versions=(), cancelled=False, outcomes=(), elapsed_ms=0)
    assert cache.record_model_version(library, unavailable) is None
    with pytest.raises(CachePayloadInvalid):
        cache.record_model_version(library, "not a run")
    with pytest.raises(CacheKeyInvalid):
        cache.pinned_model_version(library, interface_name="   ", source="double",
                                   adapter_version=ADAPTER_VERSION,
                                   prompt_version=PROMPT_VERSION)
    with pytest.raises(CacheKeyInvalid):
        cache.pinned_model_version(library, interface_name="jev-contract-double",
                                   source="synthetic-source", adapter_version=ADAPTER_VERSION,
                                   prompt_version=PROMPT_VERSION)
    assert ModelVersionPin.from_dict(pin.to_dict()) == pin
    assert ModelVersionPin.from_json(pin.to_json()) == pin
    with pytest.raises(CachePayloadInvalid):
        ModelVersionPin.from_dict({"model_version": "x"})


def test_a_superseded_model_version_is_never_served(library, monkeypatch):
    monkeypatch.setattr(cache, "utc_now", TickingClock())
    question = build("full_frequency")
    key_a = cache.judgment_key(BASE_JUDGMENT_IDENTITY, question)
    outcome_a = evidence_outcome()
    assert outcome_a.judgment.model_version == "synthetic-model-1"
    assert cache.store_judgment(library, key_a, outcome=outcome_a).stored is True

    requests = [JevScoringRequest("candidate-001", question)]
    double = JevContractDouble(model_version="synthetic-model-2")
    live = score_questions(requests, transport=double, sleep=lambda seconds: None,
                           monotonic=lambda: 0.0)
    pin = cache.record_model_version(library, live)
    assert pin.model_version == "synthetic-model-2"
    assert cache.pinned_model_version(
        library, interface_name="jev-contract-double", source="double",
        adapter_version=ADAPTER_VERSION, prompt_version=PROMPT_VERSION) == pin

    key_b = cache.judgment_key(
        JudgmentIdentity(**{**BASE_JUDGMENT_IDENTITY.to_dict(),
                            "model_version": pin.model_version}), question)
    assert_miss(cache.lookup_judgment(library, key_b, question=question), "absent", False)
    assert cache.lookup_judgment(library, key_a, question=question).status == "hit"
    assert cache.store_judgment(library, key_b, outcome=live.outcomes[0]).stored is True
    assert cache.lookup_judgment(library, key_b, question=question).status == "hit"
    assert row_count(library) == 2, "the superseded entry survives until pruned or invalidated"

