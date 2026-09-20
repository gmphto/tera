"""Synthetic DSP-baseline fixtures; no real audio, library, Jev or network use.

Every fixture declares its measured values and the expected scores are derived
by hand from the mapping documented in _docs/dsp-baseline.md, never read back
from the implementation.
"""

from dataclasses import FrozenInstanceError
from fractions import Fraction
from itertools import combinations
import math
from pathlib import Path
import re

import pytest

from backend.contracts import (AudioFeatures, AudioMetadata, MEASURES, Measurement, MusicalKey,
                               PaletteContext, RankedCandidate, RecommendationBatch, Sample, SongContext)
from backend.palette.ranking import (DEFAULT_WEIGHT_TABLE, DEFAULT_WEIGHT_TABLE_ID, DIMENSIONS,
                                     INSUFFICIENT_EVIDENCE, RANKING_VERSION, TONAL_INTERVAL_COMPATIBILITY,
                                     RankingInputError, RankingPolicy, WeightTable, rank_candidates)


RATE = 48000
FRAMES = 24000
ANALYSIS = "dsp-baseline-fixture-1"
RANKING_FILE = Path(__file__).resolve().parents[1] / "backend" / "palette" / "ranking.py"
DOCUMENT = Path(__file__).resolve().parents[1] / "_docs" / "dsp-baseline.md"

# Declared band profiles. Each sums to exactly 1 and the low band (sub + bass)
# is the only frequency input.
KICK_BANDS = {"band_sub": 0.50, "band_bass": 0.30, "band_low_mid": 0.10, "band_mid": 0.05,
              "band_high_mid": 0.03, "band_high": 0.02}
COMPLEMENT_BANDS = {"band_sub": 0.00, "band_bass": 0.10, "band_low_mid": 0.30, "band_mid": 0.40,
                    "band_high_mid": 0.15, "band_high": 0.05}
CONFLICT_BANDS = {"band_sub": 0.60, "band_bass": 0.30, "band_low_mid": 0.05, "band_mid": 0.03,
                  "band_high_mid": 0.01, "band_high": 0.01}
ALL_BANDS_ZERO = {name: 0.0 for name in KICK_BANDS}


def measure(name, value=None, confidence=None):
    return Measurement(name=name, value=value, unit=MEASURES[name][0], confidence=confidence,
                       unavailable_reason="not_implemented" if value is None else None)


def musical_key(tonic=None, mode="major", confidence=0.90):
    return MusicalKey(tonic=tonic, mode=mode if tonic else None,
                      confidence=confidence if tonic else None,
                      unavailable_reason=None if tonic else "insufficient_key_context")


def sample(sample_id, role="bass", *, analysis=ANALYSIS, tonal=None, fundamental=None,
           fundamental_confidence=None, peak=0.8, rms=0.2, frames=FRAMES, **values):
    """Build a contract Sample; omitted measurements stay explicit unknowns."""
    measurements = {name: measure(name) for name in MEASURES}
    measurements["peak"] = measure("peak", peak)
    measurements["rms"] = measure("rms", rms)
    measurements["fundamental"] = measure("fundamental", fundamental,
                                          fundamental_confidence if fundamental is not None else None)
    for name, value in values.items():
        if value is not None:
            measurements[name] = measure(name, value)
    return Sample(sample_id=sample_id, role=role,
                  audio=AudioMetadata(local_path=f"C:/synthetic-ranking/{sample_id}.wav",
                                      sample_rate_hz=RATE, channels=1, frame_count=frames,
                                      duration_ms=frames * 1000 / RATE),
                  features=AudioFeatures(measurements=tuple(measurements.values()),
                                         key=tonal or musical_key()),
                  analysis_version=analysis)


def selected_kick(sample_id="kick-001", **overrides):
    values = dict(role="kick", tonal=musical_key("C", "major", 0.90), fundamental=55.0,
                  fundamental_confidence=0.90, transient_strength=0.80, transient_position=0.0,
                  attack=10.0, decay=250.0, **KICK_BANDS)
    values.update(overrides)
    return sample(sample_id, **values)


def bass(sample_id="bass-001", **overrides):
    values = dict(role="bass", tonal=musical_key("C", "major", 0.90), fundamental=110.0,
                  fundamental_confidence=0.90, transient_strength=0.50, transient_position=300.0,
                  attack=5.0, decay=120.0, **COMPLEMENT_BANDS)
    values.update(overrides)
    return sample(sample_id, **values)


