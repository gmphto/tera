"""Bounded autocorrelation pitch and conservative major/minor key evidence."""

from dataclasses import dataclass
import math

import numpy as np
from scipy import signal

from backend.audio import LoadedAudio
from backend.contracts import Measurement, MusicalKey


ANALYSIS_VERSION = "harmony-autocorrelation-kk-v1"
PITCH_CLASSES = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")
# Published Krumhansl-Kessler coefficients, documented by music21; no music21 dependency.
MAJOR_PROFILE = (6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88)
MINOR_PROFILE = (6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17)


@dataclass(frozen=True)
class HarmonyEvidence:
    active_frames: int = 0
    accepted_frames: int = 0
    coverage: float = 0.0
    mean_periodicity: float | None = None
    dispersion_cents: float | None = None
    key_fit: float | None = None
    key_separation: float | None = None


@dataclass(frozen=True)
class HarmonyMeasurements:
    analysis_version: str
    fundamental: Measurement
    key: MusicalKey
    evidence: HarmonyEvidence


def _pitch_unknown(reason):
    return Measurement(name="fundamental", value=None, unit="Hz", unavailable_reason=reason)


def _key_unknown(reason):
    return MusicalKey(tonic=None, mode=None, confidence=None, unavailable_reason=reason)


def _unknown(reason):
    return HarmonyMeasurements(ANALYSIS_VERSION, _pitch_unknown(reason), _key_unknown(reason), HarmonyEvidence())


def _subharmonic_evidence(x, frequency, rate):
    # Fit the candidate plus two harmonics and its half-frequency jointly.
    # True subharmonic evidence must exceed both a small numerical floor and
    # the remaining model error; pure-tone interpolation leakage alone is not
    # treated as evidence of a second pitch.
    t = np.arange(len(x)) / rate
    basis = np.column_stack([np.ones(len(x))] + [
        fn(2*np.pi*frequency*multiple*t)
        for multiple in (.5, 1, 2, 3) for fn in (np.sin, np.cos)
    ])
    coefficients = np.linalg.lstsq(basis, x, rcond=None)[0]
    residual = float(np.sqrt(np.mean((x - basis @ coefficients)**2)))
    subharmonic = float(np.hypot(coefficients[1], coefficients[2]))
    primary = float(np.hypot(coefficients[3], coefficients[4]))
    return subharmonic > max(primary * 1e-6, 4 * residual)


