"""Synthetic hybrid-ranking fixtures; no audio, Jev transport, file or network use.

Every case in tests/fixtures/hybrid/hybrid-cases.json declares the kick and candidate
DSP values, the Jev evidence and, under expects, the numbers the case must produce. The
expected numbers are derived by hand from the mapping documented in
_docs/hybrid-ranking.md and never read back from the implementation: the DSP values come
from the #12 mapping, the Jev values from the label map, and the combined values from the
documented source shares and coverage renormalization.

Fixture shape:

  kick                 {bands: {sub, bass, low_mid, mid, high_mid, high}, attack, decay,
                        strength, position, tonic, mode, key_confidence, fundamental,
                        fundamental_confidence}
  candidates[]         {id, role, bands, position, tonic, mode, key_confidence, fundamental,
                        fundamental_confidence, spectral_centroid, spectral_rolloff,
                        analysis_version}
  evidence{id: []}     {dimension, label, confidence} | {dimension, abstain, model_version}
                       | {dimension, unavailable_reason}
  policy               null or the five HybridWeightTable weights with an identifier
  expects              mode, jev_status, order, unscored, alternatives, exact_tie,
                       baseline_fallback, compare_with, weight_table_id and, per candidate,
                       rank, compatibility, confidence, uncertain, warnings, jev_judgments
                       and per-dimension weight, dsp, jev, combined, unavailable_reason,
                       dsp_unavailable_reason, jev_label, jev_score, jev_confidence,
                       jev_unavailable_reason, jev_model_version

An omitted band is 0.0; an omitted measurement, key or version is an explicit unknown with
the documented reason. Floats are compared with an absolute tolerance of 1e-12, because the
documented formulas are float arithmetic; equalities the criteria call exact are asserted
exactly.
"""

import ast
import json
from dataclasses import FrozenInstanceError
from fractions import Fraction
from pathlib import Path
import re
from typing import get_args

import pytest

from backend.contracts import (AudioFeatures, AudioMetadata, Dimension, JevJudgment, Label,
                               LabelProbability, MEASURES, Measurement, MusicalKey, PaletteContext,
                               RankedCandidate, RecommendationBatch, Sample, SongContext)
from backend.palette.compatibility import CONFIDENCE_THRESHOLD
from backend.palette.ranking import (ALTERNATIVES_LIMIT, CANDIDATE_NO_JEV_EVIDENCE,
                                     DEFAULT_HYBRID_WEIGHT_TABLE, DEFAULT_WEIGHT_TABLE,
                                     DEFAULT_WEIGHT_TABLE_ID, DIMENSION_DISAGREEMENT,
                                     DISAGREEMENT_DELTA, HYBRID_CODES, HYBRID_DIMENSIONS,
                                     HYBRID_INPUT_ERROR_CODES, HYBRID_RANKING_VERSION,
                                     HYBRID_STATUS_CODES, HYBRID_WEIGHTED_DIMENSIONS, HYBRID_WEIGHTS,
                                     HYBRID_WEIGHT_TABLE_ID, JEV_ABSENT, JEV_ABSTAINED,
                                     JEV_CONFIDENCE_THRESHOLD, JEV_EVIDENCE_UNAVAILABLE,
                                     JEV_LABEL_SCORES, JEV_PARTIAL, JEV_PRESENT, LOW_CONFIDENCE_THRESHOLD,
                                     LOW_COVERAGE, LOW_JEV_CONFIDENCE, MODE_DSP_ONLY, MODE_HYBRID,
                                     NO_EVIDENCE, RANKING_VERSION, RankingInputError, RankingPolicy,
                                     SOURCE_SHARE, UNWEIGHTED_DIMENSION, UNWEIGHTED_DIMENSIONS,
                                     WEIGHT_SUM_TOLERANCE, HybridEvidence, HybridPolicy,
                                     HybridWeightTable, WeightTable, rank_candidates, rank_hybrid)

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "hybrid"
MODULE = ROOT / "backend" / "palette" / "ranking.py"
DOCUMENT = ROOT / "_docs" / "hybrid-ranking.md"
CASES = json.loads((FIXTURES / "hybrid-cases.json").read_text(encoding="utf-8"))
CASE_BY_NAME = {case["name"]: case for case in CASES}

RATE, FRAMES = 48000, 24000
ANALYSIS = "hybrid-fixture-1"
TOLERANCE = 1e-12
TICK = chr(96)
SHORT_BANDS = {"sub": "band_sub", "bass": "band_bass", "low_mid": "band_low_mid",
               "mid": "band_mid", "high_mid": "band_high_mid", "high": "band_high"}
