"""Regressions for issue #4's independent QA malformed fmt finding."""

import struct

import numpy as np
import pytest

from backend.audio import AudioErrorCode as Code, load_wav
from test_audio import FORMATS, chunk, failure, wav, write


def with_format_suffix(original, suffix):
    body = b"WAVE" + chunk(b"fmt ", original[20:36] + suffix) + original[36:]
    return b"RIFF" + struct.pack("<I", len(body)) + body


@pytest.mark.parametrize("subtype", ["FLOAT", "DOUBLE"])
@pytest.mark.parametrize("suffix", [b"x", struct.pack("<H", 99), struct.pack("<H", 0) + b"x"])
def test_malformed_non_pcm_format_extension(tmp_path, subtype, suffix):
    original = wav([[0.5]], subtype=subtype)
    failure(write(tmp_path, with_format_suffix(original, suffix)), Code.INVALID_AUDIO)


@pytest.mark.parametrize("subtype", FORMATS)
def test_valid_zero_length_format_extension(tmp_path, subtype):
    original = wav([[0.5]], subtype=subtype)
    result = load_wav(write(tmp_path, with_format_suffix(original, struct.pack("<H", 0))))
    np.testing.assert_array_equal(result.samples, [[0.5]])


def test_pcm_ignores_extension_size_field(tmp_path):
    original = wav([[0.5]], subtype="PCM_16")
    result = load_wav(write(tmp_path, with_format_suffix(original, struct.pack("<H", 99))))
    np.testing.assert_array_equal(result.samples, [[0.5]])
