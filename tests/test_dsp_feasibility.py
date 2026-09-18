"""Local synthetic WAV experiment for #3; not a production audio reader.

Run `uv run python tests/test_dsp_feasibility.py` for JSON evidence on stdout.
No audio, file paths or evidence is uploaded. No dependencies are installed.
"""

import importlib
import importlib.metadata
import io
import json
import math
import platform
import struct
import sys
import wave

import pytest


SUBTYPES = {"PCM_U8": (1, 8), "PCM_16": (1, 16), "PCM_24": (1, 24),
            "PCM_32": (1, 32), "FLOAT": (3, 32), "DOUBLE": (3, 64)}
RATE = 48000


def wav_bytes(subtype, channels):
    """Generate original, permitted one-second tones without DSP dependencies."""
    tag, bits = SUBTYPES[subtype]
    data = bytearray()
    for frame in range(RATE):
        for channel in range(channels):
            value = (0.5 / (channel + 1)) * math.sin(2 * math.pi * 440 * frame / RATE)
            if tag == 3:
                data.extend(struct.pack("<f" if bits == 32 else "<d", value))
            elif bits == 8:
                data.append(round(value * 128) + 128)
            else:
                data.extend(round(value * (1 << (bits - 1))).to_bytes(bits // 8, "little", signed=True))
    align = channels * bits // 8
    fmt = struct.pack("<HHIIHH", tag, channels, RATE, RATE * align, align, bits)
    body = b"WAVEfmt " + struct.pack("<I", len(fmt)) + fmt
    if tag == 3:
        body += b"fact" + struct.pack("<II", 4, RATE)
    body += b"data" + struct.pack("<I", len(data)) + data
    return b"RIFF" + struct.pack("<I", len(body)) + body


def fixtures():
    for subtype in SUBTYPES:
        for channels in (1, 2):
            yield f"{subtype}_{channels}ch", wav_bytes(subtype, channels), subtype, channels
    yield "corrupt", b"RIFF\x00\x00", None, None
    unsupported = bytearray(wav_bytes("PCM_16", 1))
    struct.pack_into("<H", unsupported, 20, 0xffff)
    yield "unsupported_codec", bytes(unsupported), None, None


def probe():
    report = {"environment": {"os": platform.platform(), "architecture": platform.machine(),
                              "python": platform.python_version()}, "dependencies": {}, "fixtures": []}
    modules = {}
    for name in ("numpy", "scipy", "soundfile", "essentia"):
        try:
            version = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            report["dependencies"][name] = {"status": "unavailable", "version": None}
            continue
        try:
            modules[name] = importlib.import_module(name)
            report["dependencies"][name] = {"status": "import_pass", "version": version}
        except Exception as error:
            report["dependencies"][name] = {"status": "import_fail", "version": version,
                                             "error_type": type(error).__name__}
    if "soundfile" in modules:
        report["libsndfile"] = modules["soundfile"].__libsndfile_version__
    for name, audio, subtype, channels in fixtures():
        row = {"fixture": name, "expected_subtype": subtype, "expected_channels": channels}
        report["fixtures"].append(row)
        if not all(name in modules for name in ("numpy", "scipy", "soundfile")):
            row["status"] = "unrun_dependencies_unavailable"
            continue
        np, sf = modules["numpy"], modules["soundfile"]
        signal = importlib.import_module("scipy.signal")
        try:
            with sf.SoundFile(io.BytesIO(audio)) as handle:
                row.update(sample_rate=handle.samplerate, channels=handle.channels,
                           duration_seconds=handle.frames / handle.samplerate,
                           frames=handle.frames, subtype=handle.subtype)
                samples = handle.read(dtype="float64", always_2d=True)
        except sf.LibsndfileError as error:
            row.update(status="reject_pass" if subtype is None else "read_fail",
                       error_type=type(error).__name__)
            continue
        if subtype is None:
            row["status"] = "unexpected_read"
            continue
        # Channel-wise measurement prevents averaging stereo channels from hiding errors.
        row["rms"] = np.sqrt(np.mean(samples**2, axis=0)).tolist()
        row["peak"] = np.max(np.abs(samples), axis=0).tolist()
        spectrum = np.abs(np.fft.rfft(samples * np.hanning(len(samples))[:, None], axis=0))
        frequencies = np.fft.rfftfreq(len(samples), 1 / RATE)
        row["centroid_hz"] = (np.sum(spectrum * frequencies[:, None], axis=0) / spectrum.sum(axis=0)).tolist()
        row["dominant_hz"] = frequencies[np.argmax(spectrum, axis=0)].tolist()
        pitches = []
        for channel in samples.T:
            # Only a pure-tone smoke experiment, not a robust F0 estimator.
            correlation = signal.correlate(channel, channel, mode="full", method="fft")[len(channel)-1:]
            peaks, _ = signal.find_peaks(correlation[RATE // 1000:RATE // 80])
            pitches.append(float(RATE / (RATE // 1000 + peaks[0])) if len(peaks) else None)
        row["pitch_hz"] = pitches
        row["lufs"] = "unrun_no_meter"
        expected_rms = [0.5 / (channel + 1) / math.sqrt(2) for channel in range(channels)]
        checks = [row["sample_rate"] == RATE, row["channels"] == channels,
                  row["frames"] == RATE, row["subtype"] == subtype,
                  np.allclose(row["rms"], expected_rms, atol=0.004),
                  np.allclose(row["peak"], [0.5 / (ch + 1) for ch in range(channels)], atol=0.004),
                  all(pitch is not None and abs(pitch - 440) < 5 for pitch in pitches),
                  np.allclose(row["dominant_hz"], 440, atol=1),
                  all(math.isfinite(value) and 0 <= value <= RATE / 2 for value in row["centroid_hz"])]
        # Low-bit quantization shifts magnitude centroids away from the pure tone.
        # The dominant bin is checked for every encoding; near-ideal centroid
        # assertions only apply where quantization is negligible.
        if subtype not in ("PCM_U8", "PCM_16"):
            checks.append(all(abs(value - 440) < 1 for value in row["centroid_hz"]))
        row["status"] = "pass" if all(checks) else "measurement_fail"
    return report


@pytest.mark.parametrize("subtype", ["PCM_U8", "PCM_16", "PCM_24", "PCM_32"])
@pytest.mark.parametrize("channels", [1, 2])
def test_integer_fixtures_with_independent_stdlib_reader(subtype, channels):
    with wave.open(io.BytesIO(wav_bytes(subtype, channels))) as reader:
        assert reader.getnchannels() == channels
        assert reader.getframerate() == RATE
        assert reader.getnframes() == RATE
        assert reader.getsampwidth() == SUBTYPES[subtype][1] // 8
        assert len(reader.readframes(RATE)) == RATE * channels * reader.getsampwidth()


def test_matrix_includes_both_float_widths_and_negative_cases():
    matrix = list(fixtures())
    assert len(matrix) == 14
    assert {name for name, _, _, _ in matrix if name.startswith(("FLOAT", "DOUBLE"))} == {
        "FLOAT_1ch", "FLOAT_2ch", "DOUBLE_1ch", "DOUBLE_2ch"}
    for _, audio, subtype, _ in matrix:
        if subtype is None:
            with pytest.raises((wave.Error, EOFError)):
                wave.open(io.BytesIO(audio))


def test_installed_runtime_matrix():
    for name in ("numpy", "scipy", "soundfile"):
        pytest.importorskip(name)
    report = probe()
    assert all(row["status"] in ("pass", "reject_pass") for row in report["fixtures"]), report


if __name__ == "__main__":
    result = probe()
    print(json.dumps(result, indent=2, allow_nan=False))
    statuses = {row["status"] for row in result["fixtures"]}
    sys.exit(2 if "unrun_dependencies_unavailable" in statuses else
             int(not statuses <= {"pass", "reject_pass"}))
