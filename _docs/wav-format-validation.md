# WAV format extension validation

Supplement to [the reader contract](wav-reader.md), responding to the independent
[QA finding on issue #4](https://github.com/gmphto/tera/issues/4#issuecomment-5736096172).

For supported non-PCM FLOAT/DOUBLE encodings, an extended `fmt ` chunk must
include a complete two-byte `cbSize` field whose value matches the actual
extension bytes. Incomplete fields and mismatched lengths return `invalid_audio`
before SoundFile decoding. A 17-byte format payload therefore fails, as does
an 18-byte payload claiming 99 absent extension bytes.

Legacy 16-byte float format chunks remain supported. An 18-byte chunk with
`cbSize=0` is valid for all six supported subtypes. PCM ignores `cbSize` per
the [WAVEFORMATEX specification](https://learn.microsoft.com/en-us/windows/win32/api/mmeapi/ns-mmeapi-waveformatex);
the reader does not apply non-PCM length rules to that ignored field.

`tests/test_audio_format_extensions.py` covers both floating-point widths,
truncated/mismatched extension declarations, valid zero-length controls for
all six subtypes and the PCM ignored-field behavior.