PROMPT_VERSION = "jev-questions-v1"
DEFAULT_PROBABILITIES = {
    "very-poor": {"very-poor": 0.60, "poor": 0.20, "neutral": 0.10, "good": 0.05, "excellent": 0.05},
    "poor": {"very-poor": 0.20, "poor": 0.60, "neutral": 0.10, "good": 0.05, "excellent": 0.05},
    "neutral": {"very-poor": 0.05, "poor": 0.10, "neutral": 0.70, "good": 0.10, "excellent": 0.05},
    "good": {"very-poor": 0.05, "poor": 0.05, "neutral": 0.10, "good": 0.60, "excellent": 0.20},
    "excellent": {"very-poor": 0.05, "poor": 0.05, "neutral": 0.05, "good": 0.25, "excellent": 0.60},
}
DIMENSION_FIELDS = {"weight": "weight", "dsp": "dsp_compatibility", "jev": "jev_score",
                    "combined": "compatibility", "unavailable_reason": "unavailable_reason",
                    "dsp_unavailable_reason": "dsp_unavailable_reason", "jev_label": "jev_label",
                    "jev_score": "jev_score", "jev_confidence": "jev_confidence",
                    "jev_unavailable_reason": "jev_unavailable_reason",
                    "jev_model_version": "jev_model_version", "reason": "reason"}
FLOAT_FIELDS = frozenset({"weight", "dsp", "jev", "combined", "jev_score", "jev_confidence"})


def measure(name, value=None, confidence=None):
    if value is None:
        return Measurement(name=name, value=None, unit=MEASURES[name][0], confidence=None,
                           unavailable_reason="not_implemented")
    return Measurement(name=name, value=value, unit=MEASURES[name][0], confidence=confidence)


def musical_key(spec):
    if spec.get("tonic") is None:
        return MusicalKey(tonic=None, mode=None, confidence=None,
                          unavailable_reason="insufficient_key_context")
    return MusicalKey(tonic=spec["tonic"], mode=spec.get("mode", "major"),
                      confidence=spec.get("key_confidence", 0.90))


def sample(spec, default_role="bass", default_id="sample-001"):
    """Build one contract Sample from a declared fixture record."""
    sample_id = spec.get("id", default_id)
    values = {name: measure(name) for name in MEASURES}
    values["peak"] = measure("peak", 0.8)
    values["rms"] = measure("rms", 0.2)
    for short, name in SHORT_BANDS.items():
        values[name] = measure(name, spec.get("bands", {}).get(short, 0.0))
    for name, measured in (("attack", "attack"), ("decay", "decay"),
                           ("strength", "transient_strength"), ("position", "transient_position")):
        if name in spec:
            values[measured] = measure(measured, spec[name])
    if "fundamental" in spec:
        values["fundamental"] = measure("fundamental", spec["fundamental"],
                                        spec.get("fundamental_confidence"))
    for name in ("spectral_centroid", "spectral_rolloff"):
        if name in spec:
            values[name] = measure(name, spec[name])
    return Sample(sample_id=sample_id, role=spec.get("role", default_role),
                  audio=AudioMetadata(local_path="C:/synthetic-hybrid/" + sample_id + ".wav",
                                      sample_rate_hz=RATE, channels=1, frame_count=FRAMES,
                                      duration_ms=FRAMES * 1000 / RATE),
                  features=AudioFeatures(measurements=tuple(values.values()), key=musical_key(spec)),
                  analysis_version=spec.get("analysis_version", ANALYSIS))


def judgment_of(entry, dimension):
    """The contract judgment one fixture evidence entry declares."""
    if entry.get("abstain"):
        return JevJudgment(dimension=dimension, label=None, confidence=None, probabilities=(),
                           model_version=entry.get("model_version", "hybrid-abstain-1"),
                           prompt_version=entry.get("prompt_version", PROMPT_VERSION),
                           unavailable_reason="model_abstained")
    label = entry["label"]
    probabilities = entry.get("probabilities", DEFAULT_PROBABILITIES[label])
    return JevJudgment(dimension=dimension, label=label, confidence=entry.get("confidence", 0.90),
                       probabilities=tuple(LabelProbability(label=name, probability=value)
                                           for name, value in probabilities.items()),
                       model_version=entry.get("model_version", "hybrid-fixture-model-1"),
                       prompt_version=entry.get("prompt_version", PROMPT_VERSION))


def evidence_of(case):
    """The HybridEvidence mapping one fixture case declares."""
    evidence = {}
    for candidate_id, entries in case.get("evidence", {}).items():
        built = []
        for entry in entries:
            dimension = entry["dimension"]
            if "label" in entry or entry.get("abstain"):
                built.append(HybridEvidence(dimension, judgment=judgment_of(entry, dimension)))
            else:
                built.append(HybridEvidence(dimension, unavailable_reason=entry["unavailable_reason"]))
        evidence[candidate_id] = tuple(built)
    return evidence


def policy_of(case):
    if case.get("policy") is None:
        return HybridPolicy()
    return HybridPolicy(HybridWeightTable(**case["policy"]))


def run_case(case):
    """The declared inputs, the #12 baseline and the hybrid result of one fixture case."""
    kick = sample(case["kick"], "kick", "kick-001")
    candidates = [sample(spec) for spec in case["candidates"]]
    baseline = rank_candidates(kick, candidates, policy=RankingPolicy())
    result = rank_hybrid(baseline, evidence_of(case), policy=policy_of(case))
    return kick, candidates, baseline, result


def records_by_id(result):
    found = {record.candidate_id: record for record in result.ranked}
    found.update({record.candidate_id: record for record in result.unscored})
    return found


