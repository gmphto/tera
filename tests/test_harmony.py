from dataclasses import replace
import math

import numpy as np
import pytest

from backend.analysis import harmony
from backend.audio import load_wav
from backend.contracts import AudioFeatures, AudioMetadata, MEASURES, Measurement, Sample
from harmony_reference_signals import MAJOR, MINOR, AMBIGUOUS, phrase, tone
from test_loudness import loaded


def estimate(values, rate=48000):
    result = harmony.measure_harmony(loaded(values, rate))
    assert result.analysis_version == harmony.ANALYSIS_VERSION
    return result


def cents(actual, expected):
    return abs(1200*math.log2(actual/expected))


@pytest.mark.parametrize("rate", [44100, 48000, 96000])
@pytest.mark.parametrize("frequency", [30, 40, 55, 110, 220, 440, 900, 1000])
def test_predeclared_stationary_range(rate, frequency):
    result = estimate(tone(frequency, rate), rate)
    assert result.fundamental.value is not None
    assert cents(result.fundamental.value, frequency) <= 10
    assert 0 <= result.fundamental.confidence <= 1
    assert result.key.tonic is None


@pytest.mark.parametrize("frequency", [20, 1500])
def test_outside_range_is_not_folded_into_domain(frequency):
    result = estimate(tone(frequency))
    assert result.fundamental.unavailable_reason == "pitch_out_of_range"


@pytest.mark.parametrize("rate", [44100, 48000, 96000])
def test_harmonic_rich_bass_and_octave_risk(rate):
    rich = tone(55, rate) + .5*tone(110, rate) + .25*tone(165, rate)
    assert cents(estimate(rich, rate).fundamental.value, 55) < 10
    for risk in (0.03*tone(55, rate) + tone(110, rate), tone(110, rate) + .3*tone(165, rate)):
        result = estimate(risk, rate).fundamental
        assert (result.value is not None and cents(result.value, 55) <= 10) or result.unavailable_reason == "octave_ambiguity"


@pytest.mark.parametrize("scale", [-2, 1e-300, 1e300])
def test_scale_and_polarity(scale):
    x = tone(110)
    result = estimate(np.column_stack((x*scale, -x*scale)))
    assert cents(result.fundamental.value, 110) < 10


def test_leading_trailing_silence_and_channel_disagreement():
    x = np.r_[np.zeros(14400), tone(110, duration=2), np.zeros(14400)]
    assert cents(estimate(x).fundamental.value, 110) < 10
    result = estimate(np.column_stack((tone(110), tone(147))))
    assert result.fundamental.unavailable_reason == "channel_disagreement"
    assert result.key.tonic is None


@pytest.mark.parametrize("kind", ["noise", "silence", "dc", "impulse", "kick", "sweep"])
def test_unreliable_aperiodic_or_percussive_inputs(kind):
    rng = np.random.default_rng(20260918)
    t = np.arange(48000)/48000
    signals = {
        "noise": rng.normal(0, .1, 48000), "silence": np.zeros(48000),
        "dc": np.full(48000, .5), "impulse": np.r_[1., np.zeros(47999)],
        "kick": (np.sin(2*np.pi*70*t)+rng.normal(0, .7, 48000))*np.exp(-t/0.03),
        "sweep": np.sin(2*np.pi*(130*t-50*t*t))*np.exp(-t/.2),
    }
    result = estimate(signals[kind])
    assert result.fundamental.value is None
    assert result.fundamental.unavailable_reason
    assert result.fundamental.confidence is None
    assert result.key.tonic is result.key.mode is result.key.confidence is None


@pytest.mark.parametrize("rate", [44100, 48000, 96000])
def test_three_complete_frame_boundary(rate):
    size = rate//5 + 2*(rate//20)
    result = estimate(tone(110, rate, duration=(size-1)/rate), rate)
    assert result.fundamental.unavailable_reason == "insufficient_active_frames"
    assert estimate(tone(110, rate, duration=size/rate), rate).fundamental.value is not None


@pytest.mark.parametrize("notes,transpose,label", [(MAJOR, 0, ("C", "major")),
                                                   (MINOR, 0, ("C", "minor")),
                                                   (MAJOR, 2, ("D", "major"))])
def test_independently_authored_labeled_phrase(notes, transpose, label):
    result = estimate(phrase(notes, transpose=transpose))
    assert (result.key.tonic, result.key.mode) == label
    assert 0 <= result.key.confidence <= 1
    assert result.fundamental.value is None
    assert result.evidence.key_fit >= .8
    assert result.evidence.key_separation >= .1


def test_independent_relative_key_ambiguity():
    result = estimate(phrase(AMBIGUOUS))
    assert result.key.tonic is None
    assert result.key.unavailable_reason in ("low_key_fit", "ambiguous_key")


