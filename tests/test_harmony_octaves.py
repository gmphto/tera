"""Independent QA regressions: weak55Hz component under dominant110Hz."""

import pytest

from harmony_reference_signals import tone
from test_harmony import estimate, cents


@pytest.mark.parametrize("rate", [44100, 48000, 96000])
@pytest.mark.parametrize("weak", [.003, .001, .0001])
def test_weak_fundamental_never_confidently_doubles(rate, weak):
    result = estimate(weak*tone(55, rate)+tone(110, rate), rate).fundamental
    assert (result.value is not None and cents(result.value, 55) <= 10) or result.unavailable_reason == "octave_ambiguity"


@pytest.mark.parametrize("frequency", [55, 110, 261.6255653, 900, 1000])
def test_clean_interpolation_error_is_not_subharmonic_evidence(frequency):
    result = estimate(tone(frequency)).fundamental
    assert result.value is not None and cents(result.value, frequency) <= 10
