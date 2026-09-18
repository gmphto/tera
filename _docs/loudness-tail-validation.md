# Loudness tail conditioning regression

Independent [QA](https://github.com/gmphto/tera/issues/5#issuecomment-5736283519)
found that conditioning LUFS with the full-clip peak let an ignored tail sample
change the result. A 400 ms, 0.5-peak mono 1 kHz tone measures
-9.024495184403492 LUFS at 48 kHz; appending one finite `1e308` sample must leave
that loudness unchanged, because no new complete gating block exists.

LUFS now determines the final complete-block endpoint before selecting its
conditioning peak and filtering. Only the retained prefix participates. RMS,
sample peak and crest still measure every sample in the original clip.
Retained digital zeros followed only by a nonzero discarded tail produce
`below_loudness_gate`, without inventing a signal in the retained interval.

`tests/test_loudness_tail.py` includes the exact QA reproduction, three-rate
mono/stereo checks after one and several complete blocks, an outlier just
before the next possible block endpoint, and independent whole-clip linear
expectations. The correction enforces the existing v1 tail semantics before
issue acceptance; it does not introduce a new analysis policy.
