"""Synthetic Jev question fixtures; no real audio, library, Jev or network use.

Every declared value is synthetic. Each fixture case declares its expected
outcome, and the awkward inputs are built here from the contract records, never
read back from the implementation.
"""

import json
import math
import re
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from typing import get_args

import pytest

from backend.contracts import (AudioFeatures, AudioMetadata, Dimension, Label, MEASURES, Measurement,
                               MusicalKey, Sample, SongContext)
from backend.palette.compatibility import CONFIDENCE_THRESHOLD
from backend.intelligence import questions as questions_module
from backend.intelligence.questions import (DIMENSIONS, INSTRUCTIONS, INPUT_ERROR_CODES,
                                            OPTIONAL_EVIDENCE, PROMPT_VERSION,
                                            QUESTION_UNAVAILABLE_CODES, REQUIRED_EVIDENCE,
                                            WITHHELD_REASONS, JevQuestion, QuestionInputError,
                                            UnavailableQuestion, build_question)


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "jev"
DOCUMENT = ROOT / "_docs" / "jev-questions.md"
QUESTIONS_FILE = ROOT / "backend" / "intelligence" / "questions.py"
DECISIONS_FILE = ROOT / "backend" / "intelligence" / "decisions.py"

TICK = chr(96)
RATE = 48000
FRAMES = 48000
ANALYSIS = "jev-test-1"
PROBE_PATH = r"C:\Users\synthetic-producer\Secret Library\zzz-distinctive-9f8a\silent-monolith-9f8a.wav"

BAND_NAMES = ("band_sub", "band_bass", "band_low_mid", "band_mid", "band_high_mid", "band_high")
CONTEXT_NAMES = ("key", "role", "genre")
REASONS = ("unknown", "below_reliability_threshold", "not_supplied")