def _frame_pitch(values, rate):
    x = values - np.mean(values)
    peak = float(np.max(np.abs(x)))
    if peak == 0:
        return None, None, "insufficient_periodicity"
    x = x / peak
    n = len(x)
    # Guard search includes 15–2000Hz to detect out-of-range first periods,
    # rather than relabeling a 1500Hz tone's second repetition as750Hz.
    low, high = max(2, rate // 2000), min(n - 2, math.ceil(rate / 15))
    corr = signal.correlate(x, x, mode="full", method="fft")[n-1:n+high+1]
    sums = np.r_[0., np.cumsum(x*x)]
    lags = np.arange(len(corr))
    denominator = np.sqrt(sums[n-lags] * (sums[n] - sums[lags]))
    normalized = np.divide(corr, denominator, out=np.zeros_like(corr), where=denominator > 0)
    peaks, _ = signal.find_peaks(normalized)
    candidates = []
    for lag in peaks[(peaks >= low) & (peaks <= high)]:
        left, middle, right = normalized[lag-1:lag+2]
        curvature = left - 2*middle + right
        offset = float(np.clip(.5*(left-right)/curvature, -.5, .5)) if curvature else 0.
        quality = float(np.clip(middle - .25*(left-right)*offset, -1, 1))
        if quality >= .90 - 1e-12:
            candidates.append((lag+offset, quality))
    if not candidates:
        return None, None, "insufficient_periodicity"
    lag, quality = candidates[0]
    frequency = rate / lag
    if frequency < 30 * 2**(-.1/1200) or frequency > 1000 * 2**(.1/1200):
        return None, None, "pitch_out_of_range"
    # A materially stronger full-period match at twice the earliest period
    # indicates dominant-second-harmonic competition. Abstain, do not octave-fix.
    for other_lag, other_quality in candidates[1:]:
        if abs(1200 * math.log2(other_lag / (2*lag))) <= 35:
            if other_quality - quality > 1e-4 or _subharmonic_evidence(x, frequency, rate):
                return None, None, "octave_ambiguity"
    return float(np.clip(frequency, 30, 1000)), quality, None


def _key(pitches, coverage, duration):
    if duration < 4:
        return _key_unknown("insufficient_key_context"), None, None
    if coverage < .60 or not pitches:
        return _key_unknown("insufficient_key_pitch_coverage"), None, None
    # Equal-hop accepted frames each represent50ms. Round semitone ties up.
    midi = np.floor(69 + 12*np.log2(np.asarray(pitches)/440) + .5).astype(int)
    histogram = np.bincount(midi % 12, minlength=12).astype(float)
    histogram /= histogram.sum()
    if np.count_nonzero(histogram >= .05) < 5:
        return _key_unknown("insufficient_pitch_class_diversity"), None, None
    centered = histogram - histogram.mean()
    norm = float(np.linalg.norm(centered))
    if norm <= 1e-15:
        return _key_unknown("ambiguous_key_histogram"), None, None
    scores = []
    for mode, profile in (("major", MAJOR_PROFILE), ("minor", MINOR_PROFILE)):
        for tonic in range(12):
            p = np.roll(profile, tonic)
            p -= p.mean()
            fit = float(np.clip(np.dot(centered, p)/(norm*np.linalg.norm(p)), -1, 1))
            scores.append((fit, tonic, mode))
    scores.sort(key=lambda item: item[0], reverse=True)
    fit, tonic, mode = scores[0]
    separation = fit - scores[1][0]
    if fit < .80 - 1e-12:
        return _key_unknown("low_key_fit"), fit, separation
    if separation < .10 - 1e-12:
        return _key_unknown("ambiguous_key"), fit, separation
    confidence = fit * min(1., separation/.30) * coverage
    return MusicalKey(tonic=PITCH_CLASSES[tonic], mode=mode, confidence=confidence), fit, separation


def measure_harmony(audio: LoadedAudio) -> HarmonyMeasurements:
    """Estimate bounded F0 and independent key; malformed input raises ValueError."""
    if not isinstance(audio, LoadedAudio):
        raise ValueError("Expected LoadedAudio from the local WAV reader.")
    x = audio.samples
    if not isinstance(x, np.ndarray) or x.dtype.kind != "f" or x.ndim != 2 or not len(x) or x.shape[1] not in (1, 2):
        raise ValueError("Expected nonempty floating-point frames-by-mono/stereo audio.")
    rate = audio.sample_rate_hz
    if type(rate) is not int or not 0 < rate <= 2**31-1:
        raise ValueError("Expected a positive reader-supported integer sample rate.")
    if type(audio.frame_count) is not int or type(audio.channels) is not int or (audio.frame_count, audio.channels) != x.shape:
        raise ValueError("Metadata must match the sample shape.")
    if type(audio.duration_seconds) not in (int, float) or not math.isfinite(audio.duration_seconds) or not math.isclose(audio.duration_seconds, len(x)/rate, rel_tol=0, abs_tol=1e-12):
        raise ValueError("Duration must match frames and sample rate.")
    if not np.isfinite(x).all():
        raise ValueError("Samples must be finite.")
    if not np.any(x):
        return _unknown("silent_audio")
    if rate not in (44100, 48000, 96000):
        return _unknown("unsupported_harmony_sample_rate")
    size, hop = rate // 5, rate // 20
    if len(x) < size + 2*hop:
        return _unknown("insufficient_active_frames")
    end = ((len(x)-size)//hop)*hop + size
    complete = x[:end]
    scale = np.max(np.abs(complete))
    if scale == 0:
        return _unknown("insufficient_active_frames")
    with np.errstate(over="ignore", under="ignore", invalid="ignore"):
        normalized = np.asarray(complete / scale, dtype=np.float64)
    if not np.isfinite(normalized).all():
        return _unknown("numerical_range")
    frames = [normalized[start:start+size] for start in range(0, end-size+1, hop)]
    rms = np.array([np.sqrt(np.mean(frame*frame)) for frame in frames])
    active = rms >= .01 * float(np.max(rms))
    count = int(np.count_nonzero(active))
    if count < 3:
        return _unknown("insufficient_active_frames")
    pitches, qualities, rejected = [], [], []
    for frame, enabled in zip(frames, active):
        if not enabled:
            continue
        channel_rms = np.sqrt(np.mean(frame*frame, axis=0))
        selected = np.flatnonzero(channel_rms >= .01*np.max(channel_rms))
        evidence = [_frame_pitch(frame[:, channel], rate) for channel in selected]
        failures = [reason for _, _, reason in evidence if reason]
        if failures:
            rejected.extend(failures)
            continue
        frequencies = [frequency for frequency, _, _ in evidence]
        if 1200*math.log2(max(frequencies)/min(frequencies)) > 35:
            rejected.append("channel_disagreement")
            continue
        pitches.append(float(2**np.mean(np.log2(frequencies))))
        qualities.append(float(np.mean([q for _, q, _ in evidence])))
    coverage = len(pitches)/count
    dispersion, periodicity = None, None
    if pitches:
        logs = np.log2(pitches)
        median = float(np.median(logs))
        dispersion = float(np.percentile(np.abs(logs-median)*1200, 90, method="linear"))
        periodicity = float(np.mean(qualities))
    reason = next((name for name in ("channel_disagreement", "octave_ambiguity") if name in rejected), None)
    if reason:
        fundamental = _pitch_unknown(reason)
    elif coverage < .80:
        reason = "pitch_out_of_range" if rejected and all(r == "pitch_out_of_range" for r in rejected) else "insufficient_periodicity"
        fundamental = _pitch_unknown(reason)
    elif dispersion > 35 + 1e-9:
        fundamental = _pitch_unknown("unstable_pitch")
    else:
        confidence = periodicity * coverage * max(0., 1-dispersion/70)
        fundamental = Measurement(name="fundamental", value=float(2**median), unit="Hz", confidence=confidence)
    key, fit, separation = _key(pitches, coverage, audio.duration_seconds)
    return HarmonyMeasurements(ANALYSIS_VERSION, fundamental, key,
        HarmonyEvidence(count, len(pitches), coverage, periodicity, dispersion, fit, separation))
