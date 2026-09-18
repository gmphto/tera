# DSP feasibility — issue #3

Evidence date: 2026-09-18. The user approved the dependencies before they were
added. Native Windows fallback installation, imports and the 14-fixture
experiment pass. See [measurements](dsp-feasibility-results.md). This is a
feasibility result, not production extractor or desktop packaging validation.

## Environment and dependency decision

Measured with `uv run --no-sync python`: Windows 11 build 10.0.26200, AMD64,
CPython 3.13.5 (MSC v.1943, 64 bit). The unrelated `python` on PATH is 3.14.5;
use `uv run`. Before approval `uv pip list` contained only pytest 9.1.1 and
its supporting packages. Some sandbox launches failed before execution with
`setup refresh had errors`; approved escalated launches succeeded. These
launcher errors are not DSP failures.

| Package | Candidate | Installation/import result | Approval |
| --- | --- | --- | --- |
| NumPy | 2.2.6 | Pass, CPython 3.13 Windows AMD64 wheel | Approved |
| SciPy | 1.15.3 | Pass, CPython 3.13 Windows AMD64 wheel | Approved |
| SoundFile | 0.13.1 | Pass, Windows wheel; libsndfile 1.2.2 | Approved |
| Essentia | 2.1b6.dev1389 | Unavailable; native Windows Python bindings unsupported; install not attempted | Not requested |

Recommend the installed NumPy/SciPy/SoundFile set: decoding and general
numerical/signal primitives work on the actual development platform without
a compiler. Exact experiment pins make the evidence reproducible; they are
not claimed to be latest releases or Python 3.14 compatible. `pyproject.toml`
declares direct pins; `uv.lock` records the approved CFFI 2.1.1 and pycparser
3.0 transitives. The successful installation command was:

```powershell
uv add "numpy==2.2.6" "scipy==1.15.3" "soundfile==0.13.1"
```

The fallback runtime is technically viable on this native Windows host.
Shipping remains conditional on a packaged-runtime check and license inventory
in #38; this experiment did not build a desktop package. Essentia is not viable
as the native Windows Python runtime. A WSL/Linux comparison would require
separate environment approval and would not prove native desktop support.
For such a separately approved comparison, the candidate command is
`python -m pip install essentia==2.1b6.dev1389` in a supported Linux interpreter;
it was **not run**, and no Linux platform result is claimed.

## Supported experimental WAV matrix

The standard-library generator makes original synthetic tones in memory;
no external sample permission or user library access is needed. PCM_U8,
PCM_16, PCM_24, PCM_32, FLOAT (32-bit), DOUBLE (64-bit) each pass in mono and
stereo: 48,000 Hz, 48,000 frames, one second, 440 Hz. Left/mono peak is 0.5;
stereo right is 0.25 to verify channel preservation. A corrupt RIFF and WAV
with unknown codec tag 0xffff both reject. Readability, rate, channels,
duration, RMS/peak, centroid and pitch are recorded in the results document.

Support here means successful synthetic decoding, not a production import
contract. Extensible WAV, compressed WAV, RF64, >2 channels and non-finite
float samples were not covered. Pure-tone pitch success does not establish
kick/bass pitch reliability. RMS is not LUFS.

## Remaining capability gates and named fallback experiments

- #4: SoundFile decoding is feasible for the six tested subtypes; production
  validation/error handling remains #4.
- #5: NumPy RMS/peak succeeds. Integrated LUFS remains unresolved: the named
  fallback is SciPy BS.1770 filtering/gating checked against published reference
  signals, or a separately approved metering library. Do not claim LUFS until
  reference checks pass.
- #6: NumPy FFT centroid succeeds; quantization changes magnitude centroids.
  Production band-energy/rolloff validation remains #6 using known test tones.
- #7: SciPy/NumPy envelope measurement on a generated attack/decay signal is
  the named fallback experiment. Envelope timing is unrun in this spike.
- #8: SciPy autocorrelation succeeds on the pure tone. Real short/percussive
  pitch, key and confidence are unresolved. The named next experiment is
  librosa YIN/pYIN on short tonal/noisy fixtures if baseline autocorrelation
  fails; that additional dependency needs separate approval. madmom and aubio
  were not evaluated because no additional specific gap justifies them.

## Native libraries and distribution constraints

NumPy/SciPy wheels contain compiled extensions and numerical libraries; retain
their BSD notices and bundled-library notices. SoundFile is BSD-3-Clause,
uses CFFI and dynamically loads libsndfile. Its bundled LGPL library has
separate obligations. In #38 inventory the actual DLLs/license files, preserve
notices and satisfy replacement/relinking and source obligations as applicable.
No compiler or system libsndfile install was needed for these Windows wheels.

Essentia's upstream page offers AGPLv3 and commercial licensing, with separate
obligations for optional GPL/LGPL dependencies and models. Its source build
can use Eigen, FFTW, FFmpeg, libsamplerate and others depending on algorithms.
A commercial Essentia license does not automatically clear third-party rights.
No Essentia models are needed or approved. Shipping rights are not certified
by this feasibility note.

## Reproduction and status meanings

```powershell
uv sync --locked
uv run python tests/test_dsp_feasibility.py
uv run pytest
```

The probe reports JSON without local audio paths. `pass` means valid fixture
metadata and measurement checks passed; `reject_pass` means a negative fixture
was rejected. `unavailable`/`unrun_dependencies_unavailable` are explicitly not
passing checks. Essentia is unavailable; LUFS is `unrun_no_meter`. The final
whole suite passes 236 tests with no skips. Issue closure belongs to QA and
the orchestrator after review.

## Official sources checked

- [Essentia platform/build instructions](https://essentia.upf.edu/installing.html)
- [Essentia licensing](https://essentia.upf.edu/licensing_information.html)
- [Essentia candidate release](https://pypi.org/project/essentia/2.1b6.dev1389/)
- [NumPy pinned release and wheels](https://pypi.org/project/numpy/2.2.6/)
- [SciPy pinned release and wheels](https://pypi.org/project/scipy/1.15.3/)
- [SoundFile pinned release, installation and licensing](https://pypi.org/project/soundfile/0.13.1/)
