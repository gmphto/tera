from dataclasses import replace

import numpy as np
import pytest

from backend.analysis.spectral import ANALYSIS_VERSION, _one_sided_power, measure_spectral
from backend.audio import load_wav
from backend.contracts import AudioFeatures, AudioMetadata, MEASURES, Measurement, MusicalKey, Sample
from test_loudness import loaded


def measures(audio):
    result = measure_spectral(audio)
    assert result.analysis_version == ANALYSIS_VERSION
    assert len(result.measurements) == 8
    return {item.name: item for item in result.measurements}


def cosine(rate, frequency, amplitude=1, frames=None):
    n = rate if frames is None else frames
    return amplitude * np.cos(2 * np.pi * frequency * np.arange(n) / rate)


# Expected memberships are declared independently, not imported from production.
@pytest.mark.parametrize("frequency,band", [
    (40, "sub"), (100, "bass"), (300, "low_mid"), (1000, "mid"),
    (3000, "high_mid"), (10000, "high"), (20, "sub"), (60, "bass"),
    (250, "low_mid"), (500, "mid"), (2000, "high_mid"), (6000, "high"), (20000, "high"),
])
@pytest.mark.parametrize("rate", [44100, 48000, 96000])
def test_single_tone_every_band_and_boundary(rate, frequency, band):
    result = measures(loaded(cosine(rate, frequency), rate))
    assert result["spectral_centroid"].value == pytest.approx(frequency, abs=1e-8)
    assert result["spectral_rolloff"].value == frequency
    for name in ("sub", "bass", "low_mid", "mid", "high_mid", "high"):
        assert result[f"band_{name}"].value == pytest.approx(float(name == band), abs=1e-12)


def test_multitone_known_powers_and_rolloff():
    # Three powers: 1/2, 4/2, 1/2; cumulative 1/6, 5/6, 1.
    x = cosine(48000, 40) + cosine(48000, 100, 2) + cosine(48000, 1000)
    result = measures(loaded(x))
    assert result["spectral_centroid"].value == pytest.approx((40 + 4*100 + 1000) / 6, abs=1e-8)
    assert result["spectral_rolloff"].value == 1000
    for name, expected in (("sub", 1/6), ("bass", 4/6), ("mid", 1/6)):
        assert result[f"band_{name}"].value == pytest.approx(expected, abs=1e-12)


def test_85_percent_rolloff_excludes_small_high_frequency_component():
    # Powers 9 and 1, so the first bin clears 85%.
    result = measures(loaded(cosine(48000, 100, 3) + cosine(48000, 1000)))
    assert result["spectral_rolloff"].value == 100


@pytest.mark.parametrize("rate", [44100, 48000, 96000])
def test_dc_and_out_of_band_remain_in_denominator(rate):
    # DC power 1, three interior tone powers 1/2 each. Only 100 Hz is in bands.
    x = 1 + cosine(rate, 10) + cosine(rate, 100) + cosine(rate, 21000)
    result = measures(loaded(x, rate))
    assert result["spectral_centroid"].value == pytest.approx((10 + 100 + 21000) / 5, abs=1e-8)
    assert result["spectral_rolloff"].value == 21000
    assert result["band_bass"].value == pytest.approx(0.2, abs=1e-12)
    assert sum(result[f"band_{name}"].value for name in ("sub", "bass", "low_mid", "mid", "high_mid", "high")) == pytest.approx(0.2, abs=1e-12)


@pytest.mark.parametrize("n", [5, 6, 99, 100])
def test_parseval_and_endpoint_weighting(n):
    # Direct time-domain mean square is independent of spectral implementation.
    x = np.column_stack((np.arange(n) / n, np.cos(2*np.pi*np.arange(n)/n)))
    assert np.sum(_one_sided_power(x)) == pytest.approx(np.mean(x*x), rel=1e-13)


def test_dc_interior_and_even_nyquist_have_correct_relative_weight():
    n, rate = 48000, 48000
    x = np.ones(n) + cosine(rate, 1000) + (-1.0)**np.arange(n)
    result = measures(loaded(x))
    # DC/Nyquist each power1; interior cosine power0.5.
    assert result["spectral_centroid"].value == pytest.approx((500 + 24000) / 2.5, abs=1e-8)
    assert result["spectral_rolloff"].value == 24000
    assert result["band_mid"].value == pytest.approx(0.2, abs=1e-12)