def rank(candidates, kick=None, policy=None):
    return rank_candidates(kick if kick is not None else selected_kick(), candidates,
                           policy=policy if policy is not None else RankingPolicy())


def entries(record):
    return {entry.dimension: entry for entry in record.dsp_dimensions}


def table_rows(heading):
    """Three-column markdown rows directly under a heading."""
    lines = DOCUMENT.read_text(encoding="utf-8").splitlines()
    rows = []
    for line in lines[lines.index(heading) + 1:]:
        if line.startswith("## "):
            break
        match = re.match(r"^\| (.*?) \| (.*?) \| (.*?) \|$", line.strip())
        if match is not None:
            rows.append(match.groups())
    return rows


def documented_weights():
    """Dimension -> (exact fraction, decimal) from the documented weight table."""
    weights = {}
    for first, second, third in table_rows("## Weight table"):
        name = re.fullmatch(r"`([a-z_]+)`", first.strip())
        if name is None or name.group(1) not in DIMENSIONS:
            continue
        weights[name.group(1)] = (Fraction(second.strip()), float(third.strip()))
    return weights


def documented_coverage():
    """Available-dimension tuple -> (covered fraction parts, documented value)."""
    coverage = {}
    for first, second, third in table_rows("## Missing features, renormalization and confidence"):
        names = tuple(re.findall(r"`([a-z_]+)`", first))
        if not names or not set(names) <= set(DIMENSIONS):
            continue
        coverage[names] = (tuple(part.strip() for part in second.split("+")), Fraction(third.strip()))
    return coverage


def test_version_weight_table_and_provenance_are_documented():
    assert RANKING_VERSION == "dsp-baseline-v1"
    assert DEFAULT_WEIGHT_TABLE_ID == "dsp-baseline-weights-1"
    assert DEFAULT_WEIGHT_TABLE.identifier == DEFAULT_WEIGHT_TABLE_ID
    assert DIMENSIONS == ("frequency", "transient", "tonal")
    weights = (DEFAULT_WEIGHT_TABLE.frequency, DEFAULT_WEIGHT_TABLE.transient, DEFAULT_WEIGHT_TABLE.tonal)
    assert weights == (3 / 7, 2 / 7, 2 / 7)
    assert all(weight > 0 for weight in weights) and abs(sum(weights) - 1) <= 1e-9
    result = rank([bass()])
    assert result.ranking_version == RANKING_VERSION
    assert result.weight_table_id == DEFAULT_WEIGHT_TABLE_ID
    assert result.kick_id == "kick-001" and result.kick_analysis_version == ANALYSIS


@pytest.mark.parametrize("kwargs", [
    {"identifier": "", "frequency": 0.50, "transient": 0.25, "tonal": 0.25},
    {"identifier": "x", "frequency": 0.0, "transient": 0.50, "tonal": 0.50},
    {"identifier": "x", "frequency": -0.50, "transient": 0.75, "tonal": 0.75},
    {"identifier": "x", "frequency": 0.50, "transient": 0.25, "tonal": 0.20},
    {"identifier": "x", "frequency": float("nan"), "transient": 0.50, "tonal": 0.50},
    {"identifier": "x", "frequency": True, "transient": 0.50, "tonal": 0.50},
])
def test_invalid_weight_tables_are_input_errors(kwargs):
    with pytest.raises(RankingInputError) as error:
        WeightTable(**kwargs)
    assert error.value.code == "invalid_policy"


def test_custom_weight_table_is_recorded_and_used():
    table = WeightTable("fixture-weights-2", 0.50, 0.25, 0.25)
    result = rank([bass()], policy=RankingPolicy(table))
    assert result.weight_table_id == "fixture-weights-2"
    record = result.ranked[0]
    expected = 0.50 * 0.9 + 0.25 * 1.0 + 0.25 * 1.0  # declared frequency/transient/tonal scores
    assert record.compatibility == pytest.approx(expected, abs=1e-12)


