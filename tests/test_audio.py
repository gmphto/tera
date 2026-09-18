import struct
from pathlib import Path

import numpy as np
import pytest

from backend.audio import AudioErrorCode as Code, AudioReadError, load_wav


FORMATS = {"PCM_U8": (1, 8), "PCM_16": (1, 16), "PCM_24": (1, 24),
           "PCM_32": (1, 32), "FLOAT": (3, 32), "DOUBLE": (3, 64)}


def chunk(name, payload):
    return name + struct.pack("<I", len(payload)) + payload + b"\0" * (len(payload) % 2)


def wav(samples, rate=48000, subtype="PCM_16", extra=b""):
    samples = np.asarray(samples)
    channels = samples.shape[1]
    tag, bits = FORMATS[subtype]
    data = bytearray()
    for value in samples.flat:
        if tag == 3:
            data.extend(struct.pack("<f" if bits == 32 else "<d", value))
        elif bits == 8:
            data.append(round(float(value) * 128) + 128)
        else:
            data.extend(round(float(value) * 2**(bits - 1)).to_bytes(bits // 8, "little", signed=True))
    align = channels * bits // 8
    fmt = struct.pack("<HHIIHH", tag, channels, rate, rate * align, align, bits)
    body = b"WAVE" + chunk(b"fmt ", fmt) + extra + chunk(b"data", data)
    return b"RIFF" + struct.pack("<I", len(body)) + body


def write(tmp_path, audio, name="sample.wav"):
    path = tmp_path / name
    path.write_bytes(audio)
    return path


def failure(path, code):
    with pytest.raises(AudioReadError) as caught:
        load_wav(path)
    assert caught.value.code is code
    assert str(caught.value)


@pytest.mark.parametrize("subtype", FORMATS)
@pytest.mark.parametrize("rate", [44100, 48000, 96000])
@pytest.mark.parametrize("channels", [1, 2])
def test_all_subtypes_native_rates_channels_and_scale(tmp_path, subtype, rate, channels):
    samples = np.array([[-1, 0.5], [-0.25, 0.25], [0, -0.75], [0.75, -0.5]])[:, :channels]
    path = write(tmp_path, wav(samples, rate, subtype), "no-extension")
    result = load_wav(path)
    assert result.samples.dtype == np.float64
    assert result.samples.shape == (4, channels)
    assert (result.channels, result.sample_rate_hz, result.frame_count, result.subtype) == (channels, rate, 4, subtype)
    assert result.duration_seconds == 4 / rate
    np.testing.assert_allclose(result.samples, samples, rtol=0, atol=1e-7)


@pytest.mark.parametrize("subtype", ["PCM_U8", "PCM_16", "PCM_24", "PCM_32"])
def test_full_integer_scale(tmp_path, subtype):
    bits = FORMATS[subtype][1]
    values = np.array([[-1.0], [0], [1 - 2**(1-bits)]])
    result = load_wav(write(tmp_path, wav(values, subtype=subtype)))
    np.testing.assert_array_equal(result.samples, values)


@pytest.mark.parametrize("subtype", ["FLOAT", "DOUBLE"])
def test_float_overrange_not_clipped_or_normalized(tmp_path, subtype):
    values = np.array([[-2.5, 3.25], [0.125, -0.0625]])
    result = load_wav(write(tmp_path, wav(values, subtype=subtype)))
    np.testing.assert_array_equal(result.samples, values)


@pytest.mark.parametrize("frames", [1, 7])
@pytest.mark.parametrize("channels", [1, 2])
def test_nonempty_silence_and_one_frame_are_valid(tmp_path, frames, channels):
    values = np.zeros((frames, channels))
    result = load_wav(write(tmp_path, wav(values)))
    assert result.frame_count == frames
    assert result.duration_seconds == frames / 48000
    np.testing.assert_array_equal(result.samples, values)


def test_empty_audio_distinct_from_empty_file(tmp_path):
    failure(write(tmp_path, wav(np.zeros((0, 1)))), Code.EMPTY_AUDIO)
    failure(write(tmp_path, b""), Code.INVALID_AUDIO)


def test_odd_ancillary_chunk_and_single_8bit_frame_padding(tmp_path):
    audio = wav([[0.5]], subtype="PCM_U8", extra=chunk(b"JUNK", b"abc"))
    assert load_wav(write(tmp_path, audio)).samples[0, 0] == 0.5
    failure(write(tmp_path, audio[:-1]), Code.INVALID_AUDIO)


@pytest.mark.parametrize("offset,value", [(22, 0), (24, 0), (24, 0xffffffff), (28, 1), (32, 1)])
def test_invalid_format_metadata(tmp_path, offset, value):
    audio = bytearray(wav([[0.5]]))
    struct.pack_into("<H" if offset in (22, 32) else "<I", audio, offset, value)
    failure(write(tmp_path, audio), Code.INVALID_AUDIO)


@pytest.mark.parametrize("tag,bits", [(6, 8), (0xffff, 16), (0xfffe, 16), (1, 12), (3, 16)])
def test_unsupported_encodings(tmp_path, tag, bits):
    audio = bytearray(wav([[0.5]]))
    struct.pack_into("<H", audio, 20, tag)
    struct.pack_into("<H", audio, 34, bits)
    failure(write(tmp_path, audio), Code.UNSUPPORTED_FORMAT)


def test_unsupported_channels(tmp_path):
    failure(write(tmp_path, wav([[0, 0, 0]])), Code.UNSUPPORTED_CHANNELS)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
@pytest.mark.parametrize("subtype", ["FLOAT", "DOUBLE"])
def test_nonfinite_audio(tmp_path, value, subtype):
    failure(write(tmp_path, wav([[value]], subtype=subtype)), Code.INVALID_AUDIO)


def test_truncation_and_malformed_chunks(tmp_path):
    original = wav([[0.5], [0.25]])
    variants = [b"RIFF", original[:20], original[:-2]]
    # Make RIFF size agree with a truncated data chunk: still invalid.
    repaired_riff = bytearray(original[:-2])
    struct.pack_into("<I", repaired_riff, 4, len(repaired_riff) - 8)
    variants.append(repaired_riff)
    # Both lengths agree but payload has a partial 16-bit frame.
    partial = bytearray(original[:-1] + b"\0")
    struct.pack_into("<I", partial, 40, 3)
    variants.append(partial)
    # Duplicate required chunks, missing fmt/data, and short fmt are malformed.
    for body in [b"WAVE" + original[12:36] * 2 + original[36:],
                 b"WAVE" + original[36:], b"WAVE" + original[12:36],
                 b"WAVE" + chunk(b"fmt ", b"a") + chunk(b"data", b"aa")]:
        variants.append(b"RIFF" + struct.pack("<I", len(body)) + body)
    for audio in variants:
        failure(write(tmp_path, audio), Code.INVALID_AUDIO)


@pytest.mark.parametrize("header", [b"fLaC", b"FORM", b"RIFX", b"RF64"])
def test_nonwav_renamed_as_wav_and_other_containers(tmp_path, header):
    failure(write(tmp_path, header + b"\0" * 32), Code.UNSUPPORTED_FORMAT)


def test_filesystem_categories(tmp_path, monkeypatch):
    failure(tmp_path / "missing.wav", Code.NOT_FOUND)
    failure(tmp_path, Code.NOT_FILE)
    path = write(tmp_path, wav([[0]]))
    for exception, code in [(PermissionError("denied"), Code.ACCESS_DENIED), (OSError("I/O"), Code.IO_ERROR)]:
        def denied(*args, **kwargs):
            raise exception
        with monkeypatch.context() as patch:
            patch.setattr(Path, "open", denied)
            failure(path, code)


@pytest.mark.parametrize("path", [None, b"file.wav", "", "bad\0path", "https://example.org/a.wav", "//server/share/a.wav"])
def test_nonlocal_and_invalid_paths(path):
    failure(path, Code.INVALID_PATH)


@pytest.mark.parametrize("outcome", ["success", "bad_header", "bad_samples"])
def test_source_unchanged_no_sidecars_and_handles_closed(tmp_path, outcome):
    audio = (wav([[0.5]]) if outcome == "success" else
             b"broken" if outcome == "bad_header" else wav([[float("nan")]], subtype="FLOAT"))
    path = write(tmp_path, audio)
    if outcome == "success":
        load_wav(path)
    else:
        failure(path, Code.INVALID_AUDIO)
    assert path.read_bytes() == audio
    assert list(tmp_path.iterdir()) == [path]
    renamed = path.rename(tmp_path / "renamed.wav")
    renamed.unlink()
    assert not list(tmp_path.iterdir())
