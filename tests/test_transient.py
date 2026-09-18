from dataclasses import replace
import math

import numpy as np
import pytest

from backend.analysis.transient import ANALYSIS_VERSION, measure_transient
from backend.audio import load_wav
from backend.contracts import AudioFeatures, AudioMetadata, MEASURES, Measurement, MusicalKey, Sample
from test_loudness import loaded


def block_signal(envelope, rate=48000, channels=(1,)):
    # Independent synthetic piecewise-constant amplitudes. Block expectations
    # below are declared by hand; no production envelope helper is used.
    values = np.concatenate([np.full(((k + 1) * rate // 200) - (k * rate // 200), value)
                             for k, value in enumerate(envelope)])
    return loaded(np.column_stack([values * gain for gain in channels]), rate)


def measures(audio):
    result = measure_transient(audio)
    assert result.analysis_version == ANALYSIS_VERSION
    assert len(result.measurements) == 4
    return {item.name: item for item in result.measurements}


def all_unknown(audio, reason):
    result = measures(audio)
    assert all(item.value is None and item.unavailable_reason == reason and item.confidence is None
               for item in result.values())


@pytest.mark.parametrize("rate", [44100, 48000, 96000])
@pytest.mark.parametrize("envelope,expected", [
    ([0, 0, .1, .3, .6, .9, 1, .5, .1, .1], (.3, 20, 15, 10)),
    ([0, .1, .5, 1, 1, .5, .1, .1], (.5, 15, 10, 15)),
    ([0, .25, .5, .75, 1, .1, .1], (.25, 5, 15, 5)),
    ([0, .1, .9, 1, .1, .1], (.8, 10, 5, 5)),
])
def test_declared_gradual_plateau_tie_and_threshold_expectations(rate, envelope, expected):
    result = measures(block_signal(envelope, rate))
    for name, value in zip(("transient_strength", "transient_position", "attack", "decay"), expected):
        tolerance = 1e-12 if name == "transient_strength" else 1000 / rate + 1e-10
        assert result[name].value == pytest.approx(value, abs=tolerance)


@pytest.mark.parametrize("rate", [44100, 48000, 96000])
def test_leading_silence_impulse(rate):
    x = np.zeros(5 * rate // 200)
    x[2 * rate // 200] = 1
    result = measures(loaded(x, rate))
    assert result["transient_strength"].value == 1
    assert result["transient_position"].value == pytest.approx(10, abs=1000/rate)
    assert result["attack"].value == 0
    assert result["decay"].value == pytest.approx(5, abs=1000/rate)


@pytest.mark.parametrize("rate", [44100, 48000, 96000])
def test_exponential_block_decay(rate):
    # Peak at block2; exp(-k/4) first falls <=.1 at k=10, confirmed k=11.
    envelope = [0, 0] + [math.exp(-k / 4) for k in range(13)]
    result = measures(block_signal(envelope, rate))
    assert result["attack"].value == 0
    assert result["transient_strength"].value == 1
    assert result["decay"].value == pytest.approx(50, abs=1000/rate)


@pytest.mark.parametrize("tail", [[.2] * 20, [.1], [.1, .2], []])
def test_truncated_decay_requires_two_complete_confirmations(tail):
    result = measures(block_signal([0, 1] + tail))
    assert result["decay"].value is None
    assert result["decay"].unavailable_reason == "truncated_decay"


def test_long_tail_observed_threshold_not_remaining_file_length():
    result = measures(block_signal([0, 1] + [.5] * 30 + [.1, .1] + [0] * 100))
    assert result["decay"].value == 155


def test_left_truncated_attack_still_reports_zero_state_transient():
    result = measures(block_signal([1, .5, .1, .1]))
    assert result["attack"].unavailable_reason == "left_truncated_attack"
    assert result["transient_position"].value == 0
    assert result["transient_strength"].value == 1
    assert result["decay"].value == 10


@pytest.mark.parametrize("envelope", [
    [0, 1, .1, .1, .5, .1, .1],
    [0, .5, 0, 0, 1, 0, 0],
    [0, .1, .1, .1, .5, 1, 0, 0],
])
def test_separated_repeated_onsets(envelope):
    all_unknown(block_signal(envelope), "multiple_onsets")


@pytest.mark.parametrize("envelope", [
    [0, 1, 0, .5, 0, 0],  # only one separating low block
    [0, 1, 0, 0, .49, 0, 0],  # second activity never reaches50%
])
def test_conservative_separation_threshold(envelope):
    assert any(item.value is not None for item in measures(block_signal(envelope)).values())


@pytest.mark.parametrize("channels", [(1,), (1, 1), (1, -1), (1, .25)])
@pytest.mark.parametrize("scale", [1, -2.5, 1e-300, 1e300])
def test_channels_polarity_and_scale_invariance(channels, scale):
    envelope = [0, .25, .5, 1, .05, .05]
    baseline = measures(block_signal(envelope))
    audio = block_signal(envelope, channels=tuple(gain*scale for gain in channels))
    result = measures(audio)
    for name in result:
        assert result[name].value == pytest.approx(baseline[name].value, abs=1e-12)


def test_unequal_channels_use_combined_rms_not_channel_max():
    # L/R energy combinations yield normalized envelope [0,.5,1,0,0].
    audio = block_signal([0, .5, 1, 0, 0], channels=(1, 1))
    audio.samples[240:480, 1] = 0
    result = measures(audio)
    expected_mid = .5 / math.sqrt(2)
    assert result["transient_strength"].value == pytest.approx(1-expected_mid, abs=1e-12)
    assert result["transient_position"].value == 10


@pytest.mark.parametrize("rate", [44100, 48000, 96000])
def test_partial_tail_cannot_condition_or_confirm_decay(rate):
    audio = block_signal([0, 1, .05], rate)
    remaining = (4*rate//200) - len(audio.samples) - 1
    values = np.concatenate((audio.samples[:, 0], np.zeros(remaining)))
    values[-1] = 1e308
    result = measures(loaded(values, rate))
    baseline = measures(audio)
    assert result == baseline
    assert result["decay"].unavailable_reason == "truncated_decay"


def test_availability_precedence_and_floor_boundaries():
    all_unknown(loaded([0], 100), "silent_audio")
    all_unknown(loaded([1], 100), "unsupported_envelope_sample_rate")
    all_unknown(loaded([1]), "too_short_for_envelope_analysis")
    all_unknown(loaded(np.ones(440), 44100), "too_short_for_envelope_analysis")
    assert measures(loaded(np.ones(441), 44100))["attack"].unavailable_reason == "left_truncated_attack"
    # At201Hz, floor(2*201/200)=2: two frames complete two blocks.
    assert measures(loaded([0, 1], 201))["attack"].value == 0
    all_unknown(loaded(np.r_[np.zeros(480), 1]), "insufficient_observed_audio")


@pytest.mark.parametrize("scale", [np.finfo(float).max, np.nextafter(0.0, 1.0)])
def test_extreme_block_impulse(scale):
    audio = block_signal([0, 1, 0, 0])
    result = measures(replace(audio, samples=audio.samples * scale))
    assert [result[name].value for name in ("transient_strength", "transient_position", "attack", "decay")] == [1, 5, 0, 5]


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
        measure_transient(replace(loaded([1]), **changes))


def test_source_array_and_metadata_unchanged(tmp_path):
    from test_audio import wav
    path = tmp_path / "source.wav"
    original = wav(block_signal([0, .5, 0, 0]).samples)
    path.write_bytes(original)
    audio = load_wav(path)
    before = audio.samples.copy()
    metadata = (audio.channels, audio.frame_count, audio.sample_rate_hz, audio.duration_seconds)
    audio.samples.flags.writeable = False
    measure_transient(audio)
    np.testing.assert_array_equal(audio.samples, before)
    assert (audio.channels, audio.frame_count, audio.sample_rate_hz, audio.duration_seconds) == metadata
    assert path.read_bytes() == original
    assert list(tmp_path.iterdir()) == [path]


def test_sample_contract_integration(tmp_path):
    audio = block_signal([0, .5, 1, 0, 0])
    result = measure_transient(audio)
    names = {item.name for item in result.measurements}
    unknowns = tuple(Measurement(name=name, value=None, unit=spec[0], unavailable_reason="not_implemented")
                     for name, spec in MEASURES.items() if name not in names)
    sample = Sample(sample_id="transient-example", role="kick", analysis_version=result.analysis_version,
                    audio=AudioMetadata(local_path=str((tmp_path / "local.wav").resolve()), sample_rate_hz=48000,
                        channels=1, frame_count=audio.frame_count, duration_ms=audio.duration_seconds*1000),
                    features=AudioFeatures(measurements=result.measurements + unknowns,
                        key=MusicalKey(tonic=None, mode=None, confidence=None, unavailable_reason="not_implemented")))
    assert Sample.from_json(sample.to_json()) == sample