def test_frequency_complementary_and_conflicting_pairs_order_and_evidence():
    absent = bass("bass-absent-low", band_sub=0.0, band_bass=0.0)
    complementary = bass("bass-complement", **COMPLEMENT_BANDS)
    conflicting = bass("bass-conflict", **CONFLICT_BANDS)
    result = rank([conflicting, absent, complementary])
    assert [record.candidate_id for record in result.ranked] == [
        "bass-absent-low", "bass-complement", "bass-conflict"]
    assert entries(result.ranked[0])["frequency"].compatibility == 1.0
    first, second = result.ranked[1], result.ranked[2]
    # kick low 0.50+0.30 of 1.00 -> 0.80; candidate low 0.00+0.10 of 1.00 -> 0.10;
    # shared = min(0.80, 0.10) = 0.10 -> 1 - 0.10.
    assert entries(first)["frequency"].compatibility == pytest.approx(1 - min(0.80, 0.10), abs=1e-12)
    assert entries(first)["frequency"].unavailable_reason is None
    # candidate low 0.60+0.30 -> 0.90; shared = min(0.80, 0.90) = 0.80 -> 1 - 0.80.
    assert entries(second)["frequency"].compatibility == pytest.approx(1 - min(0.80, 0.90), abs=1e-12)
    reason = first.reasons[0]
    assert "band_sub 0.000" in reason and "band_bass 0.100" in reason
    assert "share 0.100" in reason and "compatibility 0.900" in reason
    assert "compatibility 0.200" in second.reasons[0]


def test_frequency_absent_overlap_and_single_band_energy_score_one():
    absent = bass("bass-absent", band_sub=0.0, band_bass=0.0)
    single = selected_kick(band_sub=0.0, band_bass=0.0, band_low_mid=0.0, band_mid=0.0,
                           band_high_mid=0.0, band_high=1.0)
    result = rank([absent], kick=single)
    assert entries(result.ranked[0])["frequency"].compatibility == 1.0


def test_transient_inside_and_after_kick_decay_order_and_evidence():
    inside = bass("bass-inside", transient_position=100.0)
    after = bass("bass-after", transient_position=300.0)
    result = rank([inside, after])
    assert [record.candidate_id for record in result.ranked] == ["bass-after", "bass-inside"]
    # window end = attack 10 + decay 250 = 260 ms; ratio = min(onset, 260)/260;
    # score = 1 - (1 - ratio) * strength with strength 0.8.
    expected_inside = 1 - (1 - 100.0 / 260.0) * 0.8
    assert entries(result.ranked[1])["transient"].compatibility == pytest.approx(expected_inside, abs=1e-12)
    assert entries(result.ranked[0])["transient"].compatibility == 1.0
    reason = result.ranked[1].reasons[1]
    assert "attack 10.000 ms" in reason and "decay 250.000 ms" in reason
    assert "window end 260.000 ms" in reason and "candidate transient position 100.000 ms" in reason
    assert "onset ratio 0.385" in reason and "compatibility 0.508" in reason


@pytest.mark.parametrize("overrides", [{"transient_strength": 0.0}, {"attack": 0.0, "decay": 0.0}])
def test_transient_zero_strength_and_degenerate_window_score_one(overrides):
    result = rank([bass("bass-inside", transient_position=100.0)], kick=selected_kick(**overrides))
    entry = entries(result.ranked[0])["transient"]
    assert entry.compatibility == 1.0 and entry.unavailable_reason is None


@pytest.mark.parametrize("name,tonic,expected", [("matching", "C", 1.00), ("third", "A", 0.75),
                                                     ("clashing", "F#", 0.10)])
def test_tonal_matching_and_clashing_reliable_keys(name, tonic, expected):
    candidates = [bass("bass-" + item, tonal=musical_key(value, "minor", 0.85))
                  for item, value in (("matching", "C"), ("third", "A"), ("clashing", "F#"))]
    result = rank(candidates)
    assert [record.candidate_id for record in result.ranked] == [
        "bass-matching", "bass-third", "bass-clashing"]
    record = next(item for item in result.ranked if item.candidate_id == "bass-" + name)
    entry = entries(record)["tonal"]
    assert entry.compatibility == pytest.approx(expected, abs=1e-12)
    assert entry.unavailable_reason is None
    reason = record.reasons[2]
    assert "kick key C major" in reason and f"candidate key {tonic} minor" in reason
    assert "interval class" in reason and f"compatibility {expected:.3f}" in reason


@pytest.mark.parametrize("confidence,expected", [
    (math.nextafter(0.80, 0), "kick_key_unreliable"),
    (0.80, None),
    (math.nextafter(0.80, 1), None),
])
def test_kick_key_confidence_boundary(confidence, expected):
    result = rank([bass()], kick=selected_kick(tonal=musical_key("C", "major", confidence)))
    entry = entries(result.ranked[0])["tonal"]
    if expected is None:
        assert entry.compatibility is not None and entry.unavailable_reason is None
    else:
        assert entry.compatibility is None and entry.unavailable_reason == expected
        assert "kick key C major confidence" in result.ranked[0].reasons[2]
        assert "reliability threshold 0.800" in result.ranked[0].reasons[2]