@pytest.mark.parametrize("notes", [[(57, 10)], [(57, 2), (69, 2)] * 3, [(60, 2), (64, 2), (67, 2)] * 2])
def test_single_tone_octaves_and_triad_do_not_establish_key(notes):
    result = estimate(phrase(notes))
    assert result.key.unavailable_reason == "insufficient_pitch_class_diversity"


def test_activity_coverage_and_stability_boundaries(monkeypatch):
    # Controlled per-frame evidence isolates hard aggregation gates; the actual
    # autocorrelation has independent waveform fixtures above.
    values = tone(110, duration=.65)  # exactly ten complete active frames
    def with_evidence(frequencies):
        iterator = iter(frequencies)
        def frame(*args):
            frequency = next(iterator)
            return (frequency, .95, None) if frequency else (None, None, "insufficient_periodicity")
        monkeypatch.setattr(harmony, "_frame_pitch", frame)
        return estimate(values)
    assert with_evidence([110]*8+[None]*2).fundamental.value is not None
    assert with_evidence([110]*7+[None]*3).fundamental.unavailable_reason == "insufficient_periodicity"
    assert with_evidence([110*2**(-35/1200)]*5+[110*2**(35/1200)]*5).fundamental.value is not None
    assert with_evidence([110*2**(-36/1200)]*5+[110*2**(36/1200)]*5).fundamental.unavailable_reason == "unstable_pitch"


def test_key_duration_coverage_and_histogram_boundaries():
    pitches = [440*2**((note-69)/12) for note, beats in MAJOR for _ in range(beats)]
    assert harmony._key(pitches, .6, 4)[0].tonic == "C"
    assert harmony._key(pitches, .5999, 4)[0].unavailable_reason == "insufficient_key_pitch_coverage"
    assert harmony._key(pitches, 1, 4-1/48000)[0].unavailable_reason == "insufficient_key_context"
    chromatic = [440*2**((note-69)/12) for note in range(60,72)]
    assert harmony._key(chromatic, 1, 4)[0].unavailable_reason == "ambiguous_key_histogram"


def test_key_fit_and_separation_thresholds(monkeypatch):
    pitches = [440*2**((note-69)/12) for note in (60,62,64,65,67)]
    def classified(best, second):
        scores = iter([best, second] + [-.5]*22)
        def dot(centered, profile):
            return next(scores)*np.linalg.norm(centered)*np.linalg.norm(profile)
        monkeypatch.setattr(np, "dot", dot)
        return harmony._key(pitches, 1, 4)[0]
    assert classified(.8, .7).tonic is not None
    assert classified(.799, .6).unavailable_reason == "low_key_fit"
    assert classified(.9, .801).unavailable_reason == "ambiguous_key"
    assert classified(.9, .9).unavailable_reason == "ambiguous_key"


def test_unsupported_rate_silence_and_extreme_constant():
    assert estimate([0], 8000).fundamental.unavailable_reason == "silent_audio"
    assert estimate([1], 8000).fundamental.unavailable_reason == "unsupported_harmony_sample_rate"
    for value in (np.finfo(float).max, np.nextafter(0., 1.)):
        result = estimate(np.full(48000, value))
        assert result.fundamental.value is None


@pytest.mark.parametrize("changes", [
    {"samples": np.array([])}, {"samples": np.ones((1,3))}, {"samples": np.ones((0,1))},
    {"samples": np.array([[float("nan")]])}, {"samples": np.array([[float("inf")]])},
    {"sample_rate_hz": 0}, {"sample_rate_hz": -1}, {"sample_rate_hz": 48000.0},
    {"frame_count": 2}, {"channels": 2}, {"duration_seconds": float("inf")},
])
def test_invalid_input(changes):
    with pytest.raises(ValueError):
        harmony.measure_harmony(replace(loaded([1]), **changes))


def test_source_unchanged_and_sample_integration(tmp_path):
    from test_audio import wav
    path = tmp_path/"source.wav"
    original = wav(tone(110)[:,None], subtype="DOUBLE")
    path.write_bytes(original)
    audio = load_wav(path)
    before = audio.samples.copy()
    audio.samples.flags.writeable = False
    result = harmony.measure_harmony(audio)
    np.testing.assert_array_equal(audio.samples, before)
    assert path.read_bytes() == original
    assert list(tmp_path.iterdir()) == [path]
    unknowns = tuple(Measurement(name=name, value=None, unit=spec[0], unavailable_reason="not_implemented")
                     for name, spec in MEASURES.items() if name != "fundamental")
    sample = Sample(sample_id="bass-example", role="bass", analysis_version=result.analysis_version,
                    audio=AudioMetadata(local_path=str(path.resolve()), sample_rate_hz=48000, channels=1,
                                        frame_count=48000, duration_ms=1000),
                    features=AudioFeatures(measurements=(result.fundamental,)+unknowns, key=result.key))
    assert Sample.from_json(sample.to_json()) == sample
