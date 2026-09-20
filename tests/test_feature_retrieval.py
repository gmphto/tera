"""Pure normalized-retrieval tests over synthetic contract samples (issue #25).

Every sample here is synthetic: the ids, the analysis version and the stored
values come from `tests/fixtures/retrieval/retrieval-cases.json`, whose
expectations were written by hand. No real library path, sample name, content
fingerprint or audio byte appears in this file or in that fixture.
"""

from __future__ import annotations

import ast
import builtins
import importlib
import importlib.util
import json
import math
import sys
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest

from backend.analysis import batch
from backend.contracts import (AudioFeatures, AudioMetadata, MEASURES, Measurement,
                              MusicalKey, Sample)
from backend.palette import compatibility, retrieval
from backend.palette.retrieval import (DEFAULT_SHORTLIST_SIZE, DIMENSION_INACTIVE_REASONS,
                                      MAX_ABSOLUTE_Z, MIN_NORMALIZATION_POPULATION,
                                      MIN_RETRIEVAL_COVERAGE, NORMALIZATION_EPSILON,
                                      RECORD_FIELDS, REPRESENTATION_VERSION,
                                      RETRIEVAL_CANDIDATE_ROLES, RETRIEVAL_DIMENSIONS,
                                      RETRIEVAL_ERROR_CODES,
                                      RETRIEVAL_FUNDAMENTAL_CONFIDENCE_FLOOR,
                                      RETRIEVAL_POLICY_VERSION, RETRIEVAL_TRANSFORMS,
                                      SHORTLIST_MAX, SIMILARITY_UNAVAILABLE_REASONS,
                                      CutoffEvidence, NormalizationRecord,
                                      RetrievalInputError, RetrievalPolicy, fit_normalization,
                                      mask_counts, normalization_id, select_shortlist)

FIXTURE = Path(__file__).parent / "fixtures" / "retrieval" / "retrieval-cases.json"
CASES = json.loads(FIXTURE.read_text(encoding="utf-8"))
HAND_CASES = [case for case in CASES["cases"] if "build" not in case]
BUILT_CASES = [case for case in CASES["cases"] if "build" in case]
CASE_IDS = [case["name"] for case in HAND_CASES]
DOCUMENT = Path(__file__).resolve().parent.parent / "_docs" / "feature-retrieval.md"
ANALYSIS = CASES["analysis_version"]
TRANSFORM_OF = dict(RETRIEVAL_TRANSFORMS)

# Two synthetic samples that differ on all fourteen declared dimensions, so
# every dimension is active and each z-score is exactly -1 or +1.
WIDE_A = {"fundamental": 64.0, "spectral_centroid": 512.0, "spectral_rolloff": 4096.0,
          "loudness": -30.0, "crest_factor": 2.0, "transient_strength": 0.125, "attack": 0.0,
          "decay": 0.0, "band_sub": 0.125, "band_bass": 0.125, "band_low_mid": 0.125,
          "band_mid": 0.125, "band_high_mid": 0.125, "band_high": 0.125}
WIDE_B = {"fundamental": 256.0, "spectral_centroid": 2048.0, "spectral_rolloff": 16384.0,
          "loudness": -28.0, "crest_factor": 4.0, "transient_strength": 0.375, "attack": 3.0,
          "decay": 3.0, "band_sub": 0.375, "band_bass": 0.375, "band_low_mid": 0.375,
          "band_mid": 0.375, "band_high_mid": 0.375, "band_high": 0.375}


def measure(name, value, confidence=None):
    if value is None:
        return Measurement(name=name, value=None, unit=MEASURES[name][0],
                           unavailable_reason="synthetic_unknown")
    if name in ("fundamental", "tempo") and confidence is None:
        confidence = 0.9
    return Measurement(name=name, value=value, unit=MEASURES[name][0], confidence=confidence)


def sample(sample_id, values=None, *, role="bass", analysis=ANALYSIS, confidences=None):
    stored = {name: None for name in MEASURES}
    stored.update({"rms": 0.2, "peak": 1.0, "transient_position": 5.0, "stereo_width": 0.0})
    stored.update(values or {})
    confidences = confidences or {}
    measurements = tuple(measure(name, stored[name], confidences.get(name)) for name in MEASURES)
    return Sample(sample_id=sample_id, role=role,
                  audio=AudioMetadata(local_path="C:/tera-fixtures/" + sample_id + ".wav",
                                      sample_rate_hz=48000, channels=1, frame_count=24000,
                                      duration_ms=500.0),
                  features=AudioFeatures(measurements=measurements, key=MusicalKey(
                      tonic=None, mode=None, confidence=None,
                      unavailable_reason="insufficient_tonal_evidence")),
                  analysis_version=analysis)