def dimensions_by_name(record):
    return {entry.dimension: entry for entry in record.hybrid_dimensions}


def warning_codes(warnings):
    """The stable leading codes of the hybrid warning strings, in order."""
    codes = []
    for item in warnings:
        code = item.split(":", 1)[0]
        if code in HYBRID_CODES:
            codes.append(code)
    return tuple(codes)


def check_dimension(candidate_id, dimension, entry, expected):
    for key, wanted in expected.items():
        actual = getattr(entry, DIMENSION_FIELDS[key])
        where = candidate_id + "." + dimension + "." + key
        if key in FLOAT_FIELDS and wanted is not None:
            assert actual == pytest.approx(wanted, abs=TOLERANCE), where
        else:
            assert actual == wanted, where


def check_expects(case, result):
    """Assert everything one fixture case declares; nothing is read back from the run."""
    expects = case["expects"]
    if expects["mode"] == MODE_DSP_ONLY:
        assert result.ranking_version == RANKING_VERSION
        assert result.weight_table_id == DEFAULT_WEIGHT_TABLE_ID
    else:
        assert result.ranking_version == HYBRID_RANKING_VERSION
        assert result.weight_table_id == expects.get("weight_table_id", HYBRID_WEIGHT_TABLE_ID)
    assert [record.candidate_id for record in result.ranked] == expects["order"]
    assert [record.candidate_id for record in result.unscored] == expects.get("unscored", [])
    assert list(result.alternatives) == expects.get("alternatives", [])
    assert result.mode == expects["mode"]
    assert result.jev_status == expects["jev_status"]
    if expects.get("exact_tie"):
        assert len({record.compatibility for record in result.ranked}) == 1
    found = records_by_id(result)
    for candidate_id, expected in expects.get("candidates", {}).items():
        record = found[candidate_id]
        for key in ("rank", "compatibility", "confidence", "uncertain"):
            if key not in expected:
                continue
            if key in ("compatibility", "confidence"):
                assert getattr(record, key) == pytest.approx(expected[key], abs=TOLERANCE)
            else:
                assert getattr(record, key) == expected[key]
        if "warnings" in expected:
            assert list(warning_codes(record.warnings)) == expected["warnings"]
        if "jev_judgments" in expected:
            assert [item.dimension for item in record.jev_judgments] == expected["jev_judgments"]
        for dimension, wanted in expected.get("dimensions", {}).items():
            check_dimension(candidate_id, dimension, dimensions_by_name(record)[dimension], wanted)


@pytest.mark.parametrize("case", CASES, ids=[case["name"] for case in CASES])
def test_fixture_case_matches_the_documented_mapping(case):
    _, _, _, result = run_case(case)
    check_expects(case, result)
    partner = case["expects"].get("compare_with")
    assert partner is None or partner in CASE_BY_NAME
    for record in list(result.ranked) + list(result.unscored):
        assert [entry.dimension for entry in record.hybrid_dimensions] == list(HYBRID_DIMENSIONS)


def test_every_fixture_case_declares_its_own_expectations():
    assert CASES and len(CASES) == len(CASE_BY_NAME)
    assert set(CASE_BY_NAME) >= {
        "pair_frequency", "pair_transient", "pair_tonal", "pair_texture", "pair_arrangement",
        "dimension_states", "abstention", "transport_states", "mixed_batch",
        "total_unavailability", "total_unavailability_with_codes", "rhythmic_absent",
        "rhythmic_present", "tie_exact_equal", "tie_dsp_only_vs_jev_only",
        "custom_weight_table", "all_judged_certain", "alternatives_rank1_uncertain",
        "alternatives_fewer_than_four", "alternatives_none_ranked", "confidence_low",
        "confidence_high", "disagreement_frequency", "empty_baseline",
        "single_candidate_no_evidence", "single_jev_dimension"}
    for case in CASES:
        assert case["note"].strip() and "expects" in case and "order" in case["expects"]
        assert case["expects"]["mode"] in HYBRID_STATUS_CODES
        assert case["expects"]["jev_status"] in HYBRID_STATUS_CODES


def test_dsp_only_fallback_reproduces_the_baseline_field_for_field():
    """The fallback equality is asserted against the #12 entry point at runtime."""
    fallbacks = [case for case in CASES if case["expects"].get("baseline_fallback")]
    assert len(fallbacks) >= 5
    for case in fallbacks:
        _, _, baseline, result = run_case(case)
        assert result.mode == MODE_DSP_ONLY and result.jev_status == JEV_ABSENT
        assert len(result.ranked) == len(baseline.ranked)
        assert len(result.unscored) == len(baseline.unscored)
        for hybrid, ranked in zip(result.ranked, baseline.ranked):
            assert hybrid.candidate_id == ranked.candidate_id
            assert hybrid.rank == ranked.rank
            assert hybrid.compatibility == ranked.compatibility
            assert hybrid.confidence == ranked.confidence
            assert hybrid.dsp_dimensions == ranked.dsp_dimensions
            assert hybrid.reasons == ranked.reasons
            assert hybrid.warnings == ranked.warnings
            assert hybrid.jev_judgments == ()
        for hybrid, unscored in zip(result.unscored, baseline.unscored):
            assert hybrid.candidate_id == unscored.candidate_id
            assert hybrid.code == unscored.code
            assert hybrid.dsp_dimensions == unscored.dsp_dimensions
            assert hybrid.reasons == unscored.reasons


