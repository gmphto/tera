# DSP feasibility execution evidence

Issue #3, 2026-09-18. The user explicitly approved the dependency set and its
transitives before installation. See [decision and sources](dsp-feasibility.md).

## Installation and imports

`uv add "numpy==2.2.6" "scipy==1.15.3" "soundfile==0.13.1"` succeeded on
Windows 11 build 10.0.26200 AMD64 / CPython 3.13.5. `pyproject.toml` declares
the three direct pins and `uv.lock` records CFFI 2.1.1 and pycparser 3.0.
Imports of all three candidates succeeded; SoundFile reports libsndfile 1.2.2.
Essentia remains unavailable and was not installed (native Windows Python
bindings unsupported upstream). The fallback runtime runs on this development
platform. A distributable desktop package has not been built or verified.

## Local synthetic fixture measurements

The standard-library generator in `tests/test_dsp_feasibility.py` generates
original synthetic audio entirely in memory. All 12 valid WAVs decode with
the expected subtype, 48,000 frames at 48,000 Hz (1 second), and their expected
one or two channels. Corrupt RIFF and unknown 0xffff WAV codec both reject with
`LibsndfileError`. Every channel's autocorrelation pitch estimate is
440.366972 Hz and dominant FFT bin is 440 Hz for the original 440 Hz tone.
Peaks are 0.5 in mono/left and 0.25 in stereo right. The following values are
rounded; the executable probe prints full precision. Each subtype row covers
both its mono and stereo fixture; the left measurement is equal to mono to
the shown precision.

| Subtype | Mono/left RMS | Right RMS | Mono/left magnitude centroid Hz | Right centroid Hz | Status (mono/stereo) |
| --- | ---: | ---: | ---: | ---: | --- |
| PCM_U8 | 0.353640 | 0.176885 | 1392.247630 | 2180.392892 | pass / pass |
| PCM_16 | 0.353553 | 0.176777 | 444.029534 | 447.547783 | pass / pass |
| PCM_24 | 0.353553 | 0.176777 | 440.015478 | 440.030801 | pass / pass |
| PCM_32 | 0.353553 | 0.176777 | 440.000079 | 440.000144 | pass / pass |
| FLOAT | 0.353553 | 0.176777 | 440.003510 | 440.003510 | pass / pass |
| DOUBLE | 0.353553 | 0.176777 | 440.000020 | 440.000020 | pass / pass |
| Corrupt RIFF | N/A | N/A | N/A | N/A | reject_pass |
| Unsupported codec | N/A | N/A | N/A | N/A | reject_pass |

Low-bit quantization produces high-frequency energy that shifts a
magnitude-weighted centroid substantially, despite the dominant tone and
pitch remaining correct. The initial assertion incorrectly expected low-bit
centroids near 440 Hz (235 passed, 1 failed). The corrected check verifies the
dominant bin for every encoding and the centroid near 440 Hz for finer
encodings; all centroids must be finite and within 0–Nyquist. The quantized
measurements above are preserved rather than hidden by an arbitrary tolerance.

Final `uv run pytest`: **236 passed, no skips**, 13.42 seconds. The subsequent
standalone probe confirms 12 `pass` plus 2 `reject_pass` results. Before
installation, the same probe explicitly reported all candidates unavailable
and all 14 fixture measurements unrun; those were not successful DSP checks.

LUFS has not been measured; RMS is not LUFS. Real kick/bass pitch confidence,
key estimation and envelope validation remain the named fallback experiments
in the decision document. No user audio or local library path was transmitted.

## Reproduction

```powershell
uv sync --locked
uv run python tests/test_dsp_feasibility.py
uv run pytest
```

The probe prints imports, metadata and measurements as JSON. Unavailable
dependencies are explicitly unrun, never a passing measurement. Synthetic
results establish the subtype matrix's feasibility, not a production reader
or robust music analysis implementation.