def kick_sample(values=None, analysis=ANALYSIS):
    return sample("kick-synthetic-001", values, role="kick", analysis=analysis)


def case_samples(case):
    population = tuple(sample(sample_id, entry["values"], role=entry["role"], analysis=ANALYSIS)
                       for sample_id, entry in sorted(case["population"].items()))
    start = kick_sample(case["kick"]["values"])
    outside = case.get("outside_population", {})
    candidates = []
    for sample_id in case["candidates"]:
        known = next((item for item in population if item.sample_id == sample_id), None)
        if known is None:
            entry = outside[sample_id]
            known = sample(sample_id, entry["values"], role=entry["role"], analysis=ANALYSIS)
        candidates.append(known)
    return population, start, tuple(candidates)


def finite(value):
    return value is None or math.isfinite(value)


@pytest.mark.parametrize("case", HAND_CASES, ids=CASE_IDS)
def test_the_hand_written_cases(case):
    expected = case["expected"]
    if "error" in expected:
        with pytest.raises(RetrievalInputError) as refusal:
            RetrievalPolicy(case["policy"]["shortlist_size"])
        assert refusal.value.code == expected["error"]
        return
    policy = RetrievalPolicy(case["policy"]["shortlist_size"])
    population, kick, candidates = case_samples(case)
    record = fit_normalization(population, analysis_version=ANALYSIS)
    assert record.sample_count == len(population)
    assert record.candidate_roles == RETRIEVAL_CANDIDATE_ROLES
    assert record.representation_version == REPRESENTATION_VERSION
    assert record.analysis_version == ANALYSIS
    assert set(expected["normalization"]) == set(RETRIEVAL_DIMENSIONS)
    for dimension in record.dimensions:
        wanted = expected["normalization"][dimension.name]
        assert dimension.transform == TRANSFORM_OF[dimension.name]
        if wanted["mean"] is None:
            assert dimension.mean is None and dimension.std is None, dimension.name
        else:
            assert dimension.mean == pytest.approx(wanted["mean"], abs=1e-12), dimension.name
            assert dimension.std == pytest.approx(wanted["std"], abs=1e-12), dimension.name
        assert dimension.inactive_reason == wanted["inactive_reason"], dimension.name
        assert finite(dimension.mean) and finite(dimension.std)
    assert batch.canonical(record.to_dict())
    if "domain_rejected" in expected:
        counts = {mask.name: mask.domain_rejected
                  for mask in mask_counts(population, analysis_version=ANALYSIS)}
        assert counts == {name: expected["domain_rejected"].get(name, 0)
                          for name in RETRIEVAL_DIMENSIONS}
    result = select_shortlist(kick, candidates, policy=policy, normalization=record)
    assert [item.sample_id for item in result.ranked] == expected["order"]
    assert result.shortlist == tuple(expected["order"])
    assert result.shortlist_size_requested == policy.shortlist_size
    assert result.shortlist_size_returned == expected["shortlist_size_returned"]
    assert result.limit_reason == expected["limit_reason"]
    assert result.policy_version == RETRIEVAL_POLICY_VERSION
    assert result.representation_version == REPRESENTATION_VERSION
    assert result.normalization_id == normalization_id(record)
    assert result.analysis_version == ANALYSIS
    for item in result.ranked:
        wanted = expected["candidates"][item.sample_id]
        assert item.position == expected["order"].index(item.sample_id) + 1
        if wanted["similarity"] is None:
            assert item.similarity is None, item.sample_id
        else:
            assert item.similarity == pytest.approx(wanted["similarity"], abs=1e-12), item.sample_id
        assert item.similarity_unavailable_reason == wanted["reason"], item.sample_id
        assert item.coverage == pytest.approx(wanted["coverage"], abs=1e-12), item.sample_id
        assert list(item.common_dimensions) == wanted["common_dimensions"], item.sample_id
        assert math.isfinite(item.coverage)
        assert item.similarity is None or math.isfinite(item.similarity)
    assert result.cutoff.to_dict() == expected["cutoff"]
    if "not_the_population_mean" in expected:
        actual = next(item.similarity for item in result.ranked if item.sample_id == "bass-c")
        imputed = expected["not_the_population_mean"]["imputed_loudness_similarity"]
        assert actual is not None and abs(actual - imputed) > 1e-6
    permuted = select_shortlist(kick, tuple(reversed(candidates)), policy=policy,
                                normalization=record)
    assert batch.canonical(permuted.to_dict()) == batch.canonical(result.to_dict())
    assert json.dumps(result.to_dict(), allow_nan=False)


