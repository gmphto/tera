# Verified subset recovery, with remaining shortfall

Issue #61 recovers **85 unique source contents** (42 kicks, 43 basses) through a
verified nested-Ogg route. The new strict-validated pool has **100 selected kicks
and 46 basses**, plus 23 kick reserves. The remaining **54-bass shortfall** is
explicit; this is not proof of evaluation readiness. The user chose to continue
other work with that shortfall recorded. No new libraries, downloads, codecs or
dependencies were installed. Original source bytes remain unchanged.

The original #10 dataset remains intact at its own version (81 kicks/3 basses,
deficits 19/97). Prepared copies and the new dataset are separate ignored local
artifacts. Role/provenance ambiguities remain excluded or pending. The strict
production WAV reader and DSP behavior are unchanged.

## Bounded diagnosis

[VLC's codec definitions](https://github.com/videolan/vlc/blob/master/include/vlc_codecs.h)
identify 0x674F/0x6750 as Vorbis ACM variants. [Image-Line's format guidance](https://support.image-line.com/action/knowledgebase?ans=643)
describes compressed installer material and older OGG-compressed WAV content;
that general guidance does not prove an individual file is intact.
[FFmpeg's RIFF table](https://ffmpeg.org/doxygen/trunk/riff_8c_source.html) associates
Vorbis with another tag, so the installed decoder list alone was insufficient.

All four representative direct-WAV probes with existing FFmpeg
8.0.1-full_build-www.gyan.dev returned unknown codec. Automatic complete-decode
attempts failed with no decoder found (Windows exit 4294967274, signed -22).
Explicit native Vorbis selection also failed at extradata initialization (exit
4294967295, signed -1). Earlier SoundFile direct opens also failed. No headers
were patched, no extension-renaming workaround was used, and no native DLL wrapper
was added.

The declared data chunks contained an identified Ogg capture pattern. Passing
the **exact unmodified bounded payload** to the existing Ogg demuxer produced
successful complete Vorbis decoding in all four representatives, but successful
decode alone did not establish eligibility:

| Representative | Native layout | RIFF fact frames | Decoded frames | Decision |
| --- | --- | ---: | ---: | --- |
| Kick 0x674F, encoding rejection | mono, 44100 Hz | 10720 | 10720 | Eligible after full checks |
| Bass 0x674F, encoding rejection | mono, 44100 Hz | 197679 | 197679 | Eligible after full checks |
| Kick 0x6750 | mono, 44100 Hz | 15929 | 22050 | Excluded: inconsistent frame evidence |
| Bass 0x674F, padding rejection | mono, 44100 Hz | 794624 | 815850 | Excluded: bounds and frame inconsistency |

All declared outer RIFF lengths matched physical file length. The first three
representatives had bounded chunks. The fourth had a complete compressed data
payload but a terminal 7-byte `inst` chunk missing its alignment byte. This is
recorded as invalid original framing; nothing was appended or silently repaired.
No source was admitted on a padding-error explanation alone.

## Admission and exact canonical copies

`backend.evaluation.prepare` admits only the proven 0x674F subset with complete
RIFF bounds, exactly one format/data/fact chunk and a positive declared frame
count. Ogg validation checks every page's boundary, version, CRC, stream serial,
sequence, continuation lacing, BOS and final EOS. Chained/multiplexed streams,
missing pages, incomplete packets and trailing bytes are rejected. Final granule
position must equal RIFF fact frames. A structurally plausible wrapper is not
automatically declared a valid compressed stream.

FFmpeg receives the immutable Ogg bytes on stdin and decodes to interleaved
float64 PCM in memory, using `-xerror -err_detect explode -f ogg`, mapping the
first audio stream and outputting `pcm_f64le`. No gain, resampling, trimming,
dithering or clipping options are applied. Exit must be zero, stderr empty, output
finite, and byte-derived frame count exactly equal the declared count.

Independent SoundFile/libsndfile OGG/VORBIS decoding must agree on channels,
sample rate, frames and samples (`rtol=1e-5`, `atol=1e-6`). Across the 85 admitted
real sources, maximum observed absolute decoder difference was
**7.152557373046875e-7**. This tolerance applies only to comparison of two decoder
implementations. It does not permit modifying the canonical samples.

Canonical copies use deterministic little-endian RIFF/WAVE format-tag 3,
64-bit float (`DOUBLE`) with the same native rate/channels/frame count. The
unchanged strict reader must reproduce the verified FFmpeg PCM array **exactly**
(`np.array_equal`, zero tolerance). Finite float overrange is preserved. No
timestamp or incidental metadata makes rerun bytes vary. Vorbis is already lossy;
this preserves the decoded signal, not the original pre-compression signal.

Copies are named by source fingerprint plus preparation version and created
exclusively outside every source root. Existing copies must be byte-identical,
regular non-alias files or preparation rejects the collision. Reruns are
idempotent; they do not overwrite unrelated files. Interrupted partial copies
fail collision validation and need explicit local inspection/removal before
retry. Original hashes are rechecked; no original path is written.

The private report records source SHA-256 and all duplicate source mappings,
source role/category/subtype/pack provenance, tool version/settings, output path
and fingerprint, native metadata, integrity/sample equality evidence and explicit
exclusion reasons. Preparation version is `nested-ogg-674f-double-v1`; change it
when conversion/admission semantics change. Source fingerprints define the
source-to-derived relationship, so two installations do not cause two recoveries.
Byte-identical derived outputs are also deduplicated by #10 selection. A source
and its derived copy never both count here: original rejected encodings remain
in the excluded ledger. Alternate derived encodings of one source are not
produced or imported as extra coverage.

## Reproduction and local evidence

Existing FFmpeg/ffprobe and approved SoundFile/NumPy are prerequisites. Nothing
automatically installs or downloads tools. All actual paths, filenames, hashes,
audio, private diagnostics and manifests stay under the ignored local evaluation
directory or at their untouched original locations.

```text
uv run python -m backend.evaluation.diagnostics .local-evaluation/dataset.json --report .local-evaluation/codec-probe.json
uv run python -m backend.evaluation.prepare .local-evaluation/dataset.json --directory .local-evaluation/prepared --report .local-evaluation/preparation.json --output-dataset .local-evaluation/prepared-dataset.json --validation-report .local-evaluation/prepared-validation.json
uv run python -m backend.evaluation.manifest validate .local-evaluation/prepared-dataset.json --report .local-evaluation/prepared-validation.json
```

The diagnostics command deliberately returns 1: diagnostics alone never certify
recovery. It prints predefined structural/codec/error categories, not raw sample
prefixes, mappings or logs. Detailed errors stay in the private report. A raw
payload-prefix display was rejected by automatic approval review during the
investigation; subsequent diagnostics use safe identifiers and aggregates.
Preparation returns 0 only when the new dataset validates; a valid remaining
shortfall is reported rather than disguised as failure or completed coverage.

Two real preparation runs produced identical coverage and accepted existing
copies without collision, while all original hashes remained unchanged.
179 unique rejected source hashes represented 358 mappings across installations:

| Outcome | Kick | Bass |
| --- | ---: | ---: |
| Prepared and strictly validated | 42 | 43 |
| Original RIFF bounds invalid | 0 | 40 |
| Ogg final granule/fact mismatch | 0 | 36 |
| Unproved 0x6750 variant | 9 | 0 |
| Decoder frame-count mismatch | 4 | 0 |
| Missing/reordered Ogg pages | 2 | 3 |

Total remaining excluded source hashes: **94**. The new selected pool is
**100 kicks / 46 basses**, with 23 kick reserves and **54 basses still needed**.
Other #10 pending/excluded records remain unchanged (439 pending, 6121 excluded
original mappings). New sources must have genuine role/provenance evidence and
pass the same strict reader; no synthetic material fills this gap. A concrete
future prerequisite is 54 additional unique authorized, role-confirmed PCM/float
bass files or source-linked native-rate/channel PCM exports from a trusted
application. Exports require original/export mappings and frame/sample evidence;
guessed repair or trimming of mismatched sources is not acceptable. No further
decoder or library work is being pursued under the user's current direction.

## Regression scope

Synthetic tests cover native mono/stereo rates, Ogg CRC/page truncation/missing
EOS/trailing data, invalid original RIFF bounds, unsupported variants and fact/
decoded-frame disagreement, exact canonical round trips, source preservation,
idempotence/collisions and safe diagnostic output. Modern generated Vorbis can
show a 128-frame difference between these decoder implementations; those inputs
remain rejected. Positive structural/unit tests explicitly model coherent
external-decoder PCM instead of weakening frame checks or presenting those
fixtures as independent real decoder evidence. The independent integration
evidence is the 85 actual, privately verified recoveries above.

```text
uv run pytest tests/test_evaluation_prepare.py --basetemp .pytest_cache/recovery-focused
uv run pytest --basetemp .pytest_cache/recovery-final
```

Focused tests: 13 passed. No synthetic fixture enters the real evaluation pool.