def test_confidence_never_changes_a_score_a_rank_or_an_alternative():
    low = CASE_BY_NAME["confidence_low"]
    high = CASE_BY_NAME["confidence_high"]
    assert low["expects"]["compare_with"] == high["name"]
    assert high["expects"]["compare_with"] == low["name"]
    _, _, _, low_result = run_case(low)
    _, _, _, high_result = run_case(high)
    assert [(record.rank, record.candidate_id, record.compatibility) for record in low_result.ranked] == \
           [(record.rank, record.candidate_id, record.compatibility) for record in high_result.ranked]
    assert low_result.alternatives == high_result.alternatives
    assert [record.confidence for record in low_result.ranked] == \
           [record.confidence for record in high_result.ranked]
    low_entries = dimensions_by_name(low_result.ranked[0])
    high_entries = dimensions_by_name(high_result.ranked[0])
    for dimension in HYBRID_DIMENSIONS:
        assert low_entries[dimension].compatibility == high_entries[dimension].compatibility
        assert low_entries[dimension].jev_score == high_entries[dimension].jev_score
    assert low_entries["frequency"].jev_confidence == 0.0
    assert high_entries["frequency"].jev_confidence == 1.0
    assert warning_codes(low_result.ranked[0].warnings) == (LOW_JEV_CONFIDENCE,)
    assert warning_codes(high_result.ranked[0].warnings) == ()


def test_rhythmic_judgment_is_carried_unweighted_and_changes_nothing():
    absent = CASE_BY_NAME["rhythmic_absent"]
    present = CASE_BY_NAME["rhythmic_present"]
    assert absent["expects"]["compare_with"] == present["name"]
    _, _, _, without = run_case(absent)
    _, _, _, with_rhythmic = run_case(present)
    assert without.mode == with_rhythmic.mode == MODE_DSP_ONLY
    assert without.jev_status == with_rhythmic.jev_status == JEV_ABSENT
    assert [(record.candidate_id, record.rank, record.compatibility, record.confidence)
            for record in without.ranked] == \
           [(record.candidate_id, record.rank, record.compatibility, record.confidence)
            for record in with_rhythmic.ranked]
    assert without.alternatives == with_rhythmic.alternatives
    entry = dimensions_by_name(with_rhythmic.ranked[0])["rhythmic"]
    assert entry.weight == 0.0
    assert entry.unavailable_reason == UNWEIGHTED_DIMENSION != NO_EVIDENCE
    assert entry.jev_label == "poor" and entry.jev_score is None
    assert entry.compatibility is None and entry.jev_confidence == 0.9
    assert dimensions_by_name(without.ranked[0])["rhythmic"].unavailable_reason == NO_EVIDENCE
    assert "rhythmic" not in HYBRID_WEIGHTED_DIMENSIONS
    assert UNWEIGHTED_DIMENSIONS == ("rhythmic",)


def test_alternatives_are_bounded_unique_and_taken_after_rank_one():
    for case in CASES:
        _, _, _, result = run_case(case)
        ranked_ids = [record.candidate_id for record in result.ranked]
        assert len(result.alternatives) <= ALTERNATIVES_LIMIT
        assert len(set(result.alternatives)) == len(result.alternatives)
        assert set(result.alternatives) <= set(ranked_ids)
        uncertain = any(record.uncertain for record in result.ranked)
        expected = ranked_ids[1:1 + ALTERNATIVES_LIMIT] if uncertain else []
        assert list(result.alternatives) == expected


def test_every_warning_is_a_hybrid_code_and_every_reason_is_clean():
    for case in CASES:
        _, _, _, result = run_case(case)
        for record in list(result.ranked) + list(result.unscored):
            for warning in record.warnings:
                assert warning.split(":", 1)[0] in HYBRID_CODES, warning
                assert warning.strip() == warning
            for entry in record.hybrid_dimensions:
                assert entry.reason.strip() and "None" not in entry.reason
                assert "nan" not in entry.reason.lower() and "inf" not in entry.reason.lower()
                for forbidden in ("C:", chr(92), ".wav", "synthetic-hybrid"):
                    assert forbidden not in entry.reason
            for reason in record.reasons:
                assert reason.strip()


def test_uncertain_matches_the_documented_thresholds():
    for case in CASES:
        _, _, _, result = run_case(case)
        for record in result.ranked:
            by_threshold = record.confidence < LOW_CONFIDENCE_THRESHOLD or any(
                entry.jev_score is not None and entry.jev_confidence < JEV_CONFIDENCE_THRESHOLD
                for entry in record.hybrid_dimensions)
            assert record.uncertain == by_threshold
            if result.mode != MODE_HYBRID:
                continue
            if record.confidence < LOW_CONFIDENCE_THRESHOLD:
                assert LOW_COVERAGE in warning_codes(record.warnings)
            if not any(entry.jev_score is not None for entry in record.hybrid_dimensions
                       if entry.weight > 0):
                assert CANDIDATE_NO_JEV_EVIDENCE in warning_codes(record.warnings)
            for entry in record.hybrid_dimensions:
                if entry.jev_score is not None and entry.jev_confidence < JEV_CONFIDENCE_THRESHOLD:
                    assert LOW_JEV_CONFIDENCE in warning_codes(record.warnings)