def test_the_declared_dimension_list_and_transform_mapping_are_exact():
    assert RETRIEVAL_DIMENSIONS == ("fundamental", "spectral_centroid", "spectral_rolloff",
                                    "loudness", "crest_factor", "transient_strength", "attack",
                                    "decay", "band_sub", "band_bass", "band_low_mid", "band_mid",
                                    "band_high_mid", "band_high")
    assert set(RETRIEVAL_DIMENSIONS) <= set(MEASURES)
    assert [name for name, _transform in RETRIEVAL_TRANSFORMS] == list(RETRIEVAL_DIMENSIONS)
    assert TRANSFORM_OF == {
        "fundamental": "log2_hz", "spectral_centroid": "log2_hz", "spectral_rolloff": "log2_hz",
        "attack": "log1p_ms", "decay": "log1p_ms", "loudness": "linear",
        "crest_factor": "linear", "transient_strength": "linear", "band_sub": "linear",
        "band_bass": "linear", "band_low_mid": "linear", "band_mid": "linear",
        "band_high_mid": "linear", "band_high": "linear"}
    assert RETRIEVAL_CANDIDATE_ROLES == ("bass", "sub-bass")
    assert DEFAULT_SHORTLIST_SIZE == SHORTLIST_MAX
    assert SIMILARITY_UNAVAILABLE_REASONS == ("no_active_dimensions",
                                              "kick_dimensions_insufficient",
                                              "insufficient_common_dimensions",
                                              "extreme_dimension_value")
    assert DIMENSION_INACTIVE_REASONS == ("insufficient_population", "zero_variance")
    assert set(RETRIEVAL_ERROR_CODES) == {
        "invalid_policy", "invalid_shortlist_size", "invalid_normalization", "unknown_dimension",
        "invalid_candidates", "invalid_kick", "invalid_analysis_version", "stale_kick_analysis",
        "candidate_analysis_version_mismatch", "duplicate_candidate_id"}
    assert RETRIEVAL_FUNDAMENTAL_CONFIDENCE_FLOOR == 0.80  # retrieval's own floor
    assert MIN_NORMALIZATION_POPULATION == 2 and MAX_ABSOLUTE_Z == 1e9
    assert MIN_RETRIEVAL_COVERAGE == 0.50 and NORMALIZATION_EPSILON == 1e-9


def test_the_document_names_every_dimension_transform_constant_reason_code_and_exclusion():
    text = DOCUMENT.read_text(encoding="utf-8")
    for name, literal in (("RETRIEVAL_POLICY_VERSION", RETRIEVAL_POLICY_VERSION),
                          ("REPRESENTATION_VERSION", REPRESENTATION_VERSION),
                          ("SHORTLIST_MIN", "50"), ("SHORTLIST_MAX", "100"),
                          ("DEFAULT_SHORTLIST_SIZE", "100"),
                          ("MIN_RETRIEVAL_COVERAGE", "0.50"),
                          ("NORMALIZATION_EPSILON", "1e-9"),
                          ("MIN_NORMALIZATION_POPULATION", "2"),
                          ("MAX_ABSOLUTE_Z", "1e9"),
                          ("RETRIEVAL_FUNDAMENTAL_CONFIDENCE_FLOOR", "0.80"),
                          ("RETRIEVAL_CANDIDATE_ROLES", '"bass", "sub-bass"'),
                          ("RETRIEVAL_DIMENSIONS", None), ("RETRIEVAL_TRANSFORMS", None),
                          ("DIMENSION_INACTIVE_REASONS", None),
                          ("SIMILARITY_UNAVAILABLE_REASONS", None)):
        assert name in text, name
        if literal is not None:
            assert literal in text, f"{name} = {literal}"
    for dimension in RETRIEVAL_DIMENSIONS:
        assert f"`{dimension}`" in text or f"| `{dimension}`" in text, dimension
    for transform in ("log2_hz", "log1p_ms", "linear"):
        assert transform in text, transform
    for reason in (*DIMENSION_INACTIVE_REASONS, *SIMILARITY_UNAVAILABLE_REASONS):
        assert reason in text, reason
    for code in RETRIEVAL_ERROR_CODES:
        assert code in text, code
    for limit in ("shortlist_size", "eligible_exhausted", "no_candidates"):
        assert limit in text, limit
    for excluded in ("rms", "peak", "transient_position", "stereo_width", "tempo",
                     "features.key"):
        assert excluded in text, excluded
    assert "not a recommendation" in text
    assert "recall" in text


