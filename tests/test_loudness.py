from dataclasses import replace
import math

import numpy as np
import pytest

from backend.analysis.loudness import ANALYSIS_VERSION, measure_loudness
from backend.audio import LoadedAudio, load_wav
from backend.contracts import AudioFeatures, AudioMetadata, MEASURES, Measurement, MusicalKey, Sample
from loudness_reference_signals import EBU_CASES, ebu_signal, tone


def loaded(values, rate=48000):
    x = np.array(values, dtype=np.float64)
    if x.ndim == 1:
        x = x[:, None]
    return LoadedAudio(x, x.shape[1], rate, len(x), len(x) / rate, "DOUBLE")


def measures(audio):
    result = measure_loudness(audio)
    assert result.analysis_version == ANALYSIS_VERSION
    return {m.name: m for m in result.measurements}


@pytest.mark.parametrize("values,expected", [
    ([[0.25]] * 8, (0.25, 0.25, 1)),
    ([[-0.25]] * 8, (0.25, 0.25, 1)),
    ([[2.0]], (2, 2, 1)),
    ([[0.25, -0.25]] * 8, (0.25, 0.25, 1)),
    ([[0.25, 0.5]] * 8, (math.sqrt((0.25**2 + 0.5**2) / 2), 0.5, math.sqrt(1.6))),
    ([1, 0, 0, 0], (0.5, 1, 2)),
])
def test_analytic_amplitude_fixtures(values, expected):
    result = measures(loaded(values))
    for name, value in zip(("rms", "peak", "crest_factor"), expected):
        assert result[name].value == pytest.approx(value, rel=1e-14)


@pytest.mark.parametrize("scale", [0.125, -2.5, 1e-200, 1e200])
def test_sine_and_scaling(scale):
    x = tone(48000, peak=0.5)
    result = measures(loaded(x * scale))
    expected_peak = np.max(np.abs(x)) * abs(scale)
    assert result["rms"].value == pytest.approx(0.5 * abs(scale) / math.sqrt(2), rel=1e-13, abs=0)
    assert result["peak"].value == pytest.approx(expected_peak, rel=1e-14, abs=0)
    assert result["crest_factor"].value == pytest.approx(math.sqrt(2), rel=1e-13)


def test_silence_precedes_short_duration_and_unsupported_rate():
    for audio in (loaded([0]), loaded(np.zeros(8000), 8000)):
        result = measures(audio)
        assert result["rms"].value == result["peak"].value == 0
        for name in ("crest_factor", "loudness"):
            assert result[name].value is None
            assert result[name].unavailable_reason == "silent_audio"
            assert result[name].confidence is None


