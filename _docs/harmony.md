# Bounded pitch and musical key

`backend.analysis.harmony.measure_harmony(audio: LoadedAudio)` returns immutable
`HarmonyMeasurements`: `analysis_version`, a `fundamental` Measurement in Hz,
an independent `MusicalKey`, and diagnostic `HarmonyEvidence`. Version
`harmony-autocorrelation-kk-v1` identifies all policies below. Consumers must retain
this version with cached features and invalidate them when the method changes.
The existing schema-1 Sample accepts the returned measurement and key directly;
unknown values have a reason and no confidence. Malformed input raises ValueError.
Input samples and source files are unchanged; computation is entirely local.

## Admitted domain and framing

Mono/stereo floating-point reader output at 44100, 48000 or 96000 Hz is supported.
F0 is bounded to 30–1000 Hz. Pitch classes use A4=440 Hz, twelve-tone equal
temperament and sharp names. Other valid sample rates return
`unsupported_harmony_sample_rate`. Silence returns `silent_audio` first.

Complete rectangular 200 ms frames advance by 50 ms. An incomplete final tail is
discarded and does not set normalization. The complete prefix is scaled by its
absolute peak before energy calculations. An active frame has combined-channel
RMS at least 1% of the loudest frame; at least three active frames are required
(`insufficient_active_frames`). Each channel with RMS at least 1% of the loudest
channel in that frame is analyzed separately. Waveforms are never downmixed.
Accepted channel pitches must agree within 35 cents and are combined by geometric
mean. An active-channel failure rejects the frame.

## Fundamental estimation and confidence

Each channel frame has its mean removed and is peak normalized. FFT correlation
at lag L is divided by the square root of the two overlapping segment energies:
`sum(x[:N-L]*x[L:]) / sqrt(sum(x[:N-L]**2)*sum(x[L:]**2))`.
Local maxima are searched over guard periods spanning 15–2000 Hz, so a first
period outside the admitted domain is rejected rather than folded into range.
Three-point parabolic interpolation adjusts lag by at most half a sample and
interpolates periodicity. The shortest peak with periodicity at least 0.90 is
selected. An edge allowance of 0.1 cent absorbs interpolation error at 30/1000 Hz;
the accepted result is clamped to that domain.

When a competing period is within 35 cents of twice the selected period, either
an improvement in periodicity above 1e-4 or detectable half-frequency content
causes `octave_ambiguity`. Half-frequency detection jointly least-squares fits a
constant and sine/cosine pairs at 0.5, 1, 2 and 3 times the candidate frequency.
The half-frequency amplitude must exceed both 1e-6 of the primary amplitude and
four times the residual RMS. This catches the QA weak-fundamental counterexamples
without mistaking clean-tone interpolation leakage for a subharmonic.

Clip coverage is accepted frames / active frames. A stable F0 requires coverage
at least 0.80 and the 90th percentile absolute deviation from median log2 pitch
at most 35 cents (linear percentile interpolation). F0 is the exponentiated
median. Confidence is `mean_periodicity * coverage * max(0, 1-dispersion/70)`.
Any channel disagreement or octave ambiguity takes precedence over coverage;
otherwise failures use `pitch_out_of_range` when every rejected frame is outside
range, `insufficient_periodicity`, or `unstable_pitch`. Nonfinite normalization
uses `numerical_range`. Confidence is a deterministic evidence score, not a
calibrated probability. Machine tolerances are 1e-12 for periodicity/profile
scores and 1e-9 cents for dispersion, far below musical thresholds.

## Independent key estimation

Accepted frame pitches still supply key evidence when a melody has no stable
single F0. Each equal-hop frame contributes equal 50 ms weight to its nearest
semitone (half-semitone ties round upward); octave-equivalent pitches share a
class. Key requires clip duration at least 4 seconds, accepted coverage at least
0.60, and at least five classes each contributing at least 5% of accepted duration.
Single notes, octave repetitions and triads therefore cannot produce a key.

The normalized histogram is Pearson-correlated against all twelve rotations of
each Krumhansl–Kessler profile, in C-through-B order:

- Major: 6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88.
- Minor: 6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17.

Coefficients are independently published in the
[music21 primary implementation](https://music21.org/music21docs/_modules/music21/analysis/discrete.html).
No music21 dependency is used. Best fit must be at least 0.80 and exceed the
runner-up by at least 0.10. Ties abstain. Key confidence is
`fit * min(1, separation/0.30) * coverage`, separate from F0 confidence.
Failure reasons in order are `insufficient_key_context`,
`insufficient_key_pitch_coverage`, `insufficient_pitch_class_diversity`,
`ambiguous_key_histogram` (centered norm at most 1e-15), `low_key_fit`, and
`ambiguous_key`.

## Independent synthetic evidence and limits

`tests/harmony_reference_signals.py` declares note labels before estimation.
The C-major phrase MIDI notes are
`60 60 67 67 69 69 67 65 65 64 64 62 62 60`, with beat durations
`1 1 1 1 1 1 2 1 1 1 1 1 1 2`. C minor lowers 69 to 68 and 64 to 63;
D major transposes the original by two semitones. These tonic/dominant phrases
end with a held tonic; labels come from the written notes and cadence, not a
profile-generated histogram or algorithm output. Each beat lasts 0.5 seconds;
each note is a peak-0.5 sine with reset phase. An ambiguous fixture repeats
`60 62 64 65 67 69 71` twice with equal one-beat durations and no tonic emphasis.
All audio is generated in memory.

Observed 48 kHz outputs (the changing melodies correctly have unknown stable F0):

| Fixture | Key output | Fit | Separation | Key confidence |
| --- | --- | --- | --- | --- |
| C-major phrase | C major | 0.960011 | 0.322751 | 0.960011 |
| C-minor phrase | C minor | 0.853166 | 0.204129 | 0.580519 |
| D-major phrase | D major | 0.960011 | 0.322751 | 0.960011 |
| Equal shared scale | unknown: low_key_fit | 0.654226 | 0.008729 | none |

A five-second 110 Hz sine returns 109.999984 Hz with confidence approximately 1,
but unknown key (`insufficient_pitch_class_diversity`). At all three rates, the
QA signal `0.5*(a*sin(2*pi*55*t)+sin(2*pi*110*t))` for a=0.003/0.001 (and an
additional 0.0001 regression) now returns correct F0 or explicit octave ambiguity,
never the confident octave error. Clean 55, 110, 261.6255653, 900 and 1000 Hz
controls remain accepted. The original periodicity-gap-only guard missed the
0.003/0.001 cases; the joint-fit safeguard addresses that measured defect.

These are synthetic checks, not producer listening validation or a calibrated
real-library benchmark. Polyphonic chords, missing fundamentals, strong detuning,
inharmonic/noisy signals and weak content below the residual/numerical detection
floor can defeat this bounded method. It does not guarantee elimination of every
octave error, nor reliable key recognition on arbitrary audio. Abstention gates
reduce unsupported claims; they do not establish real-world accuracy.

Tests cover independent pitch/range labels at all supported rates, harmonic and
octave risks, silence/DC/noise/percussive signals, stereo cancellation/conflicts,
extreme scaling, framing/coverage/dispersion/fit boundaries, positive and ambiguous
key phrases, unchanged inputs and schema round trips. Reproduce with
`uv run pytest --basetemp .pytest_cache/harmony-final` from the repository root.
Engineer validation: **640 passed in 21.32 seconds**, including 81 harmony tests.
No new dependencies or external audio were required.
