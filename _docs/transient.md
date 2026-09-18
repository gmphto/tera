# Observed transient and envelope measurements

`backend.analysis.transient.measure_transient(LoadedAudio)` returns an immutable
`TransientMeasurements` record with `analysis_version="transient-envelope-v1"`
and four existing contract measurements: `transient_strength` (normalized),
`transient_position`, `attack`, and `decay` (milliseconds).

```python
from backend.audio import load_wav
from backend.analysis.transient import measure_transient

audio = load_wav("samples/kick.wav")
result = measure_transient(audio)
for item in result.measurements:
    print(item.name, item.value, item.unit, item.unavailable_reason)
```

## Envelope and timing

Consecutive nonoverlapping blocks use boundaries `floor(k * sample_rate / 200)`
frames, anchored at frame zero. This represents 5 ms without accumulating
rounding drift, including alternating 220/221-frame blocks at 44.1 kHz.
Only complete blocks participate. A discarded partial tail cannot affect the
normalization, create a transient, or confirm decay. Each block's RMS averages
squared samples over both frames and channels; waveforms are never summed.

Condition arithmetic using a common peak scale from the retained prefix, then
normalize block RMS by the largest observed block RMS. Within each block a
bounded secondary scale avoids squaring very small values and preserves
constant-block threshold equality. These internal operations do not modify or
normalize the input audio. Scale, polarity, and duplicate/opposite stereo
channels do not change the descriptors. Unequal channels contribute through
their actual combined mean-square energy.

All times use actual block-start frame indices divided by sample rate, in ms;
they are not interpolated. Resolution is one nominal 5 ms block, with at most
one sample of boundary rounding. An impulse is therefore allowed to have zero
block-resolved attack; this does not claim infinitely fast physical onset.

## Single-event rules

- Peak is the earliest global envelope maximum.
- Attack starts at the first block reaching 10% of peak. It ends at the first
  block reaching 90% at or after that crossing and no later than the peak.
  The time difference can be zero. If the first block already reaches 10%,
  attack is unknown with `left_truncated_attack`.
- Transient strength is the largest positive normalized envelope increment
  from the 10% crossing through the peak, inclusive. Position is its block-start
  time from file zero. Earliest ties win. The envelope preceding the first
  block is defined as zero: a clip starting at its peak can consequently have
  strength 1 and position 0 even when its attack is left-truncated.
- Decay begins at the peak block. Its endpoint is the first later block at
  or below 10% that begins two consecutive complete blocks at or below 10%.
  Equality counts. Without both blocks, decay is `truncated_decay`; it is
  never replaced by the remaining clip duration.

Before those measurements, detect separated repeated activity: a block at or
above 10%, followed by at least two blocks at or below 10%, followed by renewed
activity reaching 50% of the global peak. Such a clip returns `multiple_onsets`
for all four descriptors. Quiet blocks must follow the initial activity block;
it cannot serve as both initial activity and the first following quiet block.
This conservative rule does not resolve overlapping musical events or carrier
ripple. The transient is an observed block-rise descriptor, not confidence or
proof that the source contains an uncut onset.

Normalized threshold and tie comparisons permit eight float64 epsilons
(approximately 1.78e-15) to absorb arithmetic roundoff at equality. This is
numerical equality handling, not a musical hysteresis or a changed threshold.

## Availability and validation

Malformed arrays, empty/nonfinite input, nonpositive/noninteger or
out-of-reader-range rates, and inconsistent channel/frame/duration metadata
raise `ValueError`. Valid shape is floating-point frames × one/two channels.

For otherwise valid input, precedence is:

1. Exact digital silence: all four `silent_audio`.
2. Rate below 200 Hz: all four `unsupported_envelope_sample_rate`.
3. Fewer than two complete blocks: all four `too_short_for_envelope_analysis`.
4. Signal only in a discarded tail, with zero retained energy: all four
   `insufficient_observed_audio`.
5. Unrepresentable numerical result: all four `numerical_range`.
6. Separated events: all four `multiple_onsets`.
7. Otherwise the individual left-truncated attack and truncated-decay rules
   apply; transient descriptors remain available.

Unknowns have null values, a reason and no confidence. Known facts also omit
confidence. No NaN or infinity is emitted. Common conditioning supports both
float64 maximum finite amplitude and its smallest positive subnormal; energy
far below floating-point resolution can round away. Input arrays can be
read-only. Source files, samples, metadata and local paths remain untouched;
there are no remote calls or sidecar files.

## Independent fixtures and reproduction

`tests/test_transient.py` constructs declared constant-amplitude blocks without
production helpers. Their normalized RMS envelopes are known by construction.
Examples (block indices begin at zero):

| Envelope | Expected strength | Position | Attack | Decay |
| --- | ---: | ---: | ---: | ---: |
| 0,0,.1,.3,.6,.9,1,.5,.1,.1 | .3 | 20 ms | 15 ms | 10 ms |
| 0,.1,.5,1,1,.5,.1,.1 | .5 | 15 ms | 10 ms | 15 ms |
| 0,.25,.5,.75,1,.1,.1 | .25 | 5 ms | 15 ms | 5 ms |
| 0,.1,.9,1,.1,.1 | .8 | 10 ms | 5 ms | 5 ms |

The exponential fixture starts at block 2 with `exp(-k/4)` amplitudes;
the first decay crossing occurs at k=10 and is confirmed at k=11, giving
50 ms. A real single-frame impulse after leading silence gives strength 1,
attack 0 and one-block decay with sufficient trailing silence. Additional
fixtures cover long/truncated tails, threshold equality, ties, two onsets,
availability precedence, fractional boundaries, channels/scaling, extreme
values, input preservation and serialization. Strength tolerance is 1e-12;
rate-equivalent timing checks use one sample (tighter than the method's
one-block-plus-one-sample descriptive resolution).

```powershell
uv run pytest tests/test_transient.py --basetemp .pytest_cache/transient-check
uv run pytest --basetemp .pytest_cache/transient-full
```

## Versioned Sample integration

Changes to envelope size/boundaries, thresholds, tie rules, timings, channel
aggregation or availability semantics require a new analysis version and
invalidation of cached measurements. Shared schema 1.0 and units remain fixed.
This integration is also tested by serializing and reloading a valid `Sample`:

```python
from pathlib import Path
from backend.contracts import (
    Sample, AudioMetadata, AudioFeatures, Measurement, MusicalKey, MEASURES,
)

path = Path("samples/kick.wav").resolve()
audio = load_wav(path)
result = measure_transient(audio)
known = {m.name for m in result.measurements}
unknowns = tuple(
    Measurement(name=name, value=None, unit=spec[0],
                unavailable_reason="not_implemented")
    for name, spec in MEASURES.items() if name not in known
)
sample = Sample(
    sample_id="kick-example", role="kick", analysis_version=result.analysis_version,
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