def test_duplicate_and_unknown_dimension_names_are_refused():
    population = [sample("bass-a"), sample("bass-b")]
    record = fit_normalization(population, analysis_version=ANALYSIS)
    duplicate = replace(record, dimensions=record.dimensions[:1] + record.dimensions[:1]
                        + record.dimensions[2:])
    with pytest.raises(RetrievalInputError) as repeated:
        select_shortlist(population[0], population, policy=RetrievalPolicy(50),
                         normalization=duplicate)
    assert repeated.value.code == "invalid_normalization"
    unknown_entry = replace(record.dimensions[0], name="not_a_measure")
    unknown = replace(record, dimensions=(unknown_entry,) + record.dimensions[1:])
    with pytest.raises(RetrievalInputError) as refused:
        select_shortlist(population[0], population, policy=RetrievalPolicy(50),
                         normalization=unknown)
    assert refused.value.code == "unknown_dimension"
    for broken in (replace(record, representation_version="another-space"),
                   replace(record, candidate_roles=("bass",)),
                   replace(record, dimensions=record.dimensions[:3]),
                   replace(record, dimensions=tuple(reversed(record.dimensions))),
                   replace(record.dimensions[0], std=-1.0),
                   replace(record.dimensions[0], inactive_reason="not_a_reason")):
        if isinstance(broken, NormalizationRecord):
            with pytest.raises(RetrievalInputError) as error:
                select_shortlist(population[0], population, policy=RetrievalPolicy(50),
                                 normalization=broken)
        else:
            with pytest.raises(RetrievalInputError) as error:
                select_shortlist(population[0], population, policy=RetrievalPolicy(50),
                                 normalization=replace(
                                     record, dimensions=(broken,) + record.dimensions[1:]))
        assert error.value.code in ("invalid_normalization", "unknown_dimension")


def test_unknown_values_are_masked_never_imputed():
    population = [sample("bass-a", {"loudness": -20.0, "crest_factor": 2.0}),
                  sample("bass-b", {"loudness": None, "crest_factor": 4.0})]
    record = fit_normalization(population, analysis_version=ANALYSIS)
    loudness = next(item for item in record.dimensions if item.name == "loudness")
    assert loudness.mean is None and loudness.std is None
    assert loudness.known_count == 1 and loudness.inactive_reason == "insufficient_population"
    counts = {mask.name: (mask.known_count, mask.domain_rejected)
              for mask in mask_counts(population, analysis_version=ANALYSIS)}
    assert counts["loudness"] == (1, 0) and counts["crest_factor"] == (2, 0)
    zeroed = [sample("bass-a", {"spectral_centroid": 0.0}), sample("bass-b", {"spectral_centroid": 512.0}),
              sample("bass-c", {"spectral_centroid": 2048.0})]
    counts = {mask.name: (mask.known_count, mask.domain_rejected)
              for mask in mask_counts(zeroed, analysis_version=ANALYSIS)}
    assert counts["spectral_centroid"] == (2, 1)
    record = fit_normalization(zeroed, analysis_version=ANALYSIS)
    centroid = next(item for item in record.dimensions if item.name == "spectral_centroid")
    assert centroid.known_count == 2 and centroid.inactive_reason is None
    assert centroid.mean == pytest.approx(10.0, abs=1e-12)
    assert centroid.std == pytest.approx(1.0, abs=1e-12)
    # A log-transformed value below its domain cannot be stored: MEASURES bounds
    # attack/decay at 0 and requires a positive fundamental, so a stored
    # frequency of 0 (spectral_centroid, spectral_rolloff) is the reachable
    # domain refusal, and it is counted rather than turned into -inf.


def test_the_fundamental_confidence_floor_is_retrieval_side_only():
    low = [sample("bass-a", {"fundamental": 64.0}, confidences={"fundamental": 0.79}),
           sample("bass-b", {"fundamental": 128.0}, confidences={"fundamental": 0.79})]
    at_floor = [replace(item, features=replace(item.features, measurements=tuple(
        replace(measurement, confidence=0.80) if measurement.name == "fundamental" else measurement
        for measurement in item.features.measurements))) for item in low]
    masked = fit_normalization(low, analysis_version=ANALYSIS)
    used = fit_normalization(at_floor, analysis_version=ANALYSIS)
    masked_first = next(item for item in masked.dimensions if item.name == "fundamental")
    used_first = next(item for item in used.dimensions if item.name == "fundamental")
    assert masked_first.known_count == 0 and masked_first.inactive_reason == "insufficient_population"
    assert used_first.known_count == 2 and used_first.mean == pytest.approx(6.5, abs=1e-12)
    before = batch.canonical(used.to_dict())
    original = compatibility.CONFIDENCE_THRESHOLD
    try:
        compatibility.CONFIDENCE_THRESHOLD = 0.10
        assert batch.canonical(fit_normalization(at_floor, analysis_version=ANALYSIS).to_dict()) == before
        compatibility.CONFIDENCE_THRESHOLD = 0.99
        assert batch.canonical(fit_normalization(at_floor, analysis_version=ANALYSIS).to_dict()) == before
    finally:
        compatibility.CONFIDENCE_THRESHOLD = original
    # The floor is retrieval's own constant: it happens to carry the same value
    # as #11's threshold, and changing that threshold changes nothing here.
    assert compatibility.CONFIDENCE_THRESHOLD == 0.80


