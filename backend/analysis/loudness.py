"""Versioned linear measurements and validated mono/stereo BS.1770 loudness."""

from dataclasses import dataclass
import math

import numpy as np
from scipy import signal

from backend.audio import LoadedAudio
from backend.contracts import Measurement


ANALYSIS_VERSION = "loudness-bs1770-5-v1"
SUPPORTED_LOUDNESS_RATES = (44100, 48000, 96000)

# ITU-R BS.1770-5 Annex 1 Tables 1 and 2, 48 kHz.
_REFERENCE_SECTIONS = (
    ([1.53512485958697, -2.69169618940638, 1.19839281085285],
     [1.0, -1.69065929318241, 0.73248077421585]),
    ([1.0, -2.0, 1.0], [1.0, -1.99004745483398, 0.99007225036621]),
)


@dataclass(frozen=True)
class LoudnessMeasurements:
    analysis_version: str
    measurements: tuple[Measurement, ...]


def _measurement(name, value=None, reason=None):
    unit = {"rms": "linear", "peak": "linear", "crest_factor": "ratio", "loudness": "LUFS"}[name]
    return Measurement(name=name, value=value, unit=unit, unavailable_reason=reason)


def _k_weighting(rate):
    sections = []
    for numerator, denominator in _REFERENCE_SECTIONS:
        if rate == 48000:
            b, a = numerator, denominator
        else:
            # Invert the reference bilinear substitution z=(2F+s)/(2F-s),
            # then discretize the same analogue transfer at the target rate.
            def analogue(coeff):
                c0, c1, c2 = coeff
                scale = 2 * 48000
                return [c0 - c1 + c2, 2 * scale * (c0 - c2), scale**2 * (c0 + c1 + c2)]
            b, a = signal.bilinear(analogue(numerator), analogue(denominator), fs=rate)
        sections.append([*b, *a])
    return np.array(sections, dtype=np.float64)


def _integrated(samples, rate):
    # Precedence after silence: duration, then validated-rate support.
    if len(samples) < math.ceil(0.4 * rate):
        return _measurement("loudness", reason="too_short_for_integrated_loudness")
    if rate not in SUPPORTED_LOUDNESS_RATES:
        return _measurement("loudness", reason="unsupported_loudness_sample_rate")
    block, hop = rate * 4 // 10, rate // 10
    end = ((len(samples) - block) // hop) * hop + block
    complete = samples[:end]
    # Discarded tail samples must not influence even numerical conditioning:
    # an extreme tail peak could underflow a valid retained block's energy.
    # Linear RMS/peak/crest still use every original sample independently.
    peak = float(np.max(np.abs(complete)))
    if peak == 0:
        return _measurement("loudness", reason="below_loudness_gate")
    normalized = complete / peak
    filtered = signal.sosfilt(_k_weighting(rate), normalized, axis=0)
    # Mono, left and right all have unit weights; never sum the waveforms.
    energy = np.sum(filtered * filtered, axis=1)
    energies = np.array([np.mean(energy[start:start + block])
                         for start in range(0, len(energy) - block + 1, hop)])
    positive = energies > 0
    levels = np.full(len(energies), -np.inf)
    gain_db = 20 * math.log10(peak)
    levels[positive] = -0.691 + 10 * np.log10(energies[positive]) + gain_db
    absolute = levels > -70.0
    if not absolute.any():
        return _measurement("loudness", reason="below_loudness_gate")
    relative = -0.691 + 10 * math.log10(float(np.mean(energies[absolute]))) + gain_db - 10
    selected = absolute & (levels > relative)
    value = -0.691 + 10 * math.log10(float(np.mean(energies[selected]))) + gain_db
    return _measurement("loudness", value=float(value))


def measure_loudness(audio: LoadedAudio) -> LoudnessMeasurements:
    """Return rms, peak, crest_factor and loudness without modifying input.

    Raises ValueError for malformed LoadedAudio. See _docs/loudness.md for
    units, numerical limits, availability precedence and calibration evidence.
    """
    if not isinstance(audio, LoadedAudio):
        raise ValueError("Expected LoadedAudio from the local WAV reader.")
    x = audio.samples
    if not isinstance(x, np.ndarray) or x.dtype.kind != "f" or x.ndim != 2 or x.shape[0] == 0 or x.shape[1] not in (1, 2):
        raise ValueError("Expected a nonempty floating-point frames-by-mono/stereo array.")
    if type(audio.sample_rate_hz) is not int or audio.sample_rate_hz <= 0:
        raise ValueError("Sample rate must be a positive integer in Hz.")
    if (type(audio.frame_count) is not int or type(audio.channels) is not int
            or (audio.frame_count, audio.channels) != x.shape):
        raise ValueError("Audio metadata must match the decoded array shape.")
    if not isinstance(audio.duration_seconds, (int, float)) or not math.isfinite(audio.duration_seconds) or not math.isclose(audio.duration_seconds, len(x) / audio.sample_rate_hz, rel_tol=0, abs_tol=1e-12):
        raise ValueError("Audio duration must agree with frames and sample rate.")
    if not np.isfinite(x).all():
        raise ValueError("Audio samples must be finite.")
    x = np.asarray(x, dtype=np.float64)
    peak = float(np.max(np.abs(x)))
    if peak == 0:
        measurements = (_measurement("rms", 0.0), _measurement("peak", 0.0),
                        _measurement("crest_factor", reason="silent_audio"),
                        _measurement("loudness", reason="silent_audio"))
    else:
        # This is computational scaling only: retain the physical gain in the
        # returned amplitude and in log-space LUFS; no source array is modified.
        normalized = x / peak
        normalized_rms = min(1.0, float(np.sqrt(np.mean(normalized * normalized))))
        rms = peak * normalized_rms
        measurements = (
            _measurement("rms", rms) if rms > 0 else _measurement("rms", reason="numerical_range"),
            _measurement("peak", peak),
            _measurement("crest_factor", max(1.0, 1 / normalized_rms)),
            _integrated(x, audio.sample_rate_hz),
        )
    return LoudnessMeasurements(ANALYSIS_VERSION, measurements)
