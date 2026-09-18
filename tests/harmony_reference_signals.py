"""Musically declared fixtures, authored before running the estimator.

Major: a familiar tonic/dominant nursery-phrase contour, ending on tonic.
Minor: its parallel-minor counterpart with lowered third and sixth.
Labels follow the written tonal design, not fitted profile proportions.
"""

import numpy as np


MAJOR = [(60, 1), (60, 1), (67, 1), (67, 1), (69, 1), (69, 1), (67, 2),
         (65, 1), (65, 1), (64, 1), (64, 1), (62, 1), (62, 1), (60, 2)]
MINOR = [(60, 1), (60, 1), (67, 1), (67, 1), (68, 1), (68, 1), (67, 2),
         (65, 1), (65, 1), (63, 1), (63, 1), (62, 1), (62, 1), (60, 2)]
# Equal-duration shared C-major/A-minor scale, with no tonic/cadential emphasis.
AMBIGUOUS = [(note, 1) for note in (60, 62, 64, 65, 67, 69, 71)] * 2


def tone(frequency, rate=48000, duration=1, amplitude=.5):
    return amplitude * np.sin(2*np.pi*frequency*np.arange(round(rate*duration))/rate)


def phrase(notes, rate=48000, transpose=0):
    # Half-second beats, constant .5 peak, phase resets at each written note.
    return np.concatenate([tone(440*2**((note+transpose-69)/12), rate, beats*.5)
                           for note, beats in notes])