@pytest.mark.parametrize("confidence,expected", [
    (math.nextafter(0.80, 0), "candidate_key_unreliable"),
    (0.80, None),
    (math.nextafter(0.80, 1), None),
])
def test_candidate_key_confidence_boundary(confidence, expected):
    result = rank([bass(tonal=musical_key("C", "major", confidence))])
    entry = entries(result.ranked[0])["tonal"]
    if expected is None:
        assert entry.compatibility is not None and entry.unavailable_reason is None
    else:
        assert entry.compatibility is None and entry.unavailable_reason == expected


@pytest.mark.parametrize("confidence,expected", [
    (math.nextafter(0.80, 0), "f0_unreliable"),
    (0.80, None),
    (math.nextafter(0.80, 1), None),
])
@pytest.mark.parametrize("side", ["kick", "candidate"])
def test_f0_confidence_boundary(side, confidence, expected):
    kick = selected_kick(fundamental_confidence=confidence) if side == "kick" else selected_kick()
    candidate = bass(fundamental_confidence=confidence) if side == "candidate" else bass()
    record = rank([candidate], kick=kick).ranked[0]
    entry = entries(record)["tonal"]
    if expected is None:
        assert entry.compatibility is not None and entry.unavailable_reason is None
    else:
        assert entry.compatibility is None and entry.unavailable_reason == f"{side}_{expected}"
        assert f"{side} fundamental" in record.reasons[2]
        assert "reliability threshold 0.800" in record.reasons[2]


def test_confidence_at_or_above_threshold_never_lowers_a_score():
    low = rank([bass("bass-low", tonal=musical_key("C", "major", 0.80), fundamental_confidence=0.80)]).ranked[0]
    high = rank([bass("bass-high", tonal=musical_key("C", "major", 1.0), fundamental_confidence=1.0)]).ranked[0]
    assert low.compatibility == high.compatibility == pytest.approx(6.7 / 7, abs=1e-12)
    assert low.dsp_dimensions == high.dsp_dimensions and low.confidence == high.confidence == 1.0


@pytest.mark.parametrize("kick_overrides,candidate_overrides,dimension,code", [
    ({"band_sub": 0.0, "band_bass": 0.0, "band_low_mid": 0.0, "band_mid": 0.0,
      "band_high_mid": 0.0, "band_high": 0.0}, {}, "frequency", "kick_band_energy_zero"),
    ({}, {"band_sub": 0.0, "band_bass": 0.0, "band_low_mid": 0.0, "band_mid": 0.0,
          "band_high_mid": 0.0, "band_high": 0.0}, "frequency", "candidate_band_energy_zero"),
    # A candidate #11 admits with an unknown band is an unavailable dimension,
    # never a sum over a missing value (issue #28's recorded conflict).
    ({"band_sub": None}, {}, "frequency", "kick_band_unknown"),
    ({}, {"band_sub": None}, "frequency", "candidate_band_unknown"),
    ({"transient_strength": None}, {}, "transient", "kick_transient_strength_unknown"),
    ({"attack": None}, {}, "transient", "kick_attack_unknown"),
    ({"decay": None}, {}, "transient", "kick_decay_unknown"),
    ({}, {"transient_position": None}, "transient", "candidate_transient_position_unknown"),
    ({"tonal": None}, {}, "tonal", "kick_key_unknown"),
    ({}, {"tonal": None}, "tonal", "candidate_key_unknown"),
    ({"fundamental_confidence": 0.50}, {}, "tonal", "kick_f0_unreliable"),
    ({}, {"fundamental_confidence": 0.50}, "tonal", "candidate_f0_unreliable"),
])
def test_unavailable_dimension_names_the_side_and_is_never_zero(
        kick_overrides, candidate_overrides, dimension, code):
    record = rank([bass("bass-missing", **candidate_overrides)],
                  kick=selected_kick(**kick_overrides)).ranked[0]
    entry = entries(record)[dimension]
    assert entry.compatibility is None and entry.compatibility != 0.0
    assert entry.unavailable_reason == code
    assert code.startswith("kick_") or code.startswith("candidate_")
    assert (("kick" if code.startswith("kick_") else "candidate") in record.reasons[
        list(DIMENSIONS).index(dimension)])