def test_fit_refuses_a_non_candidate_role_a_wrong_version_and_a_repeated_id():
    population = [sample("bass-a", {"loudness": -20.0}), sample("bass-b", {"loudness": -10.0})]
    with pytest.raises(RetrievalInputError) as role:
        fit_normalization([sample("kick-001", role="kick", values={"loudness": -20.0})],
                          analysis_version=ANALYSIS)
    assert role.value.code == "invalid_candidates"
    with pytest.raises(RetrievalInputError) as version:
        fit_normalization([sample("bass-a", analysis="other-version")], analysis_version=ANALYSIS)
    assert version.value.code == "candidate_analysis_version_mismatch"
    with pytest.raises(RetrievalInputError) as repeated:
        fit_normalization(population + [population[0]], analysis_version=ANALYSIS)
    assert repeated.value.code == "duplicate_candidate_id"
    with pytest.raises(RetrievalInputError) as blank:
        fit_normalization(population, analysis_version="  ")
    assert blank.value.code == "invalid_analysis_version"
    with pytest.raises(RetrievalInputError) as not_sequence:
        fit_normalization("bass-a", analysis_version=ANALYSIS)
    assert not_sequence.value.code == "invalid_candidates"


def test_the_record_fields_and_the_ids_are_exact_path_free_and_reproducible():
    population = [sample("bass-a", {"loudness": -20.0, "crest_factor": 2.0}),
                  sample("bass-b", {"loudness": -10.0, "crest_factor": 4.0})]
    record = fit_normalization(population, analysis_version=ANALYSIS)
    assert RECORD_FIELDS["NormalizationRecord"] == ("representation_version", "analysis_version",
                                                    "candidate_roles", "sample_count",
                                                    "population_digest", "dimensions")
    assert RECORD_FIELDS["DimensionNormalization"] == ("name", "transform", "mean", "std",
                                                       "known_count", "inactive_reason")
    assert RECORD_FIELDS["RetrievedCandidate"] == ("sample_id", "position", "similarity",
                                                   "similarity_unavailable_reason", "coverage",
                                                   "common_dimensions")
    assert RECORD_FIELDS["CutoffEvidence"] == ("similarity", "included", "excluded", "tied_ids")
    assert RECORD_FIELDS["RetrievalResult"] == ("policy_version", "representation_version",
                                                "normalization_id", "analysis_version", "policy",
                                                "shortlist_size_requested",
                                                "shortlist_size_returned", "limit_reason",
                                                "ranked", "shortlist", "cutoff")
    assert RECORD_FIELDS["RetrievalPolicy"] == ("shortlist_size",)
    assert len(record.population_digest) == 64 and record.population_digest.islower()
    assert normalization_id(record) == normalization_id(
        fit_normalization(tuple(reversed(population)), analysis_version=ANALYSIS))
    assert normalization_id(record).islower() and len(normalization_id(record)) == 64
    text = batch.canonical(record.to_dict())
    for forbidden in ("C:", ".wav", "/", "\\", "sha256", "tera-fixtures"):
        assert forbidden not in text, forbidden
    result = select_shortlist(population[0], population, policy=RetrievalPolicy(50),
                              normalization=record)
    assert batch.canonical(select_shortlist(population[0], tuple(reversed(population)),
                                            policy=RetrievalPolicy(50),
                                            normalization=record).to_dict()) == batch.canonical(
        result.to_dict())
    with pytest.raises(FrozenInstanceError):
        result.shortlist = ()