def test_input_order_and_mapping_permutation_invariance_and_no_mutation():
    case = CASE_BY_NAME["all_judged_certain"]
    kick, candidates, baseline, first = run_case(case)
    evidence = evidence_of(case)
    before = [item.to_json() for item in (kick, *candidates)]
    reversed_evidence = {key: tuple(reversed(value)) for key, value in reversed(list(evidence.items()))}
    second = rank_hybrid(baseline, reversed_evidence, policy=policy_of(case))
    assert first == second
    assert [item.to_json() for item in (kick, *candidates)] == before
    reshuffled = rank_candidates(kick, list(reversed(candidates)), policy=RankingPolicy())
    third = rank_hybrid(reshuffled, evidence, policy=policy_of(case))
    assert [(record.candidate_id, record.compatibility, record.confidence, record.warnings)
            for record in third.ranked] == \
           [(record.candidate_id, record.compatibility, record.confidence, record.warnings)
            for record in first.ranked]
    with pytest.raises(FrozenInstanceError):
        first.ranked = ()
    with pytest.raises(FrozenInstanceError):
        first.ranked[0].uncertain = True


def test_every_result_is_finite_and_within_range():
    for case in CASES:
        _, _, _, result = run_case(case)
        for record in result.ranked:
            assert 0 <= record.compatibility <= 1 and 0 <= record.confidence <= 1
            for entry in record.hybrid_dimensions:
                assert entry.compatibility is None or 0 <= entry.compatibility <= 1
                assert entry.jev_score is None or entry.jev_score in tuple(JEV_LABEL_SCORES.values())
                assert (entry.compatibility is None) == (entry.unavailable_reason is not None)


def test_jev_judgments_carry_their_own_versions_labels_and_probabilities():
    case = CASE_BY_NAME["pair_frequency"]
    _, _, _, result = run_case(case)
    record = result.ranked[0]
    assert [item.dimension for item in record.jev_judgments] == ["frequency"]
    judgment = record.jev_judgments[0]
    assert judgment.label == "excellent"
    assert judgment.model_version == "hybrid-fixture-model-1"
    assert judgment.prompt_version == PROMPT_VERSION
    entry = dimensions_by_name(record)["frequency"]
    assert entry.jev_label == judgment.label
    assert entry.jev_confidence == judgment.confidence
    assert entry.jev_probabilities == tuple(judgment.probabilities)
    assert entry.jev_model_version == judgment.model_version
    assert entry.jev_prompt_version == judgment.prompt_version
    assert entry.jev_score == JEV_LABEL_SCORES[judgment.label] == 1.0
    assert "jev excellent 1.000" in entry.reason


def test_module_imports_only_the_standard_library_contracts_and_compatibility():
    source = MODULE.read_text(encoding="utf-8")
    imported = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module)
    assert imported == {"collections.abc", "dataclasses", "math", "typing", "backend.contracts",
                        "backend.palette.compatibility"}
    assert not any(name.startswith("backend.intelligence") for name in imported)


def test_module_opens_no_file_and_touches_no_network_clock_or_randomness():
    source = MODULE.read_text(encoding="utf-8")
    assert "local_path" not in source
    assert re.search(r"\b(open|eval|exec|compile|__import__)\s*\(", source) is None
    assert re.search(r"\b(urllib|socket|requests|subprocess|pathlib|shutil|tempfile|hashlib|secrets"
                     r"|random|time|os|sys)\b\s*[.(]", source) is None
    assert "backend.intelligence" not in source and "backend.audio" not in source
    assert "_docs/hybrid-ranking.md" in source and "_docs/dsp-baseline.md" in source
    assert rank_hybrid.__doc__ and "_docs/hybrid-ranking.md" in rank_hybrid.__doc__
    assert rank_candidates.__doc__ and "_docs/dsp-baseline.md" in rank_candidates.__doc__