def test_candidate_without_any_available_dimension_is_unscored():
    candidates = [bass("bass-scored"), bass("bass-unscored", tonal=None, transient_position=None,
                                            **{name: 0.0 for name in KICK_BANDS})]
    result = rank(candidates)
    assert [record.candidate_id for record in result.ranked] == ["bass-scored"]
    assert len(result.unscored) == 1
    record = result.unscored[0]
    assert record.candidate_id == "bass-unscored"
    assert record.code == INSUFFICIENT_EVIDENCE == "insufficient_evidence"
    assert record.analysis_version == ANALYSIS
    assert [entry.dimension for entry in record.dsp_dimensions] == list(DIMENSIONS)
    assert all(entry.compatibility is None and entry.unavailable_reason.strip()
               for entry in record.dsp_dimensions)


def test_all_candidates_unscored_gives_empty_ranked_list():
    empty = {name: 0.0 for name in KICK_BANDS}
    result = rank([bass("bass-z", tonal=None, transient_position=None, **empty),
                   bass("bass-a", tonal=None, transient_position=None, **empty)])
    assert result.ranked == ()
    assert [record.candidate_id for record in result.unscored] == ["bass-a", "bass-z"]


def test_ordering_is_compatibility_descending_then_candidate_id_ascending():
    expected = ["bass-a", "bass-b", "bass-c"]
    result = rank([bass(sample_id) for sample_id in reversed(expected)])
    assert [record.candidate_id for record in result.ranked] == expected
    assert [record.rank for record in result.ranked] == [1, 2, 3]
    scores = [record.compatibility for record in result.ranked]
    assert scores[0] == scores[1] == scores[2]
    better = rank([bass("bass-z"), bass("bass-a", **CONFLICT_BANDS)]).ranked
    assert [record.candidate_id for record in better] == ["bass-z", "bass-a"]
    assert better[0].compatibility > better[1].compatibility


def test_input_order_invariance_repeated_calls_and_unchanged_inputs():
    kick = selected_kick()
    candidates = [bass("bass-b"), bass("bass-a", **CONFLICT_BANDS), bass("bass-c", tonal=None)]
    before = [item.to_json() for item in (kick, *candidates)]
    first = rank_candidates(kick, candidates, policy=RankingPolicy())
    second = rank_candidates(kick, list(reversed(candidates)), policy=RankingPolicy())
    third = rank_candidates(kick, tuple(candidates[1:] + candidates[:1]), policy=RankingPolicy())
    assert first == second == third
    assert rank_candidates(kick, list(candidates), policy=RankingPolicy()) == first
    assert [item.to_json() for item in (kick, *candidates)] == before
    with pytest.raises(FrozenInstanceError):
        first.ranked = ()
    with pytest.raises(FrozenInstanceError):
        first.ranked[0].rank = 2


def test_empty_and_single_candidate_sets():
    empty = rank([])
    assert empty.ranked == () and empty.unscored == ()
    assert empty.ranking_version == RANKING_VERSION
    single = rank([bass("bass-only")])
    assert [record.rank for record in single.ranked] == [1]
    assert single.ranked[0].candidate_id == "bass-only"


@pytest.mark.parametrize("kick_overrides,candidate_overrides", [
    ({"transient_strength": 0.0}, {}),
    ({"attack": None}, {}),
    ({"decay": None}, {}),
    ({"band_sub": 0.0, "band_bass": 0.0, "band_low_mid": 0.0, "band_mid": 0.0,
      "band_high_mid": 0.0, "band_high": 0.0}, {}),
    ({"band_sub": 0.0, "band_bass": 0.0, "band_low_mid": 0.0, "band_mid": 0.0,
      "band_high_mid": 0.0, "band_high": 1.0}, {}),
    ({}, {"band_sub": 1.0, "band_bass": 1.0, "band_low_mid": 1.0, "band_mid": 1.0,
          "band_high_mid": 1.0, "band_high": 1.0}),
    ({"attack": 500.0, "decay": 500.0}, {"transient_position": 500.0}),
    ({"tonal": None, "fundamental": None}, {"tonal": None, "fundamental": None}),
])
def test_awkward_but_usable_inputs_are_finite_and_documented(kick_overrides, candidate_overrides):
    result = rank([bass("bass-awkward", **candidate_overrides)], kick=selected_kick(**kick_overrides))
    assert len(result.ranked) + len(result.unscored) == 1
    records = list(result.ranked) + list(result.unscored)
    for record in records:
        assert [entry.dimension for entry in record.dsp_dimensions] == list(DIMENSIONS)
        for entry in record.dsp_dimensions:
            assert entry.compatibility is None or 0 <= entry.compatibility <= 1
            assert entry.unavailable_reason is None or entry.unavailable_reason.strip()
        for reason in record.reasons:
            assert reason.strip() and "None" not in reason and "nan" not in reason.lower()
    for record in result.ranked:
        assert math.isfinite(record.compatibility) and 0 <= record.compatibility <= 1
        assert math.isfinite(record.confidence) and 0 <= record.confidence <= 1


