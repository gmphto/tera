"""Observed block-envelope descriptors; see _docs/transient.md."""

from dataclasses import dataclass
import math

import numpy as np

from backend.audio import LoadedAudio
from backend.contracts import Measurement


ANALYSIS_VERSION = "transient-envelope-v1"
_NAMES = ("transient_strength", "transient_position", "attack", "decay")
_ROUNDING = 8 * np.finfo(np.float64).eps


@dataclass(frozen=True)
class TransientMeasurements:
    analysis_version: str
    measurements: tuple[Measurement, ...]


def _measurement(name, value=None, reason=None):
    return Measurement(name=name, value=value, unit="normalized" if name == "transient_strength" else "ms",
                       unavailable_reason=reason)


def _unknown(reason):
    return TransientMeasurements(ANALYSIS_VERSION, tuple(_measurement(name, reason=reason) for name in _NAMES))


def _at_least(value, threshold):
    return value >= threshold - _ROUNDING


def _at_most(value, threshold):
    return value <= threshold + _ROUNDING


def _multiple_onsets(envelope):
    active, separated, low_count = False, False, 0
    for value in envelope:
        if separated and _at_least(value, 0.5):
            return True
        if active:
            low_count = low_count + 1 if _at_most(value, 0.1) else 0
            separated = separated or low_count >= 2
        elif _at_least(value, 0.1):
            # This block establishes activity; quiet blocks must follow it.
            active = True
    return False


def measure_transient(audio: LoadedAudio) -> TransientMeasurements:
    """Measure a single observed event; invalid LoadedAudio raises ValueError."""
    if not isinstance(audio, LoadedAudio):
        raise ValueError("Expected LoadedAudio from the local WAV reader.")
    x = audio.samples
    if not isinstance(x, np.ndarray) or x.dtype.kind != "f" or x.ndim != 2 or not len(x) or x.shape[1] not in (1, 2):
        raise ValueError("Expected a nonempty floating-point frames-by-mono/stereo array.")
    rate = audio.sample_rate_hz
    if type(rate) is not int or not 0 < rate <= 2**31 - 1:
        raise ValueError("Sample rate must be a positive reader-supported integer in Hz.")
    if type(audio.channels) is not int or type(audio.frame_count) is not int or (audio.frame_count, audio.channels) != x.shape:
        raise ValueError("Audio metadata must match the decoded array shape.")
    if type(audio.duration_seconds) not in (int, float) or not math.isfinite(audio.duration_seconds) or not math.isclose(audio.duration_seconds, len(x)/rate, rel_tol=0, abs_tol=1e-12):
        raise ValueError("Audio duration must agree with frames and sample rate.")
    if not np.isfinite(x).all():
        raise ValueError("Audio samples must be finite.")
    if not np.any(x):
        return _unknown("silent_audio")
    if rate < 200:
        return _unknown("unsupported_envelope_sample_rate")
    # Largest k whose floor(k*rate/200) <= frame_count; unlike floor(N*200/rate),
    # this includes a block completed on a downward-rounded boundary.
    count = ((len(x) + 1) * 200 - 1) // rate
    if count < 2:
        return _unknown("too_short_for_envelope_analysis")
    boundaries = np.arange(count + 1, dtype=np.int64) * rate // 200
    complete = x[:boundaries[-1]]
    scale = np.max(np.abs(complete))
    if scale == 0:
        return _unknown("insufficient_observed_audio")
    with np.errstate(over="ignore", under="ignore", invalid="ignore"):
        normalized = np.asarray(complete / scale, dtype=np.float64)
        envelope = np.zeros(count)
        for index, (start, end) in enumerate(zip(boundaries[:-1], boundaries[1:])):
            block = normalized[start:end]
            block_peak = float(np.max(np.abs(block)))
            if block_peak:
                # A second bounded scale avoids squaring tiny block values and
                # keeps constant-block RMS/threshold equality numerically stable.
                envelope[index] = block_peak * np.sqrt(np.mean((block / block_peak)**2))
        maximum = float(np.max(envelope))
        if not np.isfinite(envelope).all() or not math.isfinite(maximum) or maximum <= 0:
            return _unknown("numerical_range")
        envelope /= maximum
    if _multiple_onsets(envelope):
        return _unknown("multiple_onsets")
    peak = int(np.flatnonzero(envelope >= 1 - _ROUNDING)[0])
    onset = int(np.flatnonzero(_at_least(envelope[:peak + 1], 0.1))[0])
    high = int(np.flatnonzero(_at_least(envelope[onset:peak + 1], 0.9))[0]) + onset
    increments = np.diff(envelope, prepend=0.0)
    strongest = float(np.max(increments[onset:peak + 1]))
    position = onset + int(np.flatnonzero(increments[onset:peak + 1] >= strongest - _ROUNDING)[0])
    attack = (_measurement("attack", reason="left_truncated_attack") if _at_least(envelope[0], 0.1)
              else _measurement("attack", float((boundaries[high] - boundaries[onset]) * 1000 / rate)))
    decay = _measurement("decay", reason="truncated_decay")
    for index in range(peak + 1, count - 1):
        if _at_most(envelope[index], 0.1) and _at_most(envelope[index + 1], 0.1):
            decay = _measurement("decay", float((boundaries[index] - boundaries[peak]) * 1000 / rate))
            break
    return TransientMeasurements(ANALYSIS_VERSION, (
        _measurement("transient_strength", min(1.0, max(0.0, strongest))),
        _measurement("transient_position", float(boundaries[position] * 1000 / rate)),
        attack, decay,
    ))