def test_documented_weight_tables_and_records_match_the_code():
    assert HYBRID_RANKING_VERSION == "hybrid-ranking-v1"
    assert HYBRID_WEIGHT_TABLE_ID == "hybrid-weights-1"
    assert HYBRID_WEIGHTS == {"frequency": 0.30, "transient": 0.20, "tonal": 0.20,
                              "texture": 0.10, "arrangement": 0.20}
    assert abs(sum(HYBRID_WEIGHTS.values()) - 1) <= WEIGHT_SUM_TOLERANCE
    for dimension, weight in HYBRID_WEIGHTS.items():
        assert DEFAULT_HYBRID_WEIGHT_TABLE.weight(dimension) == weight
    assert DEFAULT_HYBRID_WEIGHT_TABLE.identifier == HYBRID_WEIGHT_TABLE_ID
    assert DEFAULT_HYBRID_WEIGHT_TABLE.weight("rhythmic") == 0.0
    assert UNWEIGHTED_DIMENSIONS == ("rhythmic",)
    assert SOURCE_SHARE == 0.5
    assert LOW_CONFIDENCE_THRESHOLD == 0.60
    assert DISAGREEMENT_DELTA == 0.50
    assert ALTERNATIVES_LIMIT == 3
    assert JEV_CONFIDENCE_THRESHOLD is CONFIDENCE_THRESHOLD and JEV_CONFIDENCE_THRESHOLD == 0.80
    assert tuple(JEV_LABEL_SCORES) == get_args(Label)
    assert JEV_LABEL_SCORES == {"very-poor": 0.0, "poor": 0.25, "neutral": 0.5, "good": 0.75,
                                "excellent": 1.0}
    assert HYBRID_DIMENSIONS == get_args(Dimension)
    assert HYBRID_WEIGHTED_DIMENSIONS == ("frequency", "transient", "tonal", "texture", "arrangement")
    assert set(HYBRID_WEIGHTED_DIMENSIONS) | set(UNWEIGHTED_DIMENSIONS) == set(HYBRID_DIMENSIONS)
    assert type(DEFAULT_WEIGHT_TABLE) is WeightTable
    assert RANKING_VERSION == "dsp-baseline-v1"
    assert DEFAULT_WEIGHT_TABLE_ID == "dsp-baseline-weights-1"
    assert HYBRID_STATUS_CODES == ("hybrid", "dsp-only", "jev_present", "jev_partial", "jev_absent")
    assert HYBRID_INPUT_ERROR_CODES == ("invalid_policy", "invalid_baseline", "invalid_evidence",
                                        "duplicate_evidence", "unknown_candidate")
    assert HYBRID_CODES == (NO_EVIDENCE, UNWEIGHTED_DIMENSION, JEV_ABSTAINED, JEV_EVIDENCE_UNAVAILABLE,
                            DIMENSION_DISAGREEMENT, CANDIDATE_NO_JEV_EVIDENCE, LOW_COVERAGE,
                            LOW_JEV_CONFIDENCE)


def test_the_two_weight_tables_agree_exactly_on_the_shared_dimensions():
    """0.30/0.70 == 3/7 and 0.20/0.70 == 2/7 within 1e-9, from the two documented tables."""
    shared = 0.30 + 0.20 + 0.20
    assert abs(HYBRID_WEIGHTS["frequency"] / shared - Fraction(3, 7)) <= 1e-9
    assert abs(HYBRID_WEIGHTS["transient"] / shared - Fraction(2, 7)) <= 1e-9
    assert abs(HYBRID_WEIGHTS["tonal"] / shared - Fraction(2, 7)) <= 1e-9
    assert DEFAULT_WEIGHT_TABLE.frequency == 3 / 7
    assert DEFAULT_WEIGHT_TABLE.transient == 2 / 7
    assert DEFAULT_WEIGHT_TABLE.tonal == 2 / 7
    assert HYBRID_WEIGHTS["texture"] + HYBRID_WEIGHTS["arrangement"] == pytest.approx(
        HYBRID_WEIGHTS["frequency"], abs=1e-9)
    _, _, _, result = run_case(CASE_BY_NAME["mixed_batch"])
    record = records_by_id(result)["bass-without"]
    assert record.confidence == pytest.approx(0.70, abs=TOLERANCE)
    assert record.confidence != 1.0


@pytest.mark.parametrize("kwargs", [
    {"identifier": "", "frequency": 0.50, "transient": 0.20, "tonal": 0.10, "texture": 0.10,
     "arrangement": 0.10},
    {"identifier": "x", "frequency": 0.0, "transient": 0.25, "tonal": 0.25, "texture": 0.25,
     "arrangement": 0.25},
    {"identifier": "x", "frequency": -0.50, "transient": 0.50, "tonal": 0.50, "texture": 0.25,
     "arrangement": 0.25},
    {"identifier": "x", "frequency": 0.50, "transient": 0.20, "tonal": 0.10, "texture": 0.10,
     "arrangement": 0.20},
    {"identifier": "x", "frequency": float("nan"), "transient": 0.25, "tonal": 0.25, "texture": 0.25,
     "arrangement": 0.25},
    {"identifier": "x", "frequency": True, "transient": 0.25, "tonal": 0.25, "texture": 0.25,
     "arrangement": 0.25},
])
def test_invalid_hybrid_weight_tables_are_policy_errors(kwargs):
    with pytest.raises(RankingInputError) as error:
        HybridWeightTable(**kwargs)
    assert error.value.code == "invalid_policy"


def test_custom_weight_table_is_recorded_and_changes_the_scores():
    case = CASE_BY_NAME["custom_weight_table"]
    _, _, baseline, result = run_case(case)
    assert result.weight_table_id == "hybrid-weights-fixture-2"
    default = rank_hybrid(baseline, evidence_of(case), policy=HybridPolicy())
    assert default.weight_table_id == HYBRID_WEIGHT_TABLE_ID
    custom_scores = [record.compatibility for record in result.ranked]
    default_scores = [record.compatibility for record in default.ranked]
    assert custom_scores != default_scores
    assert custom_scores[0] == pytest.approx(0.9687500000000001, abs=TOLERANCE)
    assert default_scores[0] == pytest.approx(0.9785714285714288, abs=TOLERANCE)