def test_unknown_key_on_kick_candidate_or_both_is_never_a_clash():
    kick = selected_kick(tonal=None)
    candidate = bass(tonal=None)
    both = rank([candidate], kick=kick)
    assert entries(both.ranked[0])["tonal"].unavailable_reason == "kick_key_unknown"
    candidate_side = rank([candidate], kick=selected_kick())
    assert entries(candidate_side.ranked[0])["tonal"].unavailable_reason == "candidate_key_unknown"
    # A reliable kick fundamental never substitutes for the candidate's key.
    partial = bass(tonal=None, fundamental=110.0, fundamental_confidence=0.90)
    record = rank([partial], kick=selected_kick()).ranked[0]
    assert entries(record)["tonal"].unavailable_reason == "candidate_key_unknown"
    assert entries(record)["tonal"].compatibility is None


def test_mixed_analysis_versions_warn_instead_of_silent_comparison():
    result = rank([bass("bass-old", analysis="dsp-fixture-2"), bass("bass-new")])
    assert [record.rank for record in result.ranked] == [1, 2]
    old = next(record for record in result.ranked if record.candidate_id == "bass-old")
    assert old.analysis_version == "dsp-fixture-2" and len(old.warnings) == 1
    assert "dsp-fixture-2" in old.warnings[0] and ANALYSIS in old.warnings[0]
    new = next(record for record in result.ranked if record.candidate_id == "bass-new")
    assert new.warnings == ()


def test_records_carry_exactly_the_three_documented_dimensions():
    result = rank([bass("bass-full"), bass("bass-partial", tonal=None)])
    records = list(result.ranked) + list(result.unscored)
    assert records
    for record in records:
        assert [entry.dimension for entry in record.dsp_dimensions] == list(DIMENSIONS)
        for entry in record.dsp_dimensions:
            if entry.compatibility is None:
                assert entry.unavailable_reason and entry.unavailable_reason.strip()
            else:
                assert entry.unavailable_reason is None and 0 <= entry.compatibility <= 1
        assert len(record.reasons) == len(DIMENSIONS)
        assert not any(name in " ".join(record.reasons) for name in ("rhythmic", "texture", "arrangement"))


def test_confidence_is_coverage_and_compatibility_is_renormalized():
    empty_kick = selected_kick(**{name: 0.0 for name in KICK_BANDS})
    result = rank([bass("bass-full"), bass("bass-transient", tonal=None)], kick=empty_kick)
    full, only_transient = result.ranked
    assert full.confidence == pytest.approx(4 / 7, abs=1e-12)
    assert full.compatibility == pytest.approx((2 / 7 * 1.0 + 2 / 7 * 1.0) / (4 / 7), abs=1e-12)
    assert full.compatibility != pytest.approx(0.9 * 4 / 7, abs=1e-6)
    assert only_transient.confidence == pytest.approx(2 / 7, abs=1e-12)
    assert only_transient.compatibility == pytest.approx(
        1 - (1 - min(300.0, 260.0) / 260.0) * 0.8, abs=1e-12)
    assert full.rank == 1 and only_transient.rank == 2
    # Both renormalize to 1.0 while their coverage-based confidence differs,
    # so confidence is neither multiplied into nor added to compatibility.
    assert full.compatibility == only_transient.compatibility == pytest.approx(1.0, abs=1e-12)
    assert full.confidence > only_transient.confidence


