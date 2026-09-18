# Local WAV reader

`backend.audio.load_wav(path)` accepts a local string or `Path` and returns
`LoadedAudio`. It never contacts a service or writes audio or sidecar files.

```python
from pathlib import Path
from backend.audio import load_wav, AudioReadError, AudioErrorCode

try:
    audio = load_wav(Path("samples/kick.wav"))
    print(audio.sample_rate_hz, audio.channels, audio.duration_seconds)
    left = audio.samples[:, 0]
except AudioReadError as error:
    if error.code == AudioErrorCode.NOT_FOUND:
        print("Choose an existing local file.")
    else:
        print(error.code.value, str(error))
```

## Result and conversion

- `samples`: NumPy float64 array with shape `(frame_count, channels)`, including
  `(frames, 1)` for mono. Channel order is preserved.
- `channels`: one or two; `sample_rate_hz`: native positive sample rate;
  `frame_count`: native number of frames; `duration_seconds`: exactly
  `frame_count / sample_rate_hz`; `subtype`: encoding name below.
- Signed integer PCM converts as `integer / 2**(bits - 1)`. PCM_U8 converts
  as `(integer - 128) / 128`. These binary fractions are exactly representable
  in float64, including 32-bit PCM. Float32 converts to float64 without further
  rounding; comparison to a pre-encoding signal permits absolute tolerance
  `1e-7` for normal amplitude values (the fixture matrix uses exact quarters).
  DOUBLE preserves float64 values. For arbitrary input quantized to integer PCM,
  compare against the encoded value or allow half a quantization step.
- Loading does not resample, downmix, trim, normalize or clip finite overrange
  float values. Silence and a one-frame clip are valid; zero-frame audio is not.

The shared `AudioMetadata` contract uses matching `sample_rate_hz`, `channels`
and `frame_count` units. When assembling that contract, application code supplies
its absolute local path and converts `duration_seconds * 1000` to `duration_ms`.
The reader result deliberately contains no derived features or judgment.

## Supported containers

Little-endian RIFF/WAVE, mono/stereo: PCM_U8, PCM_16, PCM_24, PCM_32, FLOAT
(32-bit IEEE), DOUBLE (64-bit IEEE). Tests cover every subtype/channel layout
at 44.1, 48 and 96 kHz. File extensions are ignored; renamed non-WAV data fails.
RIFX, RF64, WAVE_FORMAT_EXTENSIBLE, compressed encodings and >2-channel layouts
are outside this support matrix. Benign ancillary chunks and their odd-byte
padding are accepted. Required format/data chunks must occur once each.

The RIFF size must match the entire file, every chunk and its padding must be
present, block alignment/byte rate must agree with the format, and the payload
must contain complete frames. The strict reader rejects trailing bytes outside
the declared RIFF container and truncated files rather than returning a partial
decode. A snapshot is read with a read-only file handle, which closes before
decoding; SoundFile decodes that in-memory snapshot in a context manager. This
uses memory proportional to file size plus decoded float64 samples.

## Stable failures

`AudioReadError.code` is an `AudioErrorCode` enum. Messages explain the problem;
callers should branch on codes, never on underlying decoder exception text.

| Code | Meaning |
| --- | --- |
| `invalid_path` | Empty/malformed path, URL, UNC/network path or unsupported path argument |
| `not_found` | File does not exist |
| `not_file` | Path is a directory |
| `access_denied` | Filesystem permission failure |
| `io_error` | Other filesystem read failure |
| `unsupported_format` | Non-WAV container or unsupported WAV encoding |
| `unsupported_channels` | More than two channels |
| `empty_audio` | Structurally valid supported WAV with zero frames |
| `invalid_audio` | Malformed/truncated structure, inconsistent or invalid metadata, decoding failure, partial frames, NaN or infinity |

Files are read only and source bytes remain unchanged after either outcome.
Tests verify immediate rename/deletion after success and failures, and absence
of sidecar files, on the Windows development platform. Permission and generic
I/O failures use controlled failures because Windows permission fixtures are
not reliably portable.

Run the whole suite with `uv run pytest`. If the host's shared pytest temp
cleanup has a stale inaccessible junction, use
`uv run pytest --basetemp .pytest_cache/wav-reader-tmp`; this still runs the
whole suite and confines test artifacts to the ignored pytest cache.