def test_input_errors_name_the_first_problem_with_a_stable_code():
    case = CASE_BY_NAME["pair_frequency"]
    kick, _, baseline, _ = run_case(case)
    good = judgment_of({"dimension": "frequency", "label": "good", "confidence": 0.9}, "frequency")
    tampered = rank_candidates(kick, [sample(case["candidates"][0])], policy=RankingPolicy())
    object.__setattr__(tampered.ranked[0], "rank", 7)
    entries = [
        ("invalid_policy", baseline, {}, None, "invalid_policy"),
        ("invalid_policy_table", baseline, {}, object(), "invalid_policy"),
        ("invalid_baseline", None, {}, HybridPolicy(), "invalid_baseline"),
        ("invalid_baseline_rank", tampered, {}, HybridPolicy(), "invalid_baseline"),
        ("invalid_evidence_map", baseline, [], HybridPolicy(), "invalid_evidence"),
        ("invalid_evidence_entry", baseline, {"bass-complement": ("nope",)}, HybridPolicy(),
         "invalid_evidence"),
        ("invalid_evidence_both", baseline, {"bass-complement": (
            HybridEvidence("frequency", judgment=good, unavailable_reason="x"),)}, HybridPolicy(),
         "invalid_evidence"),
        ("invalid_evidence_neither", baseline, {"bass-complement": (HybridEvidence("frequency"),)},
         HybridPolicy(), "invalid_evidence"),
        ("invalid_evidence_dimension", baseline, {"bass-complement": (
            HybridEvidence("loudness", unavailable_reason="x"),)}, HybridPolicy(), "invalid_evidence"),
        ("invalid_evidence_mismatch", baseline, {"bass-complement": (
            HybridEvidence("tonal", judgment=good),)}, HybridPolicy(), "invalid_evidence"),
        ("invalid_evidence_blank_reason", baseline, {"bass-complement": (
            HybridEvidence("tonal", unavailable_reason="  "),)}, HybridPolicy(), "invalid_evidence"),
        ("duplicate_evidence", baseline, {"bass-complement": (
            HybridEvidence("tonal", unavailable_reason="a"),
            HybridEvidence("tonal", unavailable_reason="b"))}, HybridPolicy(), "duplicate_evidence"),
        ("unknown_candidate", baseline, {"bass-unknown": ()}, HybridPolicy(), "unknown_candidate"),
    ]
    for name, given, evidence, policy, code in entries:
        if policy is None:
            call = lambda b=given, e=evidence: rank_hybrid(b, e, policy=None)
        else:
            call = lambda b=given, e=evidence, p=policy: rank_hybrid(b, e, policy=p)
        with pytest.raises(RankingInputError) as error:
            call()
        assert error.value.code == code, name
        assert str(error.value).strip()


def test_tampered_judgments_and_unknown_dimension_literals_are_rejected():
    case = CASE_BY_NAME["pair_frequency"]
    _, _, baseline, _ = run_case(case)
    judgment = judgment_of({"dimension": "frequency", "label": "good", "confidence": 0.9}, "frequency")
    object.__setattr__(judgment, "label", "not-a-label")
    with pytest.raises(RankingInputError) as error:
        rank_hybrid(baseline, {"bass-complement": (HybridEvidence("frequency", judgment=judgment),)},
                    policy=HybridPolicy())
    assert error.value.code == "invalid_evidence"
    for literal in ("loudness", "Frequency", "", None, 7):
        with pytest.raises(RankingInputError) as error:
            rank_hybrid(baseline, {"bass-complement": (
                HybridEvidence(literal, unavailable_reason="x"),)}, policy=HybridPolicy())
        assert error.value.code == "invalid_evidence"


def test_empty_and_minimal_inputs_are_valid_and_finite():
    _, _, baseline, result = run_case(CASE_BY_NAME["empty_baseline"])
    assert result.ranked == () and result.unscored == () and result.alternatives == ()
    assert result.mode == MODE_DSP_ONLY and result.jev_status == JEV_ABSENT
    assert baseline.ranked == () and baseline.unscored == ()
    _, _, _, only = run_case(CASE_BY_NAME["single_jev_dimension"])
    assert len(only.ranked) == 1 and only.ranked[0].rank == 1
    assert only.ranked[0].confidence == pytest.approx(0.1, abs=TOLERANCE)
    assert only.ranked[0].uncertain and warning_codes(only.ranked[0].warnings) == (LOW_COVERAGE,)


def test_reasons_name_the_sources_and_the_result():
    _, _, _, result = run_case(CASE_BY_NAME["dimension_states"])
    record = records_by_id(result)["bass-b-both"]
    entry = dimensions_by_name(record)["frequency"]
    assert "dsp 0.900" in entry.reason and "jev good 0.750" in entry.reason
    assert "combined 0.825 at weight 0.300" in entry.reason
    assert "unavailable (no_evidence)" in dimensions_by_name(record)["texture"].reason
    unweighted = dimensions_by_name(records_by_id(result)["bass-a-judgment-only"])["rhythmic"]
    assert unweighted.reason.endswith("at weight 0.000") and "no_evidence" in unweighted.reason


