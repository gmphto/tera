# Producer labeling session (#18)

## State

**`insufficient evidence — no evaluator session run`.**

Named cause: a prerequisite artifact is missing. [#65](https://github.com/gmphto/tera/issues/65)
has not landed the pair sampler, the split manifest or the evaluator assignment, so the preflight
this issue requires fails before `start` can be called and #18 takes the blocked path. No session
directory was created, no presentation was played, no `export.json` was written and no rating of
any kind was recorded.

## The blocker

| Prerequisite | State |
| --- | --- |
| #16 — `_docs/evaluation-protocol.md` at `tera-eval-protocol-v1` | landed |
| #17 — `_docs/pair-rating-workflow.md` and `backend/evaluation/rating.py` | landed |
| #65 — pair sampler, split manifest and evaluator assignment | **missing; #65 is open** |

Evidence:

* `backend/evaluation/sampling.py` does not exist.
* `python -m backend.evaluation.sampling validate` exits 1 with
  `No module named backend.evaluation.sampling`.
* `.local-evaluation/pair-rating/` does not exist: no pair list, no split manifest, no
  assignment, no `sessions/` directory, no `answers.jsonl`, no `export.json`.

#18 must not re-derive seeds, splits or assignments by hand and must not substitute synthetic
input, so there is nothing to rate and no legitimate way to start a session. The issue stays open
for the real session.

## Preflight results

| # | Preflight item | Result |
| --- | --- | --- |
| 1 | `_docs/evaluation-protocol.md` exists and declares `PROTOCOL_VERSION = "tera-eval-protocol-v1"` | pass |
| 2 | `_docs/pair-rating-workflow.md` exists | pass |
| 3 | `python -m backend.evaluation.rating --help` lists exactly `start`, `resume`, `status`, `export`, `validate` | pass |
| 4 | a #65 pair list, split manifest and assignment exist and pass `python -m backend.evaluation.sampling validate` with `schema_version` `1.0` | **fail — no command, no artifacts** |
| 5 | the pair list's `split_manifest_digest` equals the split manifest's `sha256:` digest | not checkable — no manifest |
| 6 | the assignment gives every pair at least `MIN_RATINGS_PER_PAIR = 2` distinct evaluator ids and never assigns one pair twice to one evaluator | not checkable — no assignment |
| 7 | the pair list holds at least `MIN_HELDOUT_PAIRS = 300` pairs spanning at least `MIN_HELDOUT_QUERIES = 60` distinct held-out query kicks | not checkable — no pair list (0 pairs, 0 query kicks) |
| 8 | every pair element is a held-out sample of the recorded `dataset_version` | not checkable — no pair list |

Threshold values read from the frozen protocol document for the checks above (none was lowered,
redefined or skipped): `MIN_EVALUATORS = 5`, `TARGET_EVALUATORS = 8`,
`MIN_RATINGS_PER_PAIR = 2`, `TARGET_RATINGS_PER_PAIR = 3`, `MIN_HELDOUT_PAIRS = 300`,
`MIN_HELDOUT_QUERIES = 60`, `TARGET_HELDOUT_QUERIES = 75`,
`RATED_CANDIDATES_PER_QUERY = 5`, `MIN_POOL_KICKS = 80`, `MIN_POOL_BASSES = 8`,
`MIN_PAIR_COVERAGE = 0.80`, `MAX_RECOGNISED_RATE = 0.20`, `MIN_AGREEMENT_PAIRS = 40`,
`MAX_MEAN_ABSOLUTE_DEVIATION = 1.00`, `MIN_RATED_CANDIDATES_FOR_TOP10_GATE = 11`.

Version strings: `PROTOCOL_VERSION = tera-eval-protocol-v1`, declared in the protocol document
and pinned by `backend/evaluation/rating.py` together with `ORDER_SEED = tera-eval-order-16-v1`.
`dataset_version` is **not recorded** — it is copied from the missing #65 pair list — and there
is no `split_manifest_digest` to report. No session date, no monitoring chain and no session row
exists, because no session ran.

## Zero counts

| Count | Value | Threshold it is compared with |
| --- | ---: | --- |
| counted evaluators | 0 | `MIN_EVALUATORS = 5` (target 8) |
| sessions | 0 | — |
| presentations | 0 | — |
| ratings | 0 | — |
| valid exports | 0 | — |
| sampled pairs | 0 | `MIN_HELDOUT_PAIRS = 300` |
| distinct held-out query kicks | 0 | `MIN_HELDOUT_QUERIES = 60` (target 75) |
| pairs with at least two valid ratings | 0 | `MIN_AGREEMENT_PAIRS = 40` |
| recognised ratings | 0 | `MAX_RECOGNISED_RATE = 0.20` |
| failed playbacks | 0 | — |
| session `validate` runs that returned `valid: true` | 0 | — |

`pair_coverage` has numerator 0 and denominator 0 sampled pairs, so the ratio is undefined
rather than 1.00 or 0.00; nothing was rounded, replaced or inferred. Sessions below a minimum,
including the zero case, are insufficient evidence and never pass or fail.

## Recruitment requirement and rating burden

* Counted evaluators: at least `MIN_EVALUATORS = 5` (target `TARGET_EVALUATORS = 8`), each a
  producer who can hear the pairs on the session's monitoring and had no involvement in
  implementing or tuning the arms judged; the #16/#17/#65 runner is not one of them.
* Ratings per pair: at least `MIN_RATINGS_PER_PAIR = 2` distinct evaluators
  (target `TARGET_RATINGS_PER_PAIR = 3`).
* Rating burden: the minimum design needs 300 pairs x 2 = **600 ratings**, about 120 per evaluator
  at 5 evaluators; the target design needs 375 pairs x 3 = **1125 ratings**, about 141 per
  evaluator at 8 evaluators.
* Unblocking #18 also needs #65's pair list, split manifest and assignment. Recruitment and
  briefing a standing panel sit outside this task
  ([#66](https://github.com/gmphto/tera/issues/66)); growing the pool is out of scope
  ([#10](https://github.com/gmphto/tera/issues/10), [#61](https://github.com/gmphto/tera/issues/61),
  bass shortfall [#67](https://github.com/gmphto/tera/issues/67)).

**No rating of any kind was recorded.** No placeholder, agent-generated, synthetic or self-rated
answer exists anywhere, and no session directory or `export.json` was created for a session that
did not happen.

## Reproduction commands

Run from the repository root; logs and pytest basetemps went to an operator scratch directory
outside the repository.

```sh
.venv/Scripts/python.exe -m backend.evaluation.rating --help
# exit 0; usage shows exactly {start,resume,status,export,validate}

.venv/Scripts/python.exe -m backend.evaluation.sampling validate
# exit 1; stderr: No module named backend.evaluation.sampling

.venv/Scripts/python.exe -m backend.evaluation.rating validate --session .local-evaluation/pair-rating/sessions/<session_id>
# exit 2; stderr: session_unreadable: The session directory does not hold a session file.

.venv/Scripts/python.exe -m pytest -q -p no:cacheprovider --basetemp <scratch>/bt-eng18
# 4 failed, 1533 passed, 1 skipped in 85.97s
```

Aggregation command: **none**. There are no exports to aggregate — 0 sessions, 0 ratings — so no
aggregation script was written or run. Per the issue such a script would have to live under the
gitignored `.local-evaluation/pair-rating/`; nothing was written there.

## Test suite

`pytest` on the untouched tree: **4 failed, 1533 passed, 1 skipped** (85.97 s, 17688-byte log
kept outside the repository). All four failures are
`PermissionError: [WinError 5] Access is denied` raised inside `subprocess` at `CreatePipe`
when a test starts `ffmpeg` with captured output:
`tests/test_batch.py::test_cli_empty_and_invalid_inputs`,
`tests/test_batch.py::test_cli_fresh_and_resume`,
`tests/test_evaluation_manifest.py::test_cli_build_validate_and_synthetic_shortfall` and
`tests/test_evaluation_prepare.py::test_preparation_idempotence_source_preservation_and_collisions`.
This is the operator sandbox refusing pipe creation for a child process, not a repository change:
no tracked file was modified before, during or after the run, and this task adds no test and no
code.

## Deviations from the issue's letter

1. The private operator log `.local-evaluation/pair-rating/session-18-operator-log.md` was **not
   written**. This operator session holds `.local-evaluation/` read-only, and the blocked path
   creates no private artifact. The preflight commands and their results are recorded in the
   reproduction block above instead.
2. The issue names `uv run python -m ...`. `uv` cannot initialize its cache in this operator
   session (`Access is denied` on the uv cache path), so the equivalent
   `.venv/Scripts/python.exe -m ...` invocation was used. The repository, its dependencies and
   `pyproject.toml` are unchanged either way.
3. "…`uv run pytest` still passes with the existing test set" cannot be shown clean from this
   sandbox because of the four environmental failures above. The result is reported as observed,
   not as met. No test was changed, skipped or removed to make it green.

## Public safety

This report and the issue comment carry only version and seed identifiers, thresholds, aggregate
counts and the commands above. They contain no sample id, pair id, session id, evaluator code,
`E`-label mapping, file name, pack name, absolute or library path, no audio or render, and no
private dataset, journal or export content. Audio, records, journals, exports and renders stay on
the device under the gitignored `.local-evaluation/`; nothing private was committed, attached to
the issue or uploaded. Source audio was never opened and stays read-only and unmodified.