@pytest.mark.parametrize("n", [5, 6])
def test_last_bin_odd_and_even(n):
    rate = 48000
    frequency = (n // 2) * rate / n
    result = measures(loaded(cosine(rate, frequency, frames=n)))
    assert result["spectral_centroid"].value == pytest.approx(frequency, abs=1e-8)
    assert result["spectral_rolloff"].value == frequency


@pytest.mark.parametrize("rate,unknown", [(8000, ["high_mid", "high"]), (12000, ["high"]), (40000, [])])
def test_nyquist_partial_full_and_exact_high_endpoint(rate, unknown):
    result = measures(loaded(cosine(rate, 100), rate))
    for name in ("sub", "bass", "low_mid", "mid", "high_mid", "high"):
        item = result[f"band_{name}"]
        if name in unknown:
            assert item.value is None and item.unavailable_reason == "band_exceeds_nyquist"
        else:
            assert item.value == pytest.approx(float(name == "bass"), abs=1e-12)


def test_unobservable_band_energy_still_in_denominator():
    x = cosine(8000, 100) + cosine(8000, 3000)
    result = measures(loaded(x, 8000))
    assert result["band_high_mid"].unavailable_reason == "band_exceeds_nyquist"
    assert result["band_bass"].value == pytest.approx(0.5, abs=1e-12)


def test_no_bin_policy_and_nyquist_precedence():
    result = measures(loaded([1, -1], 8000))
    assert result["band_sub"].unavailable_reason == "insufficient_frequency_resolution"
    assert result["band_high_mid"].unavailable_reason == "band_exceeds_nyquist"
    assert result["band_high"].unavailable_reason == "band_exceeds_nyquist"
    assert result["spectral_centroid"].value == result["spectral_rolloff"].value == 4000


def test_silence_short_and_measured_zero():
    for values, reason in [([0], "silent_audio"), ([0, 0], "silent_audio"), ([1], "too_short_for_spectral_analysis")]:
        assert all(item.value is None and item.unavailable_reason == reason and item.confidence is None
                   for item in measures(loaded(values)).values())
    result = measures(loaded(np.ones(48000)))
    assert all(item.value == pytest.approx(0, abs=1e-12) for item in result.values())


@pytest.mark.parametrize("scale", [-2.5, 1e-300, 1e300])
def test_scale_and_finite_overrange_invariance(scale):
    x = cosine(48000, 40) + cosine(48000, 1000, 0.5)
    a, b = measures(loaded(x)), measures(loaded(x * scale))
    for name in a:
        assert b[name].value == pytest.approx(a[name].value, abs=1e-9)


@pytest.mark.parametrize("value", [np.finfo(float).max, np.nextafter(0.0, 1.0)])
def test_extreme_constants_remain_finite(value):
    result = measures(loaded(np.full(48000, value)))
    assert all(item.value == pytest.approx(0, abs=1e-12) for item in result.values())


def test_stereo_unequal_duplicate_and_opposite_polarity():
    a, b = cosine(48000, 100), cosine(48000, 1000, 2)
    unequal = measures(loaded(np.column_stack((a, b))))
    assert unequal["spectral_centroid"].value == pytest.approx((100 + 4*1000) / 5, abs=1e-8)
    assert unequal["band_bass"].value == pytest.approx(0.2, abs=1e-12)
    assert unequal["band_mid"].value == pytest.approx(0.8, abs=1e-12)
    for values in (np.column_stack((a, a)), np.column_stack((a, -a))):
        assert measures(loaded(values)) == measures(loaded(a))


@pytest.mark.parametrize("changes", [
    {"samples": np.array([])}, {"samples": np.ones((1, 3))}, {"samples": np.ones((0, 1))},
    {"samples": np.ones((1, 1, 1))}, {"samples": np.array([[float("nan")]])},
    {"samples": np.array([[float("inf")]])}, {"samples": np.array([[1]], dtype=int)},
    {"sample_rate_hz": 0}, {"sample_rate_hz": -1}, {"sample_rate_hz": 48000.0},
    {"sample_rate_hz": True}, {"sample_rate_hz": 2**31}, {"frame_count": 2},
    {"channels": 2}, {"duration_seconds": float("inf")}, {"duration_seconds": 1},
])
def test_invalid_input(changes):
    with pytest.raises(ValueError):
        measure_spectral(replace(loaded([1]), **changes))


def test_numerical_failure_has_explicit_reason(monkeypatch):
    # Controlled failure checks the defensive boundary, not a signal expectation.
    monkeypatch.setattr(np.fft, "rfft", lambda *args, **kwargs: np.full((2, 1), np.nan))
    assert all(item.value is None and item.unavailable_reason == "numerical_range"
               for item in measures(loaded([1, -1])).values())


def test_source_array_and_metadata_unchanged(tmp_path):
    from test_audio import wav
    path = tmp_path / "source.wav"
    original = wav([[0.5], [-0.5]])
    path.write_bytes(original)
    audio = load_wav(path)
    before = audio.samples.copy()
    metadata = (audio.channels, audio.frame_count, audio.sample_rate_hz, audio.duration_seconds)
    audio.samples.flags.writeable = False
    measure_spectral(audio)
    np.testing.assert_array_equal(audio.samples, before)
    assert (audio.channels, audio.frame_count, audio.sample_rate_hz, audio.duration_seconds) == metadata
    assert path.read_bytes() == original
    assert list(tmp_path.iterdir()) == [path]


def test_sample_contract_integration(tmp_path):
    audio = loaded(cosine(48000, 100))
    result = measure_spectral(audio)
    names = {item.name for item in result.measurements}
    unknowns = tuple(Measurement(name=name, value=None, unit=spec[0], unavailable_reason="not_implemented")
                     for name, spec in MEASURES.items() if name not in names)
    sample = Sample(sample_id="spectral-example", role="bass", analysis_version=result.analysis_version,
                    audio=AudioMetadata(local_path=str((tmp_path / "local.wav").resolve()), sample_rate_hz=48000,
                        channels=1, frame_count=48000, duration_ms=1000),
                    features=AudioFeatures(measurements=result.measurements + unknowns,
                        key=MusicalKey(tonic=None, mode=None, confidence=None, unavailable_reason="not_implemented")))
    assert Sample.from_json(sample.to_json()) == sample

def test_even_nyquist_stays_inside_strict_contract_bound():
    # Multiplying a rounded rate/N can overshoot Nyquist by one ULP at N=86.
    result = measures(loaded((-1.0)**np.arange(86), 44100))
    assert result["spectral_rolloff"].value == 22050
    assert result["spectral_centroid"].value <= 22050