def test_an_empty_population_is_a_valid_record():
    record = fit_normalization((), analysis_version=ANALYSIS)
    assert record.sample_count == 0
    assert all(dimension.known_count == 0 for dimension in record.dimensions)
    assert all(dimension.inactive_reason == "insufficient_population"
               for dimension in record.dimensions)
    assert all(dimension.mean is None and dimension.std is None for dimension in record.dimensions)
    assert len(normalization_id(record)) == 64
    assert batch.canonical(record.to_dict())
    result = select_shortlist(kick_sample(), (), policy=RetrievalPolicy(50),
                              normalization=record)
    assert result.ranked == () and result.shortlist == ()
    assert result.limit_reason == "no_candidates"
    assert result.cutoff == CutoffEvidence(None, None, None, ())
    assert result.shortlist_size_returned == 0


def test_degenerate_populations_and_extreme_values_never_go_non_finite():
    constant = [sample("bass-a", {"transient_strength": 0.5}), sample("bass-b", {"transient_strength": 0.5})]
    record = fit_normalization(constant, analysis_version=ANALYSIS)
    strength = next(item for item in record.dimensions if item.name == "transient_strength")
    assert strength.inactive_reason == "zero_variance" and strength.mean is None
    single = fit_normalization([sample("bass-a", {"loudness": -3.0})], analysis_version=ANALYSIS)
    assert all(item.inactive_reason == "insufficient_population" for item in single.dimensions)
    population = [sample("bass-a", {"crest_factor": 2.0, "loudness": -30.0}),
                  sample("bass-b", {"crest_factor": 2.000000010, "loudness": -28.0})]
    record = fit_normalization(population, analysis_version=ANALYSIS)
    kick = kick_sample({"crest_factor": 2.0, "loudness": -30.0})
    extreme = sample("bass-extreme", {"crest_factor": 1e308, "loudness": -30.0})
    result = select_shortlist(kick, population + [extreme], policy=RetrievalPolicy(50),
                              normalization=record)
    unscored = next(item for item in result.ranked if item.sample_id == "bass-extreme")
    assert unscored.similarity is None
    assert unscored.similarity_unavailable_reason == "extreme_dimension_value"
    assert result.ranked[-1].sample_id == "bass-extreme"
    for item in result.ranked:
        assert math.isfinite(item.coverage)
        assert item.similarity is None or math.isfinite(item.similarity)
    for dimension in record.dimensions:
        assert finite(dimension.mean) and finite(dimension.std)
    assert json.dumps(result.to_dict(), allow_nan=False)
    assert batch.canonical(result.to_dict())
    huge = [sample("bass-a", {"crest_factor": 1e308}), sample("bass-b", {"crest_factor": 1e308})]
    overflowing = fit_normalization(huge, analysis_version=ANALYSIS)
    dimension = next(item for item in overflowing.dimensions if item.name == "crest_factor")
    assert finite(dimension.mean) and finite(dimension.std)
    assert batch.canonical(overflowing.to_dict())


def test_distance_coverage_and_reason_order_are_exact():
    population = [sample("bass-a", {"loudness": -30.0, "crest_factor": 2.0}),
                  sample("bass-b", {"loudness": -28.0, "crest_factor": 4.0})]
    record = fit_normalization(population, analysis_version=ANALYSIS)
    kick = kick_sample({"loudness": -30.0, "crest_factor": 2.0})
    result = select_shortlist(kick, population, policy=RetrievalPolicy(50), normalization=record)
    assert result.ranked[0].similarity == pytest.approx(1.0, abs=1e-12)
    assert result.ranked[1].similarity == pytest.approx(1 / 3, abs=1e-12)
    # Coverage is over the active dimensions, and only loudness and crest_factor
    # are active here, so every candidate covers all of them.
    assert all(item.coverage == pytest.approx(1.0, abs=1e-12) for item in result.ranked)
    assert [len(item.common_dimensions) for item in result.ranked] == [2, 2]
    unknown_kick = kick_sample()
    unscored = select_shortlist(unknown_kick, population, policy=RetrievalPolicy(50),
                                normalization=record)
    assert {item.similarity_unavailable_reason for item in unscored.ranked} == {
        "kick_dimensions_insufficient"}
    inactive = select_shortlist(population[0], population, policy=RetrievalPolicy(50),
                                normalization=fit_normalization(
                                    [population[0]], analysis_version=ANALYSIS))
    assert {item.similarity_unavailable_reason for item in inactive.ranked} == {
        "no_active_dimensions"}
    assert all(item.coverage == 0.0 for item in inactive.ranked)
    wide = [sample("bass-wide-a", dict(WIDE_A)), sample("bass-wide-b", dict(WIDE_B))]
    wide_record = fit_normalization(wide, analysis_version=ANALYSIS)
    sparse = sample("bass-sparse", {"loudness": -29.0, "crest_factor": 3.0})
    result = select_shortlist(kick_sample(dict(WIDE_A)), wide + [sparse],
                              policy=RetrievalPolicy(50), normalization=wide_record)
    unscored = next(item for item in result.ranked if item.sample_id == "bass-sparse")
    assert unscored.similarity_unavailable_reason == "insufficient_common_dimensions"
    assert unscored.coverage == pytest.approx(2 / 14, abs=1e-12)  # two of fourteen active dims
    assert list(unscored.common_dimensions) == ["loudness", "crest_factor"]
    manifest = {item.similarity_unavailable_reason for item in result.ranked
                if item.similarity is None}
    assert manifest <= set(SIMILARITY_UNAVAILABLE_REASONS)


