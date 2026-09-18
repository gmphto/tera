"""Versioned full-clip power spectra; see _docs/spectral.md."""

from dataclasses import dataclass
import math

import numpy as np

from backend.audio import LoadedAudio
from backend.contracts import Measurement


ANALYSIS_VERSION = "spectral-power-v1"
BANDS = (
    ("sub", 20, 60), ("bass", 60, 250), ("low_mid", 250, 500),
    ("mid", 500, 2000), ("high_mid", 2000, 6000), ("high", 6000, 20000),
)
_NAMES = ("spectral_centroid", "spectral_rolloff", *(f"band_{name}" for name, _, _ in BANDS))


@dataclass(frozen=True)
class SpectralMeasurements:
    analysis_version: str
    measurements: tuple[Measurement, ...]


def _measurement(name, value=None, reason=None):
    return Measurement(name=name, value=value, unit="Hz" if name.startswith("spectral_") else "ratio",
                       unavailable_reason=reason)


def _unknown(reason):
    return SpectralMeasurements(ANALYSIS_VERSION, tuple(_measurement(name, reason=reason) for name in _NAMES))


def _one_sided_power(normalized):
    """Power sums to time-domain mean square, averaged over channels."""
    spectrum = np.fft.rfft(normalized, axis=0, norm="forward")
    power = np.abs(spectrum)**2
    power[1:-1 if len(normalized) % 2 == 0 else None] *= 2
    return np.mean(power, axis=1)


def measure_spectral(audio: LoadedAudio) -> SpectralMeasurements:
    """Return centroid, 85% rolloff and six band ratios without modifying audio.

    Invalid input raises ValueError. Availability/numerical policies and the
    rectangular-window limitation are documented in _docs/spectral.md.
    """
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
    if type(audio.duration_seconds) not in (int, float) or not math.isfinite(audio.duration_seconds) or not math.isclose(audio.duration_seconds, len(x) / rate, rel_tol=0, abs_tol=1e-12):
        raise ValueError("Audio duration must agree with frames and sample rate.")
    if not np.isfinite(x).all():
        raise ValueError("Audio samples must be finite.")
    if not np.any(x):
        return _unknown("silent_audio")
    if len(x) < 2:
        return _unknown("too_short_for_spectral_analysis")
    # Peak conditioning precedes the FFT and squaring; ratios restore no gain.
    # The source may be a read-only array. All calculations use new arrays.
    with np.errstate(over="ignore", under="ignore", invalid="ignore"):
        scaled = x / np.max(np.abs(x))
        normalized = np.asarray(scaled, dtype=np.float64)
        power = _one_sided_power(normalized)
    total = float(np.sum(power))
    if not np.isfinite(power).all() or not math.isfinite(total) or total <= 0:
        return _unknown("numerical_range")
    fractions = power / total
    n = len(x)
    bins = np.arange(len(power), dtype=np.int64)
    centres = bins * rate
    frequencies = centres / n
    centroid = float(np.sum(frequencies * fractions))
    cumulative = np.cumsum(fractions)
    rolloff_index = min(int(np.searchsorted(cumulative, 0.85, side="left")), len(power) - 1)
    measurements = [_measurement("spectral_centroid", min(centroid, rate / 2)),
                    _measurement("spectral_rolloff", float(frequencies[rolloff_index]))]
    # Integer comparisons avoid a rounded bin centre crossing an exact edge.
    for name, low, high in BANDS:
        key = f"band_{name}"
        if 2 * high > rate:
            measurements.append(_measurement(key, reason="band_exceeds_nyquist"))
            continue
        mask = (centres >= low * n) & ((centres <= high * n) if high == 20000 else (centres < high * n))
        if not mask.any():
            measurements.append(_measurement(key, reason="insufficient_frequency_resolution"))
        else:
            measurements.append(_measurement(key, min(1.0, float(np.sum(fractions[mask])))))
    return SpectralMeasurements(ANALYSIS_VERSION, tuple(measurements))
