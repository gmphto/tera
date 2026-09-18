"""QA regression: excluded loudness tail must not affect conditioning."""

import math

import numpy as np
import pytest

from loudness_reference_signals import tone
from test_loudness import loaded, measures


@pytest.mark.parametrize("rate", [44100, 48000, 96000])
@pytest.mark.parametrize("hops", [0, 3])
@pytest.mark.parametrize("channels", [1, 2])
def test_extreme_discarded_tail_preserves_retained_lufs(rate, hops, channels):
    frames = rate * 4 // 10 + hops * (rate // 10)
    prefix = tone(rate, frames / rate)[:, None]
    prefix = np.repeat(prefix, channels, axis=1)
    baseline = measures(loaded(prefix, rate))["loudness"]
    # At the last possible sample before another complete block, the huge
    # outlier still belongs only to a discarded partial block.
    tail = np.zeros((rate // 10 - 1, channels))
    tail[-1, -1] = 1e308
    result = measures(loaded(np.concatenate((prefix, tail)), rate))
    assert baseline.value is not None
    assert result["loudness"] == baseline
    assert result["peak"].value == 1e308
    count = (len(prefix) + len(tail)) * channels
    assert result["rms"].value == pytest.approx(1e308 / math.sqrt(count), rel=1e-14)
    assert result["crest_factor"].value == pytest.approx(math.sqrt(count), rel=1e-14)


def test_exact_qa_single_sample_reproduction():
    x = tone(48000, seconds=0.4)
    base = measures(loaded(x))["loudness"]
    result = measures(loaded(np.append(x, 1e308)))["loudness"]
    assert result == base
    assert result.value == pytest.approx(-9.024495184403492, abs=1e-10)


def test_nonzero_tail_does_not_create_retained_signal():
    result = measures(loaded(np.append(np.zeros(19200), 1e308)))
    assert result["loudness"].unavailable_reason == "below_loudness_gate"
    assert result["peak"].value == 1e308
