# Spectral measurements

`backend.analysis.spectral.measure_spectral(LoadedAudio)` returns an immutable
`SpectralMeasurements` record with `analysis_version="spectral-power-v1"` and
eight existing contract measurements. It uses only local NumPy calculations;
no source file, metadata or input array is modified.

```python
from backend.audio import load_wav
from backend.analysis.spectral import measure_spectral

audio = load_wav("samples/bass.wav")
result = measure_spectral(audio)
for item in result.measurements:
    print(item.name, item.value, item.unit, item.unavailable_reason)
```

## Power definition

Version 1 applies a single full-clip rectangular window, with exactly N real
FFT points per channel. It does not pad, detrend, remove the mean, or introduce
overlapping frames. [NumPy's FFT conventions](https://numpy.org/doc/2.2/reference/routines.fft.html)
define `norm="forward"` as scaling the forward transform by 1/N. We square
complex magnitudes and double interior one-sided bins to include their negative
frequency partners. DC and the even-N Nyquist bin are not doubled; odd N has
no Nyquist endpoint, so its final positive bin is doubled. Average channel
powers with equal weights; never combine waveforms. Thus summed spectral power
equals the time-domain mean square over frames and channels (Parseval).

The calculation first divides samples by their maximum absolute value. This
conditions arithmetic before FFT/squaring and makes finite extreme amplitudes
safe; frequencies and normalized ratios do not depend on that common gain.
The summed conditioned spectrum equals mean square of the conditioned input;
multiplication by peak squared would recover physical mean square where
representable. The extractor does not need that potentially overflowing step.
Tests cover float64's maximum and smallest positive subnormal values. Powers
below numerical precision may round away; nonfinite or zero total calculated
power for nonsilent input returns `numerical_range` unknowns, never NaN/infinity.

## Frequencies, centroid and roll-off

Bin centres are `k * sample_rate / N`; resolution is `sample_rate/N` Hz. Integer
products precede division to keep exact boundaries and the Nyquist endpoint
from crossing strict contract bounds through intermediate rounding.

- `spectral_centroid` is sum(frequency × power) / total power, in Hz.
- `spectral_rolloff` is the first bin whose cumulative power reaches at least
  85% of total power, in Hz; no frequency interpolation is applied.

Both include DC and all observable frequencies through Nyquist. This production
centroid is **power-weighted**; the magnitude-weighted feasibility probe in #3
has different semantics. The rectangular window leaks power between bins when
the clip is not periodic over its full length. Very short clips have coarse
resolution; these measurements do not imply precise pitch or a smoothed
time-varying spectrum.

## Band ratios

| Measurement | Interval in Hz |
| --- | --- |
| `band_sub` | [20, 60) |
| `band_bass` | [60, 250) |
| `band_low_mid` | [250, 500) |
| `band_mid` | [500, 2000) |
| `band_high_mid` | [2000, 6000) |
| `band_high` | [6000, 20000] |

Membership uses bin centres. Shared edges belong only to the higher band;
20 kHz belongs to high. Each numerator is divided by the total DC-to-Nyquist
power. Below-20 Hz and above-20 kHz energy stays in the denominator, and the
six ratios need not sum to one. Unknown bands do not cause renormalization.

The entire declared band must lie at or below physical Nyquist to be known.
A partially truncated or wholly unavailable interval is unknown with
`band_exceeds_nyquist`. For a fully observable interval containing no FFT bin,
the reason is `insufficient_frequency_resolution`. An observable band with
bins but no energy is a measured zero (up to floating-point roundoff), not
unknown. A high band's endpoint exactly at Nyquist is supported.

## Input and availability policies

Malformed `LoadedAudio`, nonpositive/noninteger or out-of-reader-range rates,
inconsistent channel/frame/duration metadata, empty or unsupported array
shapes, and nonfinite samples raise `ValueError`. Valid arrays have a floating
dtype and frame-by-channel shape with one or two channels.

Exact digital silence takes precedence: all eight measurements are unknown
with `silent_audio`, including a one-frame silent clip. Other one-frame clips
return `too_short_for_spectral_analysis` for all eight. Two-frame clips are
analyzed. For each band, Nyquist availability takes precedence over bin
resolution. Unknowns have null values, a reason, and no confidence; known
facts also omit confidence. Known ratios stay in [0,1] and spectral values
stay within [0, Nyquist].

## Independent validation

Analytic one-second cosines put energy at exact 1 Hz bins at 44.1/48/96 kHz.
Tests declare expected band memberships independently, covering an interior
frequency in each band and every boundary. Multi-tone expectations use the
identity that a non-endpoint cosine of amplitude A has mean-square A²/2;
constant DC and alternating Nyquist signals each have power A². These formulas
establish weighted centroid, cumulative roll-off, stereo weighting and ratios
without calling the production spectral implementation to generate answers.

Additional cases cover Parseval with odd/even lengths, DC and Nyquist endpoint
weights, excluded DC/out-of-band power, unavailable and no-bin bands, duplicate
and opposite-polarity stereo, source immutability, scaling and numerical
extremes. Tolerances: centroid 1e-8 Hz for analytic fixtures, ratio 1e-12
absolute, Parseval 1e-13 relative; roll-off is checked at its exact bin.
A constant 48,000-point signal can produce FFT roundoff around 1e-32 in the
centroid and 1e-34 in bands; tests use explicit tolerances rather than arbitrary
spectral denoising.

```powershell
uv run pytest tests/test_spectral.py --basetemp .pytest_cache/spectral-check
uv run pytest --basetemp .pytest_cache/spectral-full
```

## Versioned feature integration

Any change to windowing, FFT normalization, channel aggregation, centroid or
roll-off definition, bands, availability semantics or numerical policy requires
a new analysis version and invalidation of cached values. Schema version 1.0
does not change. This example leaves unrelated features explicitly unknown;
an executable counterpart round-trips through `Sample` JSON in the tests.

```python
from pathlib import Path
from backend.contracts import (
    Sample, AudioMetadata, AudioFeatures, Measurement, MusicalKey, MEASURES,
)

path = Path("samples/bass.wav").resolve()
audio = load_wav(path)
result = measure_spectral(audio)
known = {m.name for m in result.measurements}
unknowns = tuple(
    Measurement(name=name, value=None, unit=spec[0],
                unavailable_reason="not_implemented")
    for name, spec in MEASURES.items() if name not in known
)
sample = Sample(
    sample_id="bass-example", role="bass", analysis_version=result.analysis_version,
    audio=AudioMetadata(local_path=str(path), channels=audio.channels,
                        sample_rate_hz=audio.sample_rate_hz,
                        frame_count=audio.frame_count,
                        duration_ms=audio.duration_seconds * 1000),
    features=AudioFeatures(
        measurements=result.measurements + unknowns,
        key=MusicalKey(tonic=None, mode=None, confidence=None,
                       unavailable_reason="not_implemented")),
)
```