def test_schema_one_batches_round_trip_for_both_modes():
    def batch_for(case, mode):
        kick, candidates, _, result = run_case(case)
        palette = PaletteContext(palette_id="palette-001", revision=3, kick_id=kick.sample_id,
                                 selected_bass_id=None,
                                 song=SongContext(tempo=measure("tempo"), key=musical_key({}),
                                                  genre=None, genre_unavailable_reason="not_provided"))
        results = tuple(RankedCandidate(
            candidate_id=record.candidate_id, analysis_version=record.analysis_version,
            rank=record.rank, compatibility=record.compatibility, confidence=record.confidence,
            similarity=None, similarity_unavailable_reason="retrieval_not_run",
            dsp_dimensions=record.dsp_dimensions, jev_judgments=record.jev_judgments,
            reasons=record.reasons, warnings=record.warnings) for record in result.ranked)
        return RecommendationBatch(run_id="run-001", palette=palette,
                                   samples=tuple([kick, *candidates]), results=results,
                                   ranking_version=result.ranking_version, mode=mode,
                                   alternatives=result.alternatives), result

    hybrid, result = batch_for(CASE_BY_NAME["pair_frequency"], "hybrid")
    reloaded = RecommendationBatch.from_json(hybrid.to_json())
    assert reloaded == hybrid
    assert reloaded.mode == "hybrid" and reloaded.ranking_version == HYBRID_RANKING_VERSION
    assert list(reloaded.alternatives) == list(result.alternatives)
    assert all(item.similarity is None and item.similarity_unavailable_reason
               for item in reloaded.results)
    assert [item.rank for item in reloaded.results] == list(range(1, len(reloaded.results) + 1))
    assert all(item.jev_judgments for item in reloaded.results)
    assert all(entry.dimension in HYBRID_DIMENSIONS for item in result.ranked
               for entry in item.hybrid_dimensions)
    fallback, fallback_result = batch_for(CASE_BY_NAME["total_unavailability"], "dsp-only")
    reloaded_fallback = RecommendationBatch.from_json(fallback.to_json())
    assert reloaded_fallback == fallback
    assert reloaded_fallback.mode == "dsp-only"
    assert reloaded_fallback.ranking_version == RANKING_VERSION == fallback_result.ranking_version
    assert fallback_result.weight_table_id == DEFAULT_WEIGHT_TABLE_ID
    assert all(item.jev_judgments == () for item in reloaded_fallback.results)
    assert fallback_result.jev_status == JEV_ABSENT


def test_document_matches_the_code_numbers_names_and_codes():
    text = DOCUMENT.read_text(encoding="utf-8")
    for literal in (HYBRID_RANKING_VERSION, HYBRID_WEIGHT_TABLE_ID, "rank_hybrid", "rank_candidates",
                    "HybridResult", "HybridCandidate", "HybridUnscored", "HybridDimensionScore",
                    "HybridEvidence", "HybridPolicy", "HybridWeightTable", "0.50", "0.60", "0.80",
                    "1e-9", "0.30 / 0.70 == 3 / 7", "0.20 / 0.70 == 2 / 7", "0.9785714285714288",
                    "0.21648351648351646", "RANKING_VERSION", "DEFAULT_WEIGHT_TABLE_ID",
                    "RecommendationBatch", "jev-only", "hybrid_dimensions", "uncertain",
                    "alternatives", "no_evidence", "unweighted_dimension", "jev_abstained",
                    "dsp_unavailable_reason", "abs(dsp_score - jev_score)", "score[d]"):
        assert literal in text, literal
    for dimension in HYBRID_DIMENSIONS:
        assert "| " + TICK + dimension + TICK + " |" in text, dimension
    for label, score in JEV_LABEL_SCORES.items():
        assert label + " " + str(score) in text, (label, score)
    for code in HYBRID_CODES:
        assert code in text, code
    for status in HYBRID_STATUS_CODES:
        assert TICK + status + TICK in text, status
    for code in HYBRID_INPUT_ERROR_CODES:
        assert code in text, code
    for field in ("ranking_version", "weight_table_id", "mode", "jev_status", "kick_id",
                  "kick_analysis_version", "ranked", "unscored", "dsp_dimensions", "reasons",
                  "warnings", "hybrid_dimensions", "jev_judgments"):
        assert field in text, field
    for number in ("0.42857142857142855", "0.2857142857142857", "0.70", "3/7", "2/7"):
        assert number in text, number


def test_the_hybrid_weights_are_the_plans_five_dimension_table():
    plan = (ROOT / "_docs" / "plan.md").read_text(encoding="utf-8")
    for literal in ("frequencyFit * 0.3", "transientFit * 0.2", "tonalFit * 0.2",
                    "textureFit * 0.1", "arrangementFit * 0.2"):
        assert literal in plan, literal