# Acceptance criterion 3's required and optional evidence, transcribed by hand
# so this test never restates the implementation's own mapping.
EXPECTED_REQUIRED = {
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
EXPECTED_OPTIONAL = {
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


def build(case):
    return build_question(case["dimension"],
                          side_sample(case["kick"], "kick-001", "kick"),
                          side_sample(case["candidate"], "bass-001", "bass"),
                          song=song_context(case["song"]))


def full_pair(name="full_frequency"):
    case = CASE_BY_NAME[name]
    return (side_sample(case["kick"], "kick-001", "kick"),
            side_sample(case["candidate"], "bass-001", "bass"), song_context(case["song"]))


def with_measurement(sample, name, value, confidence=None):
    measurements = tuple(measure(name, value, confidence) if item.name == name else item
                         for item in sample.features.measurements)
    return replace(sample, features=replace(sample.features, measurements=measurements))


def with_key(sample, tonic, mode, confidence):
    return replace(sample, features=replace(sample.features,
                                            key=MusicalKey(tonic=tonic, mode=mode,
                                                           confidence=confidence,
                                                           unavailable_reason=None if tonic
                                                           is not None else "insufficient_key_context")))


def documented_order(dimension):
    return tuple(REQUIRED_EVIDENCE[dimension]) + tuple(OPTIONAL_EVIDENCE[dimension])


def withheld_triples(question):
    return [(item.side, item.name, item.reason) for item in question.withheld]


def expected_code(side, name, reason):
    if reason == "zero_energy":
        return f"{side}_band_energy_zero"
    if reason == "not_supplied":
        return "song_context_absent"
    token = "f0" if name == "fundamental" else name
    return f"{side}_{token}_{'unknown' if reason == 'unknown' else 'unreliable'}"


def check_invariants(case, question):
    """Rules every case must satisfy whatever its outcome."""
    order = documented_order(case["dimension"])
    withheld = withheld_triples(question)
    positions = [order.index((side, name)) for side, name, _ in withheld]
    assert positions == sorted(positions)
    assert len({(side, name) for side, name, _ in withheld}) == len(withheld)
    assert all(reason in REASONS for _, _, reason in withheld)
    if type(question) is JevQuestion:
        presented = [(fact.side, fact.name) for fact in question.evidence]
        assert len(set(presented)) == len(presented)
        assert not set(presented) & {(side, name) for side, name, _ in withheld}
        assert set(order) <= set(presented) | {(side, name) for side, name, _ in withheld}
        assert [order.index(item) for item in presented] == sorted(order.index(item) for item in presented)
        for fact in question.evidence:
            assert fact.name in MEASURES or fact.name in CONTEXT_NAMES
            assert fact.side in ("kick", "candidate", "song")
            if fact.name in MEASURES:
                assert fact.unit == MEASURES[fact.name][0] and fact.value is not None
            else:
                assert fact.unit is None and type(fact.value) is str and fact.value.strip()
    else:
        assert type(question.withheld) is tuple
        assert question.dimension == case["dimension"]


def check_blocked(case, question, blocked):
    side, name, reason = blocked
    if reason == "zero_energy":
        withheld = {(item.side, item.name) for item in question.withheld}
        assert not any((side, band) in withheld for band in BAND_NAMES)
        return
    assert type(question) is UnavailableQuestion
    result = {(item.side, item.name): item.reason for item in question.withheld}
    withheld_reason = "below_reliability_threshold" if reason == "unreliable" else reason
    assert result.get((side, name)) == withheld_reason
    order = documented_order(case["dimension"])
    for entry in order[:order.index((side, name))]:
        if entry in result:
            # only the non-blocking tonal fundamental guard may be withheld earlier
            assert entry[1] == "fundamental" and result[entry] == "unknown"


@pytest.mark.parametrize("case", QUESTION_CASES, ids=lambda case: case["name"])
def test_question_fixture_cases(case):
    question = build(case)
    expects = case["expects"]
    assert question.dimension == case["dimension"]
    if expects["outcome"] == "question":
        assert type(question) is JevQuestion
        assert question.prompt_version == PROMPT_VERSION
        assert question.instruction == INSTRUCTIONS[case["dimension"]]
        assert question.question_id.strip()
    else:
        assert type(question) is UnavailableQuestion
        assert question.code == expects["code"]
        assert question.code in QUESTION_UNAVAILABLE_CODES
    check_invariants(case, question)
    if "withheld" in expects:
        assert withheld_triples(question) == [tuple(item) for item in expects["withheld"]]
    if "evidence" in expects:
        assert [[fact.side, fact.name] for fact in question.evidence] == expects["evidence"]
    if "blocked" in expects:
        assert expected_code(*expects["blocked"]) == expects["code"]
        check_blocked(case, question, expects["blocked"])


def test_prompt_version_dimensions_and_instructions_match_the_contract():
    assert PROMPT_VERSION.strip() == PROMPT_VERSION and PROMPT_VERSION
    assert DIMENSIONS == get_args(Dimension)
    assert set(DIMENSIONS) == set(get_args(Dimension))
    assert set(REQUIRED_EVIDENCE) == set(OPTIONAL_EVIDENCE) == set(INSTRUCTIONS) == set(DIMENSIONS)
    for dimension in DIMENSIONS:
        instruction = INSTRUCTIONS[dimension]
        assert instruction.strip()
        for label in get_args(Label):
            assert label in instruction
        assert "probability" in instruction and "confidence" in instruction
        for forbidden in ("compatibility", "score", "rank", "overall", "ordering", "local_path"):
            assert forbidden not in instruction


def test_required_and_optional_evidence_are_documented_contract_facts():
    assert REQUIRED_EVIDENCE == EXPECTED_REQUIRED
    assert OPTIONAL_EVIDENCE == EXPECTED_OPTIONAL
    for mapping in (REQUIRED_EVIDENCE, OPTIONAL_EVIDENCE):
        for dimension, entries in mapping.items():
            assert dimension in DIMENSIONS
            assert list(entries) == list(dict.fromkeys(entries))
            for side, name in entries:
                assert side in ("kick", "candidate", "song")
                assert name in MEASURES or name in CONTEXT_NAMES
    for dimension in DIMENSIONS:
        assert not set(REQUIRED_EVIDENCE[dimension]) & set(OPTIONAL_EVIDENCE[dimension])
    assert tuple(REQUIRED_EVIDENCE) == DIMENSIONS
    assert tuple(OPTIONAL_EVIDENCE) == DIMENSIONS


def test_every_unavailable_code_is_declared_and_exercised_by_a_case():
    raised = {case["expects"]["code"] for case in QUESTION_CASES
              if case["expects"]["outcome"] == "unavailable"}
    assert raised == set(QUESTION_UNAVAILABLE_CODES)
    assert all(type(code) is str and code.strip() for code in QUESTION_UNAVAILABLE_CODES)
    # a label is never an abstention: neutral and poor are judgments, not refusals
    assert not set(QUESTION_UNAVAILABLE_CODES) & set(get_args(Label))
    assert "song_context_absent" in QUESTION_UNAVAILABLE_CODES


def test_one_question_is_one_dimension_and_no_dimension_is_combined():
    kick, candidate, song = full_pair()
    for dimension in DIMENSIONS:
        question = build_question(dimension, kick, candidate, song=song)
        assert type(question) is JevQuestion
        assert question.dimension == dimension
        assert set(question.to_dict()) == {"question_id", "dimension", "prompt_version", "instruction",
                                           "evidence", "withheld"}
    assert len(DIMENSIONS) == len(set(DIMENSIONS)) == 6


def test_input_errors_for_dimensions_records_roles_ids_and_context():
    kick, candidate, song = full_pair()
    for broken in (None, "frequencyFit", "overall", 7, "Frequency", ""):
        with pytest.raises(QuestionInputError) as error:
            build_question(broken, kick, candidate, song=song)
        assert error.value.code == "invalid_dimension"
    for broken in (None, {}, "kick-001", 7):
        with pytest.raises(QuestionInputError) as error:
            build_question("frequency", broken, candidate, song=song)
        assert error.value.code == "invalid_kick"
    with pytest.raises(QuestionInputError) as error:
        build_question("frequency", candidate, candidate, song=song)
    assert error.value.code == "invalid_kick"
    malformed = replace(kick)
    object.__setattr__(malformed, "role", "not-a-role")
    with pytest.raises(QuestionInputError) as error:
        build_question("frequency", malformed, candidate, song=song)
    assert error.value.code == "invalid_kick"
    for broken in (None, {}, "bass-001", 7):
        with pytest.raises(QuestionInputError) as error:
            build_question("frequency", kick, broken, song=song)
        assert error.value.code == "invalid_candidate"
    for broken in (replace(candidate, role="kick"),
                   replace(candidate, sample_id=kick.sample_id)):
        with pytest.raises(QuestionInputError) as error:
            build_question("frequency", kick, broken, song=song)
        assert error.value.code == "invalid_candidate"
    tampered_tempo = replace(song)
    object.__setattr__(tampered_tempo, "tempo", measure("peak", 0.5))
    for broken in ({}, "song", 7, tampered_tempo):
        with pytest.raises(QuestionInputError) as error:
            build_question("frequency", kick, candidate, song=broken)
        assert error.value.code == "invalid_context"
    tampered = replace(song)
    object.__setattr__(tampered, "genre", None)
    with pytest.raises(QuestionInputError) as error:
        build_question("frequency", kick, candidate, song=tampered)
    assert error.value.code == "invalid_context"


def test_a_withheld_fact_is_never_presented_defaulted_or_guessed():
    kick, candidate, song = full_pair()
    unknown_f0 = build_question("tonal", with_measurement(kick, "fundamental", None), candidate, song=song)
    assert type(unknown_f0) is JevQuestion
    assert ("kick", "fundamental") not in [(fact.side, fact.name) for fact in unknown_f0.evidence]
    assert ("kick", "fundamental", "unknown") in withheld_triples(unknown_f0)
    # an undeclared confidence is usable, and is not a reliability claim
    declared = build_question("frequency", with_measurement(kick, "band_sub", 0.40, CONFIDENCE_THRESHOLD),
                              candidate, song=song)
    fact = next(item for item in declared.evidence if item.side == "kick" and item.name == "band_sub")
    assert fact.value == 0.40 and fact.unit == "ratio"
    # a declared confidence below the shared threshold withholds the fact
    unreliable = build_question("frequency", with_measurement(kick, "band_sub", 0.40,
                                                             math.nextafter(CONFIDENCE_THRESHOLD, 0)),
                                candidate, song=song)
    assert type(unreliable) is UnavailableQuestion
    assert unreliable.code == "kick_band_sub_unreliable"
    assert ("kick", "band_sub", "below_reliability_threshold") in withheld_triples(unreliable)


@pytest.mark.parametrize("confidence,outcome", [
    (math.nextafter(CONFIDENCE_THRESHOLD, 0), "unavailable"),
    (CONFIDENCE_THRESHOLD, "question"),
    (math.nextafter(CONFIDENCE_THRESHOLD, 1), "question"),
])
@pytest.mark.parametrize("side_name", ["kick", "candidate"])
def test_key_confidence_boundary(side_name, confidence, outcome):
    kick, candidate, song = full_pair()
    sample = with_key(kick if side_name == "kick" else candidate, "C", "major", confidence)
    question = build_question("tonal", sample if side_name == "kick" else kick,
                              candidate if side_name == "kick" else sample, song=song)
    if outcome == "question":
        assert type(question) is JevQuestion
    else:
        assert type(question) is UnavailableQuestion
        assert question.code == f"{side_name}_key_unreliable"
        assert (side_name, "key", "below_reliability_threshold") in withheld_triples(question)


@pytest.mark.parametrize("confidence,outcome", [
    (math.nextafter(CONFIDENCE_THRESHOLD, 0), "unavailable"),
    (CONFIDENCE_THRESHOLD, "question"),
    (math.nextafter(CONFIDENCE_THRESHOLD, 1), "question"),
])
@pytest.mark.parametrize("side_name", ["kick", "candidate"])
def test_known_fundamental_confidence_boundary(side_name, confidence, outcome):
    kick, candidate, song = full_pair()
    sample = with_measurement(kick if side_name == "kick" else candidate, "fundamental", 55.0, confidence)
    question = build_question("tonal", sample if side_name == "kick" else kick,
                              candidate if side_name == "kick" else sample, song=song)
    if outcome == "question":
        assert type(question) is JevQuestion
    else:
        assert type(question) is UnavailableQuestion and question.code == f"{side_name}_f0_unreliable"
        assert (side_name, "fundamental", "below_reliability_threshold") in withheld_triples(question)


@pytest.mark.parametrize("confidence,outcome", [
    (math.nextafter(CONFIDENCE_THRESHOLD, 0), "unavailable"),
    (CONFIDENCE_THRESHOLD, "question"),
    (math.nextafter(CONFIDENCE_THRESHOLD, 1), "question"),
])
@pytest.mark.parametrize("side_name", ["candidate", "song"])
def test_tempo_confidence_boundary(side_name, confidence, outcome):
    kick, candidate, song = full_pair()
    if side_name == "candidate":
        candidate = with_measurement(candidate, "tempo", 128.0, confidence)
    else:
        song = replace(song, tempo=measure("tempo", 128.0, confidence))
    question = build_question("rhythmic", kick, candidate, song=song)
    if outcome == "question":
        assert type(question) is JevQuestion
    else:
        assert type(question) is UnavailableQuestion and question.code == f"{side_name}_tempo_unreliable"
        assert (side_name, "tempo", "below_reliability_threshold") in withheld_triples(question)


def test_missing_required_evidence_lists_every_unusable_fact_in_documented_order():
    kick, candidate, song = full_pair()
    kick = with_measurement(with_measurement(with_measurement(kick, "attack", None), "decay", None),
                            "transient_strength", None)
    candidate = with_measurement(candidate, "transient_position", None)
    question = build_question("transient", kick, candidate, song=song)
    assert type(question) is UnavailableQuestion and question.code == "kick_attack_unknown"
    assert withheld_triples(question)[:4] == [
        ("kick", "attack", "unknown"), ("kick", "decay", "unknown"),
        ("kick", "transient_strength", "unknown"), ("candidate", "transient_position", "unknown")]
    assert all(item.reason in WITHHELD_REASONS for item in question.withheld)
    assert expected_code("kick", "attack", "unknown") == "kick_attack_unknown"


def test_missing_song_context_is_dimension_specific():
    kick, candidate, _ = full_pair()
    for dimension in DIMENSIONS:
        question = build_question(dimension, kick, candidate, song=None)
        if dimension == "rhythmic":
            assert type(question) is UnavailableQuestion
            assert question.code == "song_context_absent"
        else:
            assert type(question) is JevQuestion
        not_supplied = {(item.side, item.name, item.reason) for item in question.withheld}
        for side, name in OPTIONAL_EVIDENCE[dimension]:
            if side == "song":
                assert (side, name, "not_supplied") in not_supplied


def test_fixture_cases_include_accepted_questions_asked_without_song_context():
    accepted = [case for case in QUESTION_CASES
                if case["expects"]["outcome"] == "question" and case["song"] is None]
    needs_song = {dimension for dimension in DIMENSIONS
                  if any(side == "song" for side, _ in REQUIRED_EVIDENCE[dimension])}
    assert needs_song == {"rhythmic"}
    assert set(DIMENSIONS) - needs_song <= {case["dimension"] for case in accepted}
    for case in accepted:
        question = build(case)
        assert type(question) is JevQuestion and question.dimension == case["dimension"]
        absent = {(item.side, item.name, item.reason) for item in question.withheld}
        for side, name in OPTIONAL_EVIDENCE[case["dimension"]]:
            if side == "song":
                assert (side, name, "not_supplied") in absent
    # rhythmic legitimately cannot be asked without song context: it is not asked
    rhythmic = CASE_BY_NAME["rhythmic_song_context_absent"]
    assert rhythmic["song"] is None and rhythmic["expects"]["outcome"] == "unavailable"
    assert rhythmic["expects"]["code"] == "song_context_absent"


def test_a_question_requires_usable_keys_and_never_falls_back_to_a_fundamental():
    kick, candidate, song = full_pair()
    keyless = with_key(candidate, None, None, None)
    question = build_question("tonal", kick, with_measurement(keyless, "fundamental", 110.0, 0.90),
                              song=song)
    assert type(question) is UnavailableQuestion and question.code == "candidate_key_unknown"


def test_identical_inputs_give_equal_questions_ids_and_payloads():
    kick, candidate, song = full_pair()
    before = [item.to_json() for item in (kick, candidate, song)]
    first = build_question("frequency", kick, candidate, song=song)
    second = build_question("frequency", kick, candidate, song=song)
    assert first == second and first.question_id == second.question_id
    assert first.to_json() == second.to_json()
    assert json.loads(first.to_json()) == first.to_dict()
    assert list(json.loads(first.to_json())) == sorted(json.loads(first.to_json()))
    assert [item.to_json() for item in (kick, candidate, song)] == before
    assert not (kick.sample_id in first.to_json() or candidate.sample_id in first.to_json())
    with pytest.raises(FrozenInstanceError):
        first.evidence = ()
    with pytest.raises(FrozenInstanceError):
        first.evidence[0].value = 0
    unavailable = build_question("rhythmic", kick, candidate, song=None)
    with pytest.raises(FrozenInstanceError):
        unavailable.code = "other"


def test_fact_supply_order_does_not_change_the_question():
    kick, candidate, song = full_pair()
    reversed_kick = replace(kick, features=replace(kick.features,
                                                   measurements=tuple(reversed(kick.features.measurements))))
    assert build_question("frequency", reversed_kick, candidate, song=song) == \
        build_question("frequency", kick, candidate, song=song)
    assert build_question("frequency", reversed_kick, candidate, song=song).question_id == \
        build_question("frequency", kick, candidate, song=song).question_id


def test_a_changed_fact_or_prompt_version_changes_the_question_id(monkeypatch):
    kick, candidate, song = full_pair()
    base = build_question("transient", kick, candidate, song=song)
    changed = build_question("transient", with_measurement(kick, "attack", 13.0), candidate, song=song)
    assert changed.question_id != base.question_id and changed.evidence != base.evidence
    monkeypatch.setattr(questions_module, "PROMPT_VERSION", "jev-questions-v2")
    versioned = build_question("transient", kick, candidate, song=song)
    assert versioned.prompt_version == "jev-questions-v2"
    assert versioned.question_id != base.question_id


def test_two_candidates_with_identical_facts_share_a_question_and_id():
    kick, candidate, song = full_pair()
    twin = replace(candidate, sample_id="bass-002")
    other_kick = replace(kick, sample_id="kick-002")
    first = build_question("frequency", kick, candidate, song=song)
    assert build_question("frequency", kick, twin, song=song) == first
    assert build_question("frequency", other_kick, candidate, song=song) == first


def test_presented_values_are_the_declared_values():
    case = CASE_BY_NAME["full_frequency"]
    question = build(case)
    declared = {(side, item["name"]): item["value"] for side in ("kick", "candidate")
                for item in case[side]["measurements"] if "value" in item}
    for fact in question.evidence:
        if fact.name in MEASURES:
            assert fact.value == declared[(fact.side, fact.name)]
    tonal = build(CASE_BY_NAME["full_tonal"])
    kick_key = next(fact for fact in tonal.evidence if fact.side == "kick" and fact.name == "key")
    song_key = next(fact for fact in tonal.evidence if fact.side == "song" and fact.name == "key")
    assert (kick_key.value, song_key.value) == ("C major", "F major")


def test_payload_carries_no_path_file_name_sample_id_frame_count_or_audio():
    question = build(CASE_BY_NAME["frequency_payload_carries_no_sample_identity"])
    assert type(question) is JevQuestion
    payload = question.to_json()
    for forbidden in (PROBE_PATH, PROBE_PATH.rsplit("\\", 1)[0], "silent-monolith-9f8a",
                      "zzz-distinctive-9f8a", "zeta-distinctive-9f8a-id", "13579", ".wav",
                      "local_path", "frame_count", "sample_id", "C:", "\\\\"):
        assert forbidden not in payload
    assert re.fullmatch(r"q-[0-9a-f]{64}", question.question_id)
    mapping = question.to_dict()
    assert set(mapping) == {"question_id", "dimension", "prompt_version", "instruction", "evidence",
                            "withheld"}
    for fact in mapping["evidence"]:
        assert set(fact) == {"side", "name", "value", "unit"}
        assert type(fact["value"]) in (int, float, str)
    for item in mapping["withheld"]:
        assert set(item) == {"side", "name", "reason"}


def test_both_modules_are_local_audio_free_and_do_not_import_the_transport():
    allowed = ("from __future__ import", "import hashlib", "import json", "import math",
               "from collections.abc import", "from dataclasses import", "from typing import",
               "from backend.contracts import", "from backend.palette.compatibility import",
               "from backend.intelligence.questions import")
    for path in (QUESTIONS_FILE, DECISIONS_FILE):
        source = path.read_text(encoding="utf-8")
        for forbidden in ("local_path", "socket", "requests", "urllib", "http://", "https://",
                          "open(", "soundfile", "pathlib", "subprocess", "import os",
                          "backend.intelligence.jev", "backend.audio", "backend.evaluation"):
            assert forbidden not in source, (path.name, forbidden)
        imports = [line.strip() for line in source.splitlines() if line.startswith(("import ", "from "))]
        assert imports
        for line in imports:
            assert line.startswith(allowed), (path.name, line)
    questions = QUESTIONS_FILE.read_text(encoding="utf-8")
    assert "backend.intelligence.questions" not in questions
    assert "CONFIDENCE_THRESHOLD" in questions and "0.80" not in questions
    assert "idempot" not in questions and "cache" not in questions
    assert build_question.__doc__ and "QuestionInputError" in build_question.__doc__


def test_shared_contract_vocabulary_and_threshold_are_reused_not_forked():
    import backend.contracts as contracts
    import backend.palette.compatibility as compatibility
    assert questions_module.MEASURES is contracts.MEASURES
    assert questions_module.Dimension is contracts.Dimension
    assert CONTEXT_NAMES == ("key", "role", "genre")
    assert questions_module.CONFIDENCE_THRESHOLD == compatibility.CONFIDENCE_THRESHOLD == 0.80


def table_rows(text, heading):
    lines = text.splitlines()
    rows = []
    for line in lines[lines.index(heading) + 1:]:
        if line.startswith("## "):
            break
        if line.startswith("|") and not set(line) <= set("|- "):
            rows.append([cell.strip() for cell in line.strip().strip("|").split("|")])
    return rows


def test_document_records_the_version_instructions_evidence_and_code_table():
    text = DOCUMENT.read_text(encoding="utf-8")
    assert PROMPT_VERSION in text
    for dimension in DIMENSIONS:
        assert TICK + dimension + TICK in text
        assert INSTRUCTIONS[dimension] in text
    documented = {}
    for row in table_rows(text, "## Evidence by dimension"):
        if len(row) != 4 or row[2] not in ("required", "optional"):
            continue
        dimension, side, kind, fact = row
        documented.setdefault((dimension.strip(TICK), side.strip(TICK), kind), []).append(fact.strip(TICK))
    for kind, mapping in (("required", REQUIRED_EVIDENCE), ("optional", OPTIONAL_EVIDENCE)):
        for dimension in DIMENSIONS:
            for side in ("kick", "candidate", "song"):
                expected = [name for entry_side, name in mapping[dimension] if entry_side == side]
                if expected:
                    assert documented[(dimension, side, kind)] == expected, (dimension, side, kind)
    assert {row[0].strip(TICK) for row in table_rows(text, "## Unavailable codes")
            if row[0].startswith(TICK)} == set(QUESTION_UNAVAILABLE_CODES)
    for code in QUESTION_UNAVAILABLE_CODES:
        assert TICK + code + TICK in text
    for code in INPUT_ERROR_CODES:
        assert TICK + code + TICK in text
    for name in ("unknown", "below_reliability_threshold", "not_supplied", "song_context_absent",
                 "model_abstained", "CONFIDENCE_THRESHOLD", "0.80", "1e-6"):
        assert name in text
    assert "not a reliability claim" in text
    assert "#15" in text and "#63" in text and "#46" in text and "#14" in text
