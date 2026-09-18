"""Controlled correlation evidence verifies the hard periodicity gate."""

import numpy as np
import pytest

from backend.analysis import harmony
from harmony_reference_signals import tone


@pytest.mark.parametrize("quality,accepted", [(.90, True), (.8999, False)])
def test_periodicity_threshold_equality(monkeypatch, quality, accepted):
    def correlation(a, b, **kwargs):
        result = np.zeros(2*len(a)-1)
        for lag, value in ((99, quality-.01), (100, quality), (101, quality-.01)):
            denominator = np.sqrt(np.sum(a[:-lag]**2)*np.sum(a[lag:]**2))
            result[len(a)-1+lag] = value*denominator
        return result
    monkeypatch.setattr(harmony.signal, "correlate", correlation)
    frequency, periodicity, reason = harmony._frame_pitch(tone(110, duration=.2), 48000)
    if accepted:
        assert frequency == pytest.approx(480, abs=1e-9)
        assert periodicity == pytest.approx(.90, abs=1e-12)
    else:
        assert frequency is None and reason == "insufficient_periodicity"