@pytest.mark.parametrize("kick_overrides,candidate_overrides,available", [
    ({}, {}, ("frequency", "transient", "tonal")),
    ({"tonal": None}, {}, ("frequency", "transient")),
    ({"attack": None}, {}, ("frequency", "tonal")),
    ({}, {"transient_position": None}, ("frequency", "tonal")),
    (ALL_BANDS_ZERO, {}, ("transient", "tonal")),
    ({"transient_strength": None}, {"tonal": None}, ("frequency",)),
    ({"transient_strength": None, "tonal": None}, {}, ("frequency",)),
    ({**ALL_BANDS_ZERO, "tonal": None}, {}, ("transient",)),
    (ALL_BANDS_ZERO, {"transient_position": None}, ("tonal",)),
])
def test_confidence_is_exactly_the_covered_weight_for_every_availability_set(
        kick_overrides, candidate_overrides, available):
    record = rank([bass("bass-coverage", **candidate_overrides)],
                  kick=selected_kick(**kick_overrides)).ranked[0]
    assert tuple(entry.dimension for entry in record.dsp_dimensions
                 if entry.compatibility is not None) == available
    expected_confidence = sum(DEFAULT_WEIGHT_TABLE.weight(name) for name in available)
    assert record.confidence == expected_confidence
    scores = {entry.dimension: entry.compatibility for entry in record.dsp_dimensions}
    expected = sum(DEFAULT_WEIGHT_TABLE.weight(name) * scores[name] for name in available)
    assert record.compatibility == pytest.approx(expected / expected_confidence, abs=1e-12)
    if available == ("frequency",):
        assert record.confidence == 3 / 7 == 0.42857142857142855
    elif len(available) == 1:
        assert record.confidence == 2 / 7 == 0.2857142857142857


def test_confidence_never_determines_order():
    narrow = bass("bass-narrow", tonal=None, **{name: 0.0 for name in KICK_BANDS})
    broad = bass("bass-broad", tonal=musical_key("F#", "major", 0.90), transient_position=100.0,
                 **CONFLICT_BANDS)
    result = rank([broad, narrow])
    assert [record.candidate_id for record in result.ranked] == ["bass-narrow", "bass-broad"]
    first, second = result.ranked
    assert first.confidence == pytest.approx(2 / 7, abs=1e-12)
    assert second.confidence == 1.0
    assert first.confidence < second.confidence
    assert first.compatibility == 1.0 and first.compatibility > second.compatibility


def test_reasons_contain_measured_values_and_no_local_paths():
    record = rank([bass("bass-001")]).ranked[0]
    joined = " ".join(record.reasons)
    for shown in ("0.500", "0.300", "0.100", "55.000", "110.000", "10.000", "250.000", "300.000",
                  "0.800"):
        assert shown in joined
    for forbidden in ("C:", "\\", ".wav", "synthetic-ranking", "None", "nan", "inf"):
        assert forbidden not in joined


def test_input_errors_for_malformed_contracts_roles_ids_and_policy():
    kick = selected_kick()
    for broken in ({}, None, "kick-001"):
        with pytest.raises(RankingInputError) as error:
            rank_candidates(broken, [bass()], policy=RankingPolicy())
        assert error.value.code == "invalid_kick"
    with pytest.raises(RankingInputError) as error:
        rank_candidates(bass("kick-001"), [bass()], policy=RankingPolicy())
    assert error.value.code == "invalid_kick"
    malformed = selected_kick()
    object.__setattr__(malformed, "role", "not-a-role")
    with pytest.raises(RankingInputError) as error:
        rank_candidates(malformed, [bass()], policy=RankingPolicy())
    assert error.value.code == "invalid_kick"
    for broken in (None, "bass-a", {"bass-a": bass()}, [{}], [None]):
        with pytest.raises(RankingInputError) as error:
            rank_candidates(kick, broken, policy=RankingPolicy())
        assert error.value.code == "invalid_candidates"
    with pytest.raises(RankingInputError) as error:
        rank_candidates(kick, [bass("bass-a", role="kick")], policy=RankingPolicy())
    assert error.value.code == "invalid_candidates"
    with pytest.raises(RankingInputError) as error:
        rank_candidates(kick, [bass("bass-a"), bass("bass-a")], policy=RankingPolicy())
    assert error.value.code == "duplicate_candidate_id"
    with pytest.raises(RankingInputError) as error:
        rank_candidates(kick, [bass("kick-001")], policy=RankingPolicy())
    assert error.value.code == "selected_kick_in_candidates"
    for policy in (None, object(), "policy", WeightTable):
        with pytest.raises(RankingInputError) as error:
            rank_candidates(kick, [], policy=policy)
        assert error.value.code == "invalid_policy"
    tampered = RankingPolicy()
    object.__setattr__(tampered, "weight_table", "table")
    with pytest.raises(RankingInputError) as error:
        rank_candidates(kick, [], policy=tampered)
    assert error.value.code == "invalid_policy"