def test_ordering_ties_and_the_cutoff_are_deterministic():
    population = [sample("bass-c", {"loudness": -20.0}), sample("bass-a", {"loudness": -20.0}),
                  sample("bass-b", {"loudness": -10.0})]
    record = fit_normalization(population, analysis_version=ANALYSIS)
    kick = kick_sample({"loudness": -19.0})
    result = select_shortlist(kick, population, policy=RetrievalPolicy(50), normalization=record)
    assert [item.sample_id for item in result.ranked] == ["bass-a", "bass-c", "bass-b"]
    assert result.ranked[0].similarity == result.ranked[1].similarity
    # Nothing was cut, so the last included candidate is the worst one.
    assert result.cutoff.tied_ids == ("bass-b",)
    assert result.cutoff.excluded is None
    case = BUILT_CASES[0]
    build = case["build"]
    population, candidates = [], []
    for index in range(1, build["count"] + 1):
        sample_id = build["id_format"].format(index=index)
        value = build["tie_value"] if index >= build["tie_from_index"] else (
            build["first_value"] + (index - 1) * build["step"])
        item = sample(sample_id, {"loudness": value})
        population.append(item)
        candidates.append(item)
    kick = kick_sample({"loudness": build["kick_loudness"]})
    record = fit_normalization(population, analysis_version=ANALYSIS)
    policy = RetrievalPolicy(case["policy"]["shortlist_size"])
    result = select_shortlist(kick, candidates, policy=policy, normalization=record)
    expected = case["expected"]
    assert [item.sample_id for item in result.ranked[:len(expected["order_prefix"])]] == \
        expected["order_prefix"]
    assert result.shortlist_size_returned == expected["shortlist_size_returned"]
    assert result.limit_reason == expected["limit_reason"]
    assert result.shortlist[-1] == expected["included"]
    assert result.cutoff.included == expected["included"]
    assert result.cutoff.excluded == expected["excluded"]
    assert list(result.cutoff.tied_ids) == expected["tied_ids"]
    again = select_shortlist(kick, tuple(reversed(candidates)), policy=policy, normalization=record)
    assert again.shortlist == result.shortlist
    assert batch.canonical(again.to_dict()) == batch.canonical(result.to_dict())


def test_size_bounds_and_every_shortfall_reason():
    for refused in (True, False, 49, 101, 50.5, "100", None):
        with pytest.raises(RetrievalInputError) as error:
            RetrievalPolicy(refused)
        assert error.value.code == "invalid_shortlist_size"
    for allowed in (50, 100, 73):
        assert RetrievalPolicy(allowed).shortlist_size == allowed
    population = [sample(f"bass-{index:03d}", {"loudness": -20.0 - index})
                  for index in range(1, 61)]
    record = fit_normalization(population, analysis_version=ANALYSIS)
    kick = kick_sample({"loudness": -20.0})
    many = select_shortlist(kick, population, policy=RetrievalPolicy(50), normalization=record)
    assert many.shortlist_size_returned == 50 and many.limit_reason == "shortlist_size"
    assert len(many.ranked) == 60 and set(many.shortlist) <= set(
        item.sample_id for item in many.ranked)
    few = population[:12]
    result = select_shortlist(kick, few, policy=RetrievalPolicy(100),
                              normalization=fit_normalization(few, analysis_version=ANALYSIS))
    assert result.shortlist_size_requested == 100 and result.shortlist_size_returned == 12
    assert result.limit_reason == "eligible_exhausted" and result.cutoff.excluded is None
    exact = population[:50]
    result = select_shortlist(kick, exact, policy=RetrievalPolicy(50),
                              normalization=fit_normalization(exact, analysis_version=ANALYSIS))
    assert result.limit_reason == "eligible_exhausted" and result.cutoff.excluded is None
    empty = select_shortlist(kick, (), policy=RetrievalPolicy(50), normalization=record)
    assert empty.limit_reason == "no_candidates"
    with pytest.raises(RetrievalInputError) as error:
        select_shortlist(kick, population, policy="50", normalization=record)
    assert error.value.code == "invalid_policy"


