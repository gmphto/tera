"""Strict local RIFF/WAVE loading; see _docs/wav-reader.md."""

from dataclasses import dataclass
from enum import StrEnum
import io
import os
from pathlib import Path
import struct

import numpy as np
from numpy.typing import NDArray
import soundfile as sf


class AudioErrorCode(StrEnum):
    INVALID_PATH = "invalid_path"
    NOT_FOUND = "not_found"
    NOT_FILE = "not_file"
    ACCESS_DENIED = "access_denied"
    IO_ERROR = "io_error"
    UNSUPPORTED_FORMAT = "unsupported_format"
    UNSUPPORTED_CHANNELS = "unsupported_channels"
    EMPTY_AUDIO = "empty_audio"
    INVALID_AUDIO = "invalid_audio"


class AudioReadError(ValueError):
    """Stable failure code plus a useful message; never parse decoder text."""

    def __init__(self, code: AudioErrorCode, message: str):
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class LoadedAudio:
    samples: NDArray[np.float64]
    channels: int
    sample_rate_hz: int
    frame_count: int
    duration_seconds: float
    subtype: str


_SUBTYPES = {(1, 8): "PCM_U8", (1, 16): "PCM_16", (1, 24): "PCM_24",
             (1, 32): "PCM_32", (3, 32): "FLOAT", (3, 64): "DOUBLE"}


def _invalid(message: str) -> AudioReadError:
    return AudioReadError(AudioErrorCode.INVALID_AUDIO, message)


def _validate_riff(audio: bytes) -> tuple[int, int, int, str]:
    if len(audio) < 12:
        raise _invalid("Audio is empty or has a truncated container header.")
    if audio[:4] != b"RIFF" or audio[8:12] != b"WAVE":
        raise AudioReadError(AudioErrorCode.UNSUPPORTED_FORMAT, "Only little-endian RIFF/WAVE is supported.")
    if struct.unpack_from("<I", audio, 4)[0] + 8 != len(audio):
        raise _invalid("RIFF size does not match the file length; audio may be truncated.")
    chunks = {}
    offset = 12
    while offset < len(audio):
        if offset + 8 > len(audio):
            raise _invalid("Truncated WAV chunk header.")
        name, size = struct.unpack_from("<4sI", audio, offset)
        start = offset + 8
        end = start + size
        if end + (size % 2) > len(audio):
            raise _invalid("Truncated WAV chunk payload or padding.")
        if name in (b"fmt ", b"data"):
            if name in chunks:
                raise _invalid("Duplicate WAV format or audio data chunk.")
            chunks[name] = (start, size)
        offset = end + (size % 2)
    if b"fmt " not in chunks or b"data" not in chunks:
        raise _invalid("WAV requires format and audio data chunks.")
    start, size = chunks[b"fmt "]
    if size < 16:
        raise _invalid("WAV format chunk is too short.")
    tag, channels, rate, byte_rate, align, bits = struct.unpack_from("<HHIIHH", audio, start)
    if channels == 0 or rate == 0 or rate > 2**31 - 1:
        raise _invalid("WAV channel count and sample rate must be valid positive values.")
    if channels > 2:
        raise AudioReadError(AudioErrorCode.UNSUPPORTED_CHANNELS, "Only mono and stereo WAV layouts are supported.")
    subtype = _SUBTYPES.get((tag, bits))
    if subtype is None:
        raise AudioReadError(AudioErrorCode.UNSUPPORTED_FORMAT, "WAV encoding is not supported (use integer PCM or 32/64-bit float).")
    if tag != 1 and size > 16:
        # Legacy 16-byte float fmt chunks are accepted. Once WAVEFORMATEX
        # is present, cbSize must be complete and describe the actual bytes.
        # PCM explicitly ignores cbSize, so this check is non-PCM only.
        if size < 18:
            raise _invalid("WAV format extension size field is incomplete.")
        extension_size = struct.unpack_from("<H", audio, start + 16)[0]
        if extension_size != size - 18:
            raise _invalid("WAV format extension length disagrees with its declared size.")
    if align != channels * (bits // 8) or byte_rate != rate * align:
        raise _invalid("WAV block alignment or byte rate disagrees with its format.")
    data_size = chunks[b"data"][1]
    if data_size % align:
        raise _invalid("WAV audio ends in a partial frame.")
    frames = data_size // align
    if frames == 0:
        raise AudioReadError(AudioErrorCode.EMPTY_AUDIO, "WAV contains zero audio frames.")
    return channels, rate, frames, subtype


def load_wav(path: str | os.PathLike[str]) -> LoadedAudio:
    """Read a local WAV as float64 frames×channels without transforming audio.

    Integer PCM divides signed values by 2**(bits-1); unsigned 8-bit first
    subtracts 128 and divides by 128. Finite float overrange is preserved.
    Raises AudioReadError with an AudioErrorCode; never rewrites source audio.
    """
    try:
        filename = os.fspath(path)
        if not isinstance(filename, str) or not filename or "\0" in filename or "://" in filename or filename.startswith(("\\\\", "//")):
            raise ValueError("Expected a local filesystem path.")
        local_path = Path(filename)
    except (TypeError, ValueError) as error:
        raise AudioReadError(AudioErrorCode.INVALID_PATH, "Expected a valid local filesystem path.") from error
    try:
        if local_path.is_dir():
            raise AudioReadError(AudioErrorCode.NOT_FILE, "Path names a directory, not an audio file.")
        with local_path.open("rb") as source:
            audio = source.read()
    except FileNotFoundError as error:
        raise AudioReadError(AudioErrorCode.NOT_FOUND, "Audio file was not found.") from error
    except IsADirectoryError as error:
        raise AudioReadError(AudioErrorCode.NOT_FILE, "Path names a directory, not an audio file.") from error
    except PermissionError as error:
        raise AudioReadError(AudioErrorCode.ACCESS_DENIED, "Permission denied while reading audio.") from error
    except OSError as error:
        raise AudioReadError(AudioErrorCode.IO_ERROR, "Filesystem error while reading audio.") from error
    channels, rate, frames, subtype = _validate_riff(audio)
    try:
        with sf.SoundFile(io.BytesIO(audio)) as decoder:
            if (decoder.channels, decoder.samplerate, decoder.frames, decoder.subtype) != (channels, rate, frames, subtype):
                raise _invalid("Decoded WAV metadata disagrees with the container.")
            samples = decoder.read(dtype="float64", always_2d=True)
    except (sf.SoundFileError, ValueError) as error:
        if isinstance(error, AudioReadError):
            raise
        raise _invalid("WAV could not be decoded.") from error
    if samples.shape != (frames, channels) or not np.isfinite(samples).all():
        raise _invalid("WAV has incomplete frames or non-finite sample values.")
    return LoadedAudio(samples, channels, rate, frames, frames / rate, subtype)