@pytest.mark.parametrize("rate", [44100, 48000, 96000])
@pytest.mark.parametrize("delta", [-1, 0, 1])
def test_integrated_boundary_and_tail_policy(rate, delta):
    size = rate * 4 // 10 + delta
    values = tone(rate, (size + 1) / rate)[:size]
    result = measures(loaded(values, rate))["loudness"]
    if delta < 0:
        assert result.unavailable_reason == "too_short_for_integrated_loudness"
    else:
        assert result.value is not None
        exact = measures(loaded(values[:rate * 4 // 10], rate))["loudness"]
        assert result.value == exact.value


def test_unsupported_rate_keeps_linear_facts_and_duration_precedence():
    result = measures(loaded(tone(32000), 32000))
    assert result["loudness"].unavailable_reason == "unsupported_loudness_sample_rate"
    assert all(result[name].value is not None for name in ("rms", "peak", "crest_factor"))
    assert measures(loaded([1], 32000))["loudness"].unavailable_reason == "too_short_for_integrated_loudness"


def test_below_gate_and_numerical_limits():
    for amplitude in (1e-6, 1e-200, np.nextafter(0.0, 1.0)):
        result = measures(loaded(np.full(48000, amplitude)))
        assert result["peak"].value == result["rms"].value == amplitude
        assert result["loudness"].unavailable_reason == "below_loudness_gate"
    x = np.zeros(48000)
    x[0] = np.nextafter(0.0, 1.0)
    result = measures(loaded(x))
    assert result["rms"].unavailable_reason == "numerical_range"
    assert result["crest_factor"].value == pytest.approx(math.sqrt(48000))
    result = measures(loaded(np.full(48000, np.finfo(float).max)))
    assert all(item.value is None or math.isfinite(item.value) for item in result.values())
    assert result["peak"].value == result["rms"].value == np.finfo(float).max


@pytest.mark.parametrize("case", EBU_CASES)
def test_ebu_published_integrated_references(case):
    result = measures(loaded(ebu_signal(case, 48000)))["loudness"]
    assert result.value == pytest.approx(EBU_CASES[case][1], abs=0.1)


@pytest.mark.parametrize("rate", [44100, 96000])
@pytest.mark.parametrize("case", [1, 4])
def test_rate_equivalent_reference_signals(rate, case):
    result = measures(loaded(ebu_signal(case, rate), rate))["loudness"]
    assert result.value == pytest.approx(EBU_CASES[case][1], abs=0.1)


@pytest.mark.parametrize("rate", [44100, 48000, 96000])
def test_stereo_duplication_and_polarity(rate):
    mono = tone(rate)
    a = measures(loaded(mono, rate))
    b = measures(loaded(np.column_stack((mono, mono)), rate))
    c = measures(loaded(np.column_stack((mono, -mono)), rate))
    assert b["loudness"].value - a["loudness"].value == pytest.approx(10 * math.log10(2), abs=1e-10)
    for name in b:
        assert b[name].value == c[name].value


@pytest.mark.parametrize("rate", [44100, 48000, 96000])
@pytest.mark.parametrize("frequency", [40, 100, 1000, 10000])
def test_frequency_weighting_against_published_48k_transfer(rate, frequency):
    # Independent direct complex evaluation of ITU Tables 1/2; no production
    # coefficient generator or filtering code used to derive the expectation.
    z = np.exp(-2j * np.pi * frequency / 48000)
    h1 = (1.53512485958697 - 2.69169618940638*z + 1.19839281085285*z*z) / (1 - 1.69065929318241*z + 0.73248077421585*z*z)
    h2 = (1 - 2*z + z*z) / (1 - 1.99004745483398*z + 0.99007225036621*z*z)
    expected = -0.691 + 20 * math.log10(0.1 * abs(h1*h2))
    mono = tone(rate, seconds=10, peak=0.1, frequency=frequency)
    actual = measures(loaded(np.column_stack((mono, mono)), rate))["loudness"].value
    assert actual == pytest.approx(expected, abs=0.1)


@pytest.mark.parametrize("changes", [
    {"samples": np.array([])}, {"samples": np.ones((1, 3))},
    {"samples": np.ones((0, 1))}, {"samples": np.ones((1, 1, 1))},
    {"samples": np.array([[float("nan")]])}, {"samples": np.array([[float("inf")]])},
    {"samples": np.array([[1]], dtype=int)}, {"sample_rate_hz": 0},
    {"sample_rate_hz": -48000}, {"sample_rate_hz": 48000.0}, {"sample_rate_hz": True},
    {"frame_count": 2}, {"channels": 2}, {"duration_seconds": float("inf")},
    {"duration_seconds": 1},
])
def test_invalid_callable_boundary(changes):
    with pytest.raises(ValueError):
        measure_loudness(replace(loaded([1]), **changes))


def test_input_array_and_source_unchanged(tmp_path):
    from test_audio import wav
    path = tmp_path / "source.wav"
    original = wav([[0.5], [-0.5]])
    path.write_bytes(original)
    audio = load_wav(path)
    before = audio.samples.copy()
    audio.samples.flags.writeable = False
    measure_loudness(audio)
    np.testing.assert_array_equal(audio.samples, before)
    assert path.read_bytes() == original
    assert list(tmp_path.iterdir()) == [path]


def test_feature_contract_integration(tmp_path):
    audio = loaded(tone(48000))
    result = measure_loudness(audio)
    names = {item.name for item in result.measurements}
    unknowns = tuple(Measurement(name=name, value=None, unit=spec[0], unavailable_reason="not_implemented")
                     for name, spec in MEASURES.items() if name not in names)
    sample = Sample(sample_id="loudness-example", role="kick",
                    audio=AudioMetadata(local_path=str((tmp_path / "local.wav").resolve()),
                        sample_rate_hz=audio.sample_rate_hz, channels=audio.channels,
                        frame_count=audio.frame_count, duration_ms=audio.duration_seconds * 1000),
                    features=AudioFeatures(measurements=result.measurements + unknowns,
                        key=MusicalKey(tonic=None, mode=None, confidence=None, unavailable_reason="not_implemented")),
                    analysis_version=result.analysis_version)
    assert Sample.from_json(sample.to_json()) == sample