def test_schema_one_dsp_only_batch_round_trip():
    kick = selected_kick()
    candidates = [bass("bass-001"), bass("bass-002", **CONFLICT_BANDS), bass("bass-003", tonal=None)]
    result = rank_candidates(kick, candidates, policy=RankingPolicy())
    assert [record.rank for record in result.ranked] == [1, 2, 3] and result.unscored == ()
    palette = PaletteContext(palette_id="palette-001", revision=3, kick_id=kick.sample_id,
                             selected_bass_id=None,
                             song=SongContext(tempo=measure("tempo"), key=musical_key(), genre=None,
                                              genre_unavailable_reason="not_provided"))
    results = tuple(RankedCandidate(candidate_id=record.candidate_id,
                                    analysis_version=record.analysis_version, rank=record.rank,
                                    compatibility=record.compatibility, confidence=record.confidence,
                                    similarity=None, similarity_unavailable_reason="retrieval_not_run",
                                    dsp_dimensions=record.dsp_dimensions, jev_judgments=(),
                                    reasons=record.reasons, warnings=record.warnings)
                    for record in result.ranked)
    batch = RecommendationBatch(run_id="run-001", palette=palette, samples=tuple([kick, *candidates]),
                                results=results, ranking_version=result.ranking_version,
                                mode="dsp-only",
                                alternatives=tuple(record.candidate_id for record in result.ranked))
    reloaded = RecommendationBatch.from_json(batch.to_json())
    assert reloaded == batch
    assert reloaded.mode == "dsp-only" and reloaded.ranking_version == RANKING_VERSION
    assert [item.rank for item in reloaded.results] == [1, 2, 3]
    assert all(item.similarity is None and item.similarity_unavailable_reason for item in reloaded.results)
    assert all(item.jev_judgments == () for item in reloaded.results)
    assert all(entry.dimension in DIMENSIONS for item in reloaded.results for entry in item.dsp_dimensions)


def test_document_coverage_table_matches_the_documented_weight_table():
    weights = documented_weights()
    assert set(weights) == set(DIMENSIONS)
    for name, (fraction, decimal) in weights.items():
        assert decimal == DEFAULT_WEIGHT_TABLE.weight(name)
        assert float(fraction) == decimal
    assert sum(fraction for fraction, _ in weights.values()) == 1
    coverage = documented_coverage()
    assert set(coverage) == {names for size in range(1, len(DIMENSIONS) + 1)
                             for names in combinations(DIMENSIONS, size)}
    for names, (parts, confidence) in coverage.items():
        assert names == tuple(name for name in DIMENSIONS if name in names)
        assert [Fraction(part) for part in parts] == [weights[name][0] for name in names]
        assert confidence == sum(weights[name][0] for name in names)


def test_document_matches_the_code_numbers_names_and_reason_codes():
    text = DOCUMENT.read_text(encoding="utf-8")
    for literal in ("rank_candidates", "dsp-baseline-v1", "dsp-baseline-weights-1", "0.80", "3/7", "2/7",
                    "0.42857142857142855", "0.2857142857142857", "insufficient_evidence", "Tie-break",
                    "renormalized", "confidence"):
        assert literal in text
    for value in TONAL_INTERVAL_COMPATIBILITY:
        assert f"{value:.2f}" in text
    for code in ("kick_band_energy_zero", "candidate_band_energy_zero", "kick_transient_strength_unknown",
                 "kick_attack_unknown", "kick_decay_unknown", "candidate_transient_position_unknown",
                 "kick_key_unknown", "candidate_key_unknown", "kick_key_unreliable",
                 "candidate_key_unreliable", "kick_f0_unreliable", "candidate_f0_unreliable"):
        assert code in text


def test_baseline_module_is_local_documented_and_audio_free():
    source = RANKING_FILE.read_text(encoding="utf-8")
    assert "backend.intelligence" not in source and "backend.audio" not in source
    assert "local_path" not in source and "import requests" not in source and "import socket" not in source
    assert "_docs/dsp-baseline.md" in source
    assert rank_candidates.__doc__ and "_docs/dsp-baseline.md" in rank_candidates.__doc__