def test_analysis_version_representation_and_duplicate_candidate_rules():
    population = [sample("bass-a", {"loudness": -30.0}), sample("bass-b", {"loudness": -28.0})]
    record = fit_normalization(population, analysis_version=ANALYSIS)
    stale = kick_sample({"loudness": -30.0}, analysis="other-version")
    with pytest.raises(RetrievalInputError) as kick_error:
        select_shortlist(stale, population, policy=RetrievalPolicy(50), normalization=record)
    assert kick_error.value.code == "stale_kick_analysis"
    with pytest.raises(RetrievalInputError) as candidate_error:
        select_shortlist(population[0],
                         [sample("bass-c", analysis="other-version")],
                         policy=RetrievalPolicy(50), normalization=record)
    assert candidate_error.value.code == "candidate_analysis_version_mismatch"
    with pytest.raises(RetrievalInputError) as duplicate_error:
        select_shortlist(population[0], [population[0], population[0]],
                         policy=RetrievalPolicy(50), normalization=record)
    assert duplicate_error.value.code == "duplicate_candidate_id"
    with pytest.raises(RetrievalInputError) as kick_type:
        select_shortlist({"sample_id": "kick"}, population, policy=RetrievalPolicy(50),
                         normalization=record)
    assert kick_type.value.code == "invalid_kick"
    with pytest.raises(RetrievalInputError) as normalization_error:
        select_shortlist(population[0], population, policy=RetrievalPolicy(50),
                         normalization=record.to_dict())
    assert normalization_error.value.code == "invalid_normalization"



def test_similarity_is_not_compatibility_and_is_never_written_elsewhere():
    from backend.contracts import RankedCandidate

    population = [sample("bass-a", {"loudness": -30.0}), sample("bass-b", {"loudness": -10.0})]
    record = fit_normalization(population, analysis_version=ANALYSIS)
    result = select_shortlist(kick_sample({"loudness": -30.0}), population,
                              policy=RetrievalPolicy(50), normalization=record)
    assert set(RECORD_FIELDS["RetrievedCandidate"]) == {
        "sample_id", "position", "similarity", "similarity_unavailable_reason", "coverage",
        "common_dimensions"}
    for forbidden in ("compatibility", "confidence", "weight", "jev", "dimension_score"):
        assert all(forbidden not in field for field in RECORD_FIELDS["RetrievedCandidate"])
        assert all(forbidden not in field for field in RECORD_FIELDS["RetrievalResult"])
    # A DSP-only compatibility order may put the last retrieval candidate first;
    # each value stays in its own contract field.
    last = result.ranked[-1]
    contract = RankedCandidate(candidate_id=last.sample_id, analysis_version=record.analysis_version,
                               rank=1, compatibility=0.9, confidence=0.5, similarity=last.similarity,
                               similarity_unavailable_reason=None, dsp_dimensions=(),
                               jev_judgments=(), reasons=(), warnings=())
    assert contract.similarity == last.similarity
    assert contract.compatibility == 0.9
    assert result.ranked[0].similarity != contract.compatibility


def test_the_module_imports_nothing_forbidden_and_opens_nothing():
    tree = ast.parse(Path(retrieval.__file__).read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    allowed = {"__future__", "collections.abc", "dataclasses", "math",
               "backend.analysis.batch", "backend.contracts"}
    assert imported <= allowed, imported - allowed
    for module in ("backend.analysis.batch", "backend.contracts"):
        assert module in imported
    for forbidden in ("sqlite3", "backend.library", "backend.library.repository",
                      "backend.palette.compatibility", "backend.palette.ranking",
                      "backend.intelligence", "backend.audio", "numpy", "scipy", "soundfile"):
        assert forbidden not in imported

    def forbidden(*args, **kwargs):
        raise AssertionError("importing retrieval must not open a path or decode audio")

    # The module is executed once more in a fresh namespace with `open`
    # poisoned; reloading the imported module instead would rebind its classes
    # and break every later test that holds an instance of them.
    name = "retrieval_reimport"
    specification = importlib.util.spec_from_file_location(name, retrieval.__file__)
    reimported = importlib.util.module_from_spec(specification)
    real_open = builtins.open
    try:
        builtins.open = forbidden
        sys.modules[name] = reimported
        specification.loader.exec_module(reimported)
    finally:
        builtins.open = real_open
        sys.modules.pop(name, None)
    assert reimported.REPRESENTATION_VERSION == REPRESENTATION_VERSION
    assert reimported.RetrievalPolicy(50).shortlist_size == 50
