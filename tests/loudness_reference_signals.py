"""Original synthesized signals; expectations transcribed from EBU Tech3341.

Table 1 cases 1–5, November 2023: https://tech.ebu.ch/docs/tech/tech3341.pdf.
No downloaded audio, implementation-derived calibration, or network calls.
"""

import numpy as np


EBU_CASES = {
    1: ([(20, -23)], -23.0),
    2: ([(20, -33)], -33.0),
    3: ([(10, -36), (60, -23), (10, -36)], -23.0),
    4: ([(10, -72), (10, -36), (60, -23), (10, -36), (10, -72)], -23.0),
    5: ([(20, -26), (20.1, -20), (20, -26)], -23.0),
}


def tone(rate, seconds=1, peak=0.5, frequency=1000):
    return peak * np.sin(2 * np.pi * frequency * np.arange(round(rate * seconds)) / rate)


def ebu_signal(case, rate):
    segments, _ = EBU_CASES[case]
    mono = np.concatenate([tone(rate, seconds, 10**(dbfs / 20)) for seconds, dbfs in segments])
    return np.column_stack((mono, mono))
