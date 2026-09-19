## Decision
Decision: revise
Decision date: 2026-09-19
Scope: unchanged — evidence insufficient
This record rests on the committed report `_docs/ranking-comparison-19.md`, whose `## State` is `insufficient evidence` and whose gate verdicts are `insufficient` for every quality, lift, latency and evidence-minimum gate it evaluates, `met` only for `MIN_POOL_KICKS` and `MIN_POOL_BASSES`, and `not supported` for none.

## Evidence state

The report's `## State` line, quoted verbatim:

> insufficient evidence — no held-out pair list exists (#65 has not landed the sampler, the split manifest or the evaluator assignment); 0 of 300 rated pairs and 0 of 60 eligible held-out queries; no validated #18 rating session exists; no evaluator assignment exists; no #9 analysis manifest exists; no live Jev outcome exists

The state line begins `insufficient evidence`, which is the R2 condition of the required mapping. It is not `comparison reported`, so R3, R4, R5 and R6 are unreachable from this evidence and only R1 and R2 can be considered; the report is present and readable, so R1 does not apply. The decision value is `revise` and the scope is `unchanged — evidence insufficient`.

| Item | Value |
| --- | --- |
| Report | `_docs/ranking-comparison-19.md` (committed at HEAD `5d84f7c`) |
| Report SHA-256 | `2e3499dee4c22cc042ff5f98965b9913817e0874a01b749e6842b1cdb86057cc` |
| Command used for the digest | `(Get-FileHash -Algorithm SHA256 D:/dev/work/tera/_docs/ranking-comparison-19.md).Hash.ToLower()` (run from the repository root; the relative form `Get-FileHash -Algorithm SHA256 _docs/ranking-comparison-19.md` gives the same digest) |
| `run_key` the report was produced from | `0660820cf549b65ffc2fd9afefd746169bf8f913ba5e3715e996a0677b56c09e` |
| `PROTOCOL_VERSION` | `tera-eval-protocol-v1` |
| Dataset version | `fca713b6e113df4d83684f3c630e42602c0dee34b99f45c95022d6fb169d13f5` |
| Split manifest digest | `not recorded` |
| Pair list digest | `not recorded` |
| `## Deviations` item count | 6 bullet items |

The `## Deviations` section holds exactly 6 bullet items: unscored candidates; identical orderings; unrated queries; the #65 split-manifest and evaluator-assignment schema expectation; the statement that no threshold was lowered, no metric was changed and no denominator was redefined; and the latency cold-sample process note. It also carries the session-rows statement (no session exists, so no session row carries a `validate` exit status) and the standing sentence that no rating was fabricated and no private record left the device.

The exact published zeros and shortfalls are:

- 0 of 5 counted evaluators (`MIN_EVALUATORS = 5`)
- 0 of 300 rated pairs (`MIN_HELDOUT_PAIRS = 300`)
- 0 of 60 eligible held-out queries (`MIN_HELDOUT_QUERIES = 60`)
- 0 of 40 multi-rated pairs — 0 of 40 pairs with at least two valid ratings (`MIN_AGREEMENT_PAIRS = 40`)
- 0 of 30 latency samples per arm (`MIN_LATENCY_REQUESTS_PER_ARM = 30`), over 0 of 10 distinct query kicks per arm
- no live outcomes (0 of ≥1 `source="interface"`) — `no_live_jev_outcomes`
- `pair_coverage` 0/0 undefined (numerator 0, denominator 0)
- 0 of 0 sampled pairs rated; mean absolute deviation not computable (0 of 40 reportable pairs)
- 0 sessions (validated / tooling / excluded: 0 / 0 / 0), so no session's `recognised_rate` can be checked and no session's `validate` exit status exists
- 0 of 2 declared analysis manifests present (`analysis_manifest_missing`)
- `queries_with_11_rated_candidates`: 0 of 0 eligible queries

No gate could be evaluated from this evidence; the record is insufficient evidence, not a pass and not a failure.

No arm comparison, lift, quality or latency conclusion is available. No arm produced an ordering (the report's arm mechanics table records `(no ordering produced)` for the mode counts and the Jev status counts of all four arms), no metric was computed, no 95% interval exists, and the report's own `## Quality` note states that none of the arm mechanics, latency or determinism evidence carries a rating and that the section cannot support any lift or quality claim.

## Decision values and their evidence preconditions

`proceed`, `revise` and `stop` are defined only as the conditions on `_docs/ranking-comparison-19.md` set out in the required mapping below. The table is reproduced in full with the required `Applied?` column added; the first matching row wins, exactly one row is marked `yes`, and every other row states the condition that is absent.

| Row | Condition on `_docs/ranking-comparison-19.md` | Decision | Scope | Applied? |
| --- | --- | --- | --- | --- |
| R1 | No report, or the report is unreadable | `revise` | `unchanged — evidence insufficient` | no — the report exists and is readable (SHA-256 recorded in `## Evidence state`) |
| R2 | `## State` begins `insufficient evidence` | `revise` | `unchanged — evidence insufficient` | **yes** |
| R3 | `## State` is `comparison reported` and `MIN_LIFT_OVER_DSP` on `hybrid` is evaluable and `not supported` | `stop` | `DSP-only kick-to-bass` when `MIN_DSP_PAIRWISE_ACCURACY` and `MIN_LIFT_OVER_RANDOM` on `dsp-only` are `supported`; otherwise `no further Phase 1 work` | no — `## State` is `insufficient evidence`, not `comparison reported`, and that gate is `insufficient`, not evaluable |
| R4 | `## State` is `comparison reported` and any other applicable minimum-class gate is `not supported` | `stop` | `no further Phase 1 work` | no — no applicable minimum-class gate is `not supported`; every applicable gate is `insufficient`, or `met` for the two pool floors |
| R5 | `## State` is `comparison reported` and any applicable gate is `inconclusive` (including two gated metrics that disagree), or any gate is `insufficient` for an unmet evidence minimum (including `no_live_jev_outcomes`, `hybrid_fell_back_to_dsp`, an arm error code or a missing artifact) | `revise` | `unchanged — evidence insufficient` | no — R2 matches first (first matching row wins); the preconditions of R5 name a report whose `## State` is `comparison reported` |
| R6 | `## State` is `comparison reported` and every applicable minimum-class gate is `supported`, no evidence minimum is unmet, at least one `source="interface"` Jev outcome exists and the protocol's hybrid consistency rule holds | `proceed` | `hybrid kick-to-bass` | no — no gate is `supported`, evidence minimums are unmet and no `source="interface"` Jev outcome exists |

The three classes this record uses for a gate that is neither `supported` nor `not supported` are:

- `not applicable at this design` — the gate does not apply at the frozen design, for example the literal top-10 gates at `RATED_CANDIDATES_PER_QUERY = 5`, where `k` equals the rated-subset size. Such a gate is reported under `## Limits of this decision` and is never a pass.
- `insufficient — evidence minimum unmet` — an evidence floor is not met, so the gate is not evaluable at all.
- `not available — <cause>` — the gate's metric could not be computed for a named cause, such as `no_live_jev_outcomes`, a missing artifact or an arm error code.

`proceed` requires that neither `insufficient — evidence minimum unmet` nor `not available — <cause>` is present anywhere in the gate accounting; a `not applicable at this design` gate is never counted towards a pass and is recorded only as a limit.

Rule text (quoted so that it is not read as a finding of this record): `hybrid milestone passed` may not be written while either answer in `## Jev lift and the DSP-only scope question` is `not established`.

## Gate accounting

Every row below is a gate this decision touches. The published value and 95% interval, the `#19` verdict and the shortfall are transcribed from `_docs/ranking-comparison-19.md`; nothing is re-derived or corrected. `not available` in the value column means the report publishes no value for that arm.

### Quality and lift gates

| Gate (arm) | Published value and 95% interval (#19) | Constant and value, with unit | Class | #19 verdict | #20 finding | Source section in #19 |
| --- | --- | --- | --- | --- | --- | --- |
| `MIN_PAIRWISE_ACCURACY` (hybrid) | not reported (no live Jev outcomes); interval not reported | `MIN_PAIRWISE_ACCURACY = 0.60` accuracy points | minimum | `insufficient` | not available — no live Jev outcomes (0 of ≥1 `source="interface"`) | `### Gates` |
| `MIN_DSP_PAIRWISE_ACCURACY` (dsp-only) | not computed; interval not computed | `MIN_DSP_PAIRWISE_ACCURACY = 0.55` accuracy points | minimum | `insufficient` | unmet — 0 of 60 eligible held-out queries | `### Gates` |
| `MIN_JEV_ONLY_PAIRWISE_ACCURACY` (jev-only) | not reported (no live Jev outcomes); interval not reported | `MIN_JEV_ONLY_PAIRWISE_ACCURACY = 0.55` accuracy points | minimum | `insufficient` | not available — no live Jev outcomes (0 of ≥1 `source="interface"`) | `### Gates` |
| `MIN_LIFT_OVER_RANDOM` (random) | not computed; interval not computed | `MIN_LIFT_OVER_RANDOM = +0.10` accuracy points | minimum | `insufficient` | unmet — 0 of 60 paired eligible queries | `### Gates` |
| `MIN_LIFT_OVER_RANDOM` (dsp-only) | not computed; interval not computed | `MIN_LIFT_OVER_RANDOM = +0.10` accuracy points | minimum | `insufficient` | unmet — 0 of 60 paired eligible queries | `### Gates` |
| `MIN_LIFT_OVER_RANDOM` (jev-only) | not reported (no live Jev outcomes); interval not reported | `MIN_LIFT_OVER_RANDOM = +0.10` accuracy points | minimum | `insufficient` | not available — no live Jev outcomes (0 of ≥1 `source="interface"`) | `### Gates` |
| `MIN_LIFT_OVER_RANDOM` (hybrid) | not reported (no live Jev outcomes); interval not reported | `MIN_LIFT_OVER_RANDOM = +0.10` accuracy points | minimum | `insufficient` | not available — no live Jev outcomes (0 of ≥1 `source="interface"`) | `### Gates` |
| `MIN_LIFT_OVER_DSP` (hybrid) | not reported (no live Jev outcomes); interval not reported | `MIN_LIFT_OVER_DSP = +0.10` accuracy points | minimum | `insufficient` | not available — no live Jev outcomes (0 of ≥1 `source="interface"`) | `### Gates` |
| `MIN_TOP1_MARGIN` (hybrid) | not reported (no live Jev outcomes); interval not reported | `MIN_TOP1_MARGIN = +0.30` rating points (0-3 scale) | minimum | `insufficient` | not available — no live Jev outcomes (0 of ≥1 `source="interface"`) | `### Gates` |

The `MIN_LIFT_OVER_RANDOM` row for the `random` arm is reproduced exactly as #19 publishes it; it is listed for completeness and no value of it is claimed.

### Literal top-10 gates (not applicable at this design)

| Gate (arm) | Published value and 95% interval (#19) | Constant and value, with unit | Class | #19 verdict | #20 finding | Source section in #19 |
| --- | --- | --- | --- | --- | --- | --- |
| `MIN_TOP_K_MEAN` (random, dsp-only, jev-only, hybrid) | not computed (k = 5 is descriptive); interval not computed | `MIN_TOP_K_MEAN = 2.00` rating points (0-3 scale) | minimum (gate, literal top-10 only) | `insufficient` | not applicable at this design — `queries_with_11_rated_candidates` 0 of 0 | `### Gates` |
| `MIN_TOP_K_MEAN_LIFT_OVER_RANDOM` (dsp-only, jev-only, hybrid) | not computed (k = 5 is descriptive); interval not computed | `MIN_TOP_K_MEAN_LIFT_OVER_RANDOM = +0.30` rating points (0-3 scale) | minimum (gate, literal top-10 only) | `insufficient` | not applicable at this design — `queries_with_11_rated_candidates` 0 of 0 | `### Gates` |
| `MIN_TOP_K_MEAN_LIFT_OVER_DSP` (jev-only, hybrid) | not computed (k = 5 is descriptive); interval not computed | `MIN_TOP_K_MEAN_LIFT_OVER_DSP = +0.15` rating points (0-3 scale) | minimum (gate, literal top-10 only) | `insufficient` | not applicable at this design — `queries_with_11_rated_candidates` 0 of 0 | `### Gates` |
| `RATED_CANDIDATES_PER_QUERY` (all four arms, design rule) | no rated candidate exists; the design draws 5 rated candidates per query | `RATED_CANDIDATES_PER_QUERY = 5` rated candidates per query | not applicable at this design (fixed rule, not a gate) | not evaluated (fixed rule, not a gate) | not applicable at this design | `### Gates` |
| `MIN_RATED_CANDIDATES_FOR_TOP10_GATE` (all four arms, design rule) | `queries_with_11_rated_candidates`: 0 of 0 eligible queries | `MIN_RATED_CANDIDATES_FOR_TOP10_GATE = 11` rated candidates per query | not applicable at this design (fixed rule, not a gate) | `insufficient` for the literal top-10 gates it enables | not applicable at this design | `### Gates` |

### Evidence minima

| Gate (arm) | Published value and 95% interval (#19) | Constant and value, with unit | Class | #19 verdict | #20 finding | Source section in #19 |
| --- | --- | --- | --- | --- | --- | --- |
| `MIN_EVALUATORS` (all four arms) | 0 of 5 counted evaluators; interval not applicable (evidence minimum) | `MIN_EVALUATORS = 5` counted evaluators | minimum | `insufficient` | unmet — 0 of 5 counted evaluators | `### Gates` |
| `MIN_RATINGS_PER_PAIR` (all four arms) | 0 of 0 sampled pairs hold 2 valid ratings; interval not applicable (evidence minimum) | `MIN_RATINGS_PER_PAIR = 2` valid ratings | minimum | `insufficient` | unmet — 0 of 0 sampled pairs rated | `### Gates` |
| `MIN_HELDOUT_PAIRS` (all four arms) | 0 of 300 sampled pairs; interval not applicable (evidence minimum) | `MIN_HELDOUT_PAIRS = 300` held-out pairs | minimum | `insufficient` | unmet — 0 of 300 rated pairs | `### Gates` |
| `MIN_HELDOUT_QUERIES` (all four arms) | 0 of 60 eligible held-out queries; interval not applicable (evidence minimum) | `MIN_HELDOUT_QUERIES = 60` held-out query kicks | minimum | `insufficient` | unmet — 0 of 60 eligible held-out queries | `### Gates` |
| `MIN_PAIR_COVERAGE` (all four arms) | not computable (0 of 0 sampled pairs); interval not applicable (evidence minimum) | `MIN_PAIR_COVERAGE = 0.80` rated share of sampled pairs | minimum | `insufficient` | unmet — 0 of 0 sampled pairs rated (`pair_coverage` 0/0 undefined) | `### Gates` |
| `MIN_AGREEMENT_PAIRS` (all four arms) | 0 of 40 pairs with at least two valid ratings; interval not applicable (evidence minimum) | `MIN_AGREEMENT_PAIRS = 40` multi-rated pairs | minimum | `insufficient` | unmet — 0 of 40 multi-rated pairs | `### Gates` |
| `MAX_MEAN_ABSOLUTE_DEVIATION` (all four arms) | not computable (0 of 40 reportable pairs); interval not applicable (evidence minimum) | `MAX_MEAN_ABSOLUTE_DEVIATION = 1.00` rating points (0-3 scale) | minimum | `insufficient` | unmet — 0 of 40 pairs with at least two valid ratings | `### Gates` |
| `MAX_RECOGNISED_RATE` (all four arms) | 0 sessions to check; interval not applicable (evidence minimum) | `MAX_RECOGNISED_RATE = 0.20` flagged share of a session | minimum | `insufficient` | not available — no session exists to check (0 of 0 sessions, 0 counted evaluators) | `### Gates` |
| `MIN_SCORED_SHARE_PER_QUERY` (random, dsp-only) | no eligible query; interval not applicable (evidence minimum) | `MIN_SCORED_SHARE_PER_QUERY = 0.50` scored share of the rated subset | minimum | `insufficient` | unmet — 0 of 0 eligible queries | `### Gates` |
| Live-Jev requirement (jev-only, hybrid — every Jev-dependent gate) | 0 of ≥1 `source="interface"` outcome; the live integration check is recorded UNVERIFIED | `source = "interface"` outcomes ≥ 1 | minimum | `insufficient` — reason `no_live_jev_outcomes`, never `not supported` | unmet — 0 of ≥1 `source="interface"` live outcome (`no_live_jev_outcomes`) | `## Quality` (Jev evidence), `### Gates` |
| `MIN_TUNING_PAIRS` (no arm — no tuning claim is made) | not evaluated here; interval not applicable | `MIN_TUNING_PAIRS = 40` tuning pairs | minimum | not evaluated (no tuning claim is made) | not applicable at this design — no tuning claim is made | `### Gates` |

### Pool floors (met)

| Gate (arm) | Published value and 95% interval (#19) | Constant and value, with unit | Class | #19 verdict | #20 finding | Source section in #19 |
| --- | --- | --- | --- | --- | --- | --- |
| `MIN_POOL_KICKS` (all four arms) | 100 of 80 pool kicks; interval not applicable (evidence minimum) | `MIN_POOL_KICKS = 80` pool kicks | minimum | `met` | met | `### Gates` |
| `MIN_POOL_BASSES` (all four arms) | 46 of 8 pool basses; interval not applicable (evidence minimum) | `MIN_POOL_BASSES = 8` pool basses/sub-basses | minimum | `met` | met | `### Gates` |

### Latency gates

| Gate (arm) | Published value and 95% interval (#19) | Constant and value, with unit | Class | #19 verdict | #20 finding | Source section in #19 |
| --- | --- | --- | --- | --- | --- | --- |
| `MIN_LATENCY_REQUESTS_PER_ARM` (random, dsp-only) | 0 of 30 valid samples on the least-sampled published arm; interval not applicable (evidence minimum) | `MIN_LATENCY_REQUESTS_PER_ARM = 30` latency measurements per arm | minimum | `insufficient` | unmet — 0 of 30 latency samples per arm | `### Gates` |
| `MAX_COLD_RECOMMENDATION_P95_MS` (random, dsp-only) | not measured (least-sampled arm 0 of 30 valid samples); interval not applicable (no complete sample set) | `MAX_COLD_RECOMMENDATION_P95_MS = 2000` milliseconds, p95 | minimum | `insufficient` | unmet — 0 of 30 latency samples per arm | `### Gates` |
| `MAX_WARM_RECOMMENDATION_P95_MS` (random, dsp-only) | not measured (least-sampled arm 0 of 30 valid samples); interval not applicable (no complete sample set) | `MAX_WARM_RECOMMENDATION_P95_MS = 500` milliseconds, p95 | minimum | `insufficient` | unmet — 0 of 30 latency samples per arm | `### Gates` |

### Target-class constants (non-gating — they can never fail this decision)

| Gate (arm) | Published value and 95% interval (#19) | Constant and value, with unit | Class | #19 verdict | #20 finding | Source section in #19 |
| --- | --- | --- | --- | --- | --- | --- |
| `TARGET_PAIRWISE_ACCURACY` (all four arms) | descriptive; interval not applicable (target class) | `TARGET_PAIRWISE_ACCURACY = 0.68` accuracy points | target (non-gating) | not evaluated (target class) | not applicable at this design — a target cannot fail this decision | `### Gates` |
| `TARGET_LIFT_OVER_RANDOM` (all four arms) | descriptive; interval not applicable (target class) | `TARGET_LIFT_OVER_RANDOM = +0.18` accuracy points | target (non-gating) | not evaluated (target class) | not applicable at this design — a target cannot fail this decision | `### Gates` |
| `TARGET_LIFT_OVER_DSP` (all four arms) | descriptive; interval not applicable (target class) | `TARGET_LIFT_OVER_DSP = +0.15` accuracy points | target (non-gating) | not evaluated (target class) | not applicable at this design — a target cannot fail this decision | `### Gates` |
| `TARGET_TOP1_MARGIN` (all four arms) | descriptive; interval not applicable (target class) | `TARGET_TOP1_MARGIN = +0.50` rating points (0-3 scale) | target (non-gating) | not evaluated (target class) | not applicable at this design — a target cannot fail this decision | `### Gates` |
| `TARGET_TOP_K_MEAN` (all four arms) | descriptive; interval not applicable (target class) | `TARGET_TOP_K_MEAN = 2.30` rating points (0-3 scale) | target (non-gating) | not evaluated (target class) | not applicable at this design — a target cannot fail this decision | `### Gates` |
| `TARGET_EVALUATORS` (all four arms) | descriptive; interval not applicable (target class) | `TARGET_EVALUATORS = 8` counted evaluators | target (non-gating) | not evaluated (target class) | not applicable at this design — a target cannot fail this decision | `### Gates` |
| `TARGET_RATINGS_PER_PAIR` (all four arms) | descriptive; interval not applicable (target class) | `TARGET_RATINGS_PER_PAIR = 3` valid ratings | target (non-gating) | not evaluated (target class) | not applicable at this design — a target cannot fail this decision | `### Gates` |
| `TARGET_HELDOUT_QUERIES` (all four arms) | descriptive; interval not applicable (target class) | `TARGET_HELDOUT_QUERIES = 75` held-out query kicks | target (non-gating) | not evaluated (target class) | not applicable at this design — a target cannot fail this decision | `### Gates` |
| `TARGET_COLD_RECOMMENDATION_P95_MS` (all four arms) | descriptive; interval not applicable (target class) | `TARGET_COLD_RECOMMENDATION_P95_MS = 1000` milliseconds, p95 | target (non-gating) | not evaluated (target class) | not applicable at this design — a target cannot fail this decision | `### Gates` |
| `TARGET_WARM_RECOMMENDATION_P95_MS` (all four arms) | descriptive; interval not applicable (target class) | `TARGET_WARM_RECOMMENDATION_P95_MS = 250` milliseconds, p95 | target (non-gating) | not evaluated (target class) | not applicable at this design — a target cannot fail this decision | `### Gates` |

### Not applicable to Phase 0 (#39 / #36)

| Gate (arm) | Published value and 95% interval (#19) | Constant and value, with unit | Class | #19 verdict | #20 finding | Source section in #19 |
| --- | --- | --- | --- | --- | --- | --- |
| `MAX_AUDITION_START_P95_MS` (none — not a comparison gate) | not evaluated here; interval not applicable | `MAX_AUDITION_START_P95_MS = 250` milliseconds, p95 | minimum (Phase 1, #39) | not evaluated (out of scope for #19) | not applicable to Phase 0 (#39 / #36) | `### Gates` |
| `RETENTION_WINDOW_DAYS` (none) | not evaluated here; interval not applicable | `RETENTION_WINDOW_DAYS = 14` days | fixed (not a gate) | not evaluated (out of scope for #19) | not applicable to Phase 0 (#39 / #36) | `### Gates` |
| `MIN_RETENTION_RATE` (none) | not evaluated here; interval not applicable | `MIN_RETENTION_RATE = 0.20` retained share of completed selections | minimum (Phase 1) | not evaluated (out of scope for #19) | not applicable to Phase 0 (#39 / #36) | `### Gates` |
| `TARGET_RETENTION_RATE` (none) | not evaluated here; interval not applicable | `TARGET_RETENTION_RATE = 0.30` retained share of completed selections | target (non-gating, Phase 1) | not evaluated (out of scope for #19) | not applicable to Phase 0 (#39 / #36) | `### Gates` |
| `MIN_RETENTION_SELECTIONS` (none) | not evaluated here; interval not applicable | `MIN_RETENTION_SELECTIONS = 30` completed selections | minimum (Phase 1) | not evaluated (out of scope for #19) | not applicable to Phase 0 (#39 / #36) | `### Gates` |

`MAX_AUDITION_START_P95_MS` and the Phase 1 retention constants do not enter this decision: this record is a Phase 0 feasibility decision, and audition start and retention belong to #39 and #36.

### Reading of the table

No row is `supported` and no row is `not supported`, so the global rule of `_docs/evaluation-protocol.md` (pass only when every applicable gate is `supported`; fail when any applicable gate is `not supported`; otherwise insufficient evidence) returns insufficient evidence, not a pass and not a failure. Every affected gate carries its published shortfall with numerator and denominator above. R2 of the required mapping is the first matching row, so the recorded value is `revise` with scope `unchanged — evidence insufficient`.

## Jev lift and the DSP-only scope question

The two questions are answered in order, and each answer cites the gate row it rests on.

1. **Does Jev materially improve over DSP-only?** `not established`. The deciding row is `MIN_LIFT_OVER_DSP = +0.10` on `hybrid` in the `### Gates` section of `_docs/ranking-comparison-19.md`: published value `not reported (no live Jev outcomes)`, interval `not reported`, verdict `insufficient`, shortfall `no_live_jev_outcomes`. The answer is `yes` only when that gate is `supported` and at least one `source="interface"` Jev outcome exists, and `no` only when that gate is evaluable and `not supported`. Neither precondition holds — the gate is not evaluable and 0 of ≥1 `source="interface"` outcomes exist — so the evidence-bound answer is `not established`.
2. **Does the hybrid top-10 beat random?** `0 of 0 eligible queries reached MIN_RATED_CANDIDATES_FOR_TOP10_GATE = 11 — not evaluable at RATED_CANDIDATES_PER_QUERY = 5`. The report publishes `queries_with_11_rated_candidates`: 0 of 0 eligible queries, so the answer is neither `yes` nor `no`. The arm-level `MIN_LIFT_OVER_RANDOM = +0.10` row on `hybrid` (published value `not reported (no live Jev outcomes)`, interval `not reported`, verdict `insufficient`, shortfall `no_live_jev_outcomes`) is adjacent evidence at the arm level only; it is never the top-10 answer.

Scope rule applied here: the decision-values section quotes the rule that the hybrid milestone may not be recorded as passed while either answer is `not established`. Both answers are `not established`.

Rule text (quoted; its precondition does not hold on this evidence, and quoting it is not a finding of this record): "When MIN_LIFT_OVER_DSP on hybrid is not supported, this record decides the DSP-only scope question explicitly and does not call the hybrid milestone passed." The precondition is the `not supported` verdict; the published verdict on that gate is `insufficient`, not `not supported`, so this record does not decide the DSP-only scope question and authorizes no DSP-only scope.

Because answer (1) is `not established`, both the hybrid scope question and the DSP-only scope question stay open until the experiment in `## Next experiment` lands. No DSP-only scope is authorized here, so no plan-alignment edit and no follow-up plan issue are required by this record.

## Contradiction rule

`_docs/evaluation-protocol.md` `## Uncertainty and decision rules` items 11 and 12 make a contradictory gated metric a non-negative result: two gated metrics that disagree on the same comparison (one `supported`, one `not supported`) are inconclusive, and a leave-one-evaluator-out or same-pack with/without recomputation that flips a conclusion is inconclusive.

No contradiction exists in this evidence, so there are no two gate rows to name here. The report contains no `supported` gate and no `not supported` gate, so no two gated metrics can disagree; and its `## Agreement` section records that no evaluator holds a rating, so no leave-one-evaluator-out variant exists, and the report records no same-pack with/without recomputation. Items 11 and 12 therefore cannot have fired.

Should a later report on this evidence set show either condition, this record's rule is fixed in advance: name both gate rows with both published verdicts; record the affected gate as `inconclusive` per items 11 and 12; record the decision value as `revise` (row R5 of the required mapping); and name the re-run or the additional evidence that resolves the contradiction. The better verdict is never adopted, the worse one is never adopted to force a harsher outcome, and the two are never averaged. The evidence that resolves a contradiction in this evidence set is the experiment in `## Next experiment`.

## Limits of this decision

1. **Gates that are `not applicable at this design`, and therefore never a pass.** `MIN_TOP_K_MEAN = 2.00`, `MIN_TOP_K_MEAN_LIFT_OVER_RANDOM = +0.30` and `MIN_TOP_K_MEAN_LIFT_OVER_DSP = +0.15` are the literal top-10 gates. They are not evaluable at `RATED_CANDIDATES_PER_QUERY = 5`, because `k` equals the rated-subset size there; the report publishes `queries_with_11_rated_candidates`: 0 of 0 eligible queries, and states that with k = 5 the top-k means are descriptive values and no k = 5 mean is compared with a top-10 constant. `MIN_RATED_CANDIDATES_FOR_TOP10_GATE = 11` and `RATED_CANDIDATES_PER_QUERY = 5` are the fixed design rules behind them, and `MIN_TUNING_PAIRS = 40` is not applicable because no tuning claim is made. Making the literal top-10 gates evaluable is #69's budget: at least 11 rated candidates on each of at least `MIN_HELDOUT_QUERIES = 60` eligible queries — at least 660 rated pairs and about 1320 ratings at `MIN_RATINGS_PER_PAIR = 2`. The pool's 54-bass shortfall against the plan target (#67) is one of the constraints on that budget. None of these gates is reported as a pass.
2. **Gate verdicts that would reverse this decision if they changed.** The decision would change if, on a `comparison reported` state, any of `MIN_EVALUATORS`, `MIN_RATINGS_PER_PAIR`, `MIN_HELDOUT_PAIRS`, `MIN_HELDOUT_QUERIES`, `MIN_PAIR_COVERAGE`, `MIN_AGREEMENT_PAIRS`, `MAX_MEAN_ABSOLUTE_DEVIATION`, `MAX_RECOGNISED_RATE`, `MIN_LATENCY_REQUESTS_PER_ARM`, `MAX_COLD_RECOMMENDATION_P95_MS`, `MAX_WARM_RECOMMENDATION_P95_MS` or the live-Jev requirement stopped being `insufficient`; if `MIN_LIFT_OVER_DSP` on `hybrid` became evaluable and `not supported` (R3) or if it and its companion gates became `supported` (R6); if any other applicable minimum-class gate became `not supported` (R4); or if any applicable gate became `inconclusive` (R5). The gates that are `not applicable at this design` (`MIN_TOP_K_MEAN`, `MIN_TOP_K_MEAN_LIFT_OVER_RANDOM`, `MIN_TOP_K_MEAN_LIFT_OVER_DSP`) would also have to stop being not applicable for a fully settled decision, which is why #69's budget is named above.
3. **This record is bound to one run key.** A change to the rating budget, the pair list, the split, any seed, `PROTOCOL_VERSION`, the ranking version, the adapter version, the prompt version or an observed model version produces a new `run_key` and requires a new decision record under that new run key; this record is never edited to carry a later result.
4. **Latency stages that are not measured.** The report's latency stage table marks the retrieval stage `not_implemented` (retrieval before #25) and every other stage `not measured` with 0 samples. `not_implemented` is a limit of the latency rows: it is not 0 ms and no latency value is claimed for it, and no per-stage p50 or p95 exists in this record.
5. **No arm-level comparison, lift, quality or latency conclusion, and no top-10 result, is available from this evidence.**

## What this record may not change

`_docs/evaluation-protocol.md` at `tera-eval-protocol-v1` with its frozen constants and thresholds, `_docs/ranking-comparison-19.md` and its run artifacts under `.local-evaluation/ranking-comparison/`, the #2 contracts (`backend/contracts.py`, schema 1.0) and every landed code module are inputs to this record.

No threshold may be lowered, no metric redefined, no denominator changed, no seed, split, pair list or assignment re-derived, no report value corrected, and no defect in #16/#17/#18/#19/#65 fixed here — such a defect is reported as a blocker in `## Blockers` with a linked follow-up issue.

## Next experiment

One primary experiment only.

| Field | Value |
| --- | --- |
| Short name | Producer rating session and live Jev capture on the frozen held-out design |
| Owning issue | [#18](https://github.com/gmphto/tera/issues/18) re-run — the producer labeling session, on [#65](https://github.com/gmphto/tera/issues/65)'s pair list with [#66](https://github.com/gmphto/tera/issues/66)'s evaluators |
| Owner role | Producer panel (the session itself); orchestrator for briefing and for the issue re-open or re-file; user for credentials |
| Recorded inputs it needs | `PROTOCOL_VERSION = tera-eval-protocol-v1`; dataset version `fca713b6e113df4d83684f3c630e42602c0dee34b99f45c95022d6fb169d13f5`; the split-manifest digest and pair-list digest from #65, both `not recorded` today; a rating session with ≥ `MIN_EVALUATORS = 5` counted evaluators and ≥ `MIN_RATINGS_PER_PAIR = 2` valid ratings per pair, over ≥ `MIN_HELDOUT_QUERIES = 60` eligible queries and ≥ `MIN_HELDOUT_PAIRS = 300` pairs; live-Jev prerequisites from #68 / #14 — `TERA_JEV_ENDPOINT` and `TERA_JEV_API_KEY` set, so that ≥ 1 `source="interface"` outcome exists |
| Evidence it must produce | The first validated producer ratings and the first live Jev outcomes, making evaluable: `MIN_EVALUATORS`, `MIN_RATINGS_PER_PAIR`, `MIN_HELDOUT_PAIRS`, `MIN_HELDOUT_QUERIES`, `MIN_PAIR_COVERAGE`, `MIN_AGREEMENT_PAIRS`, `MAX_MEAN_ABSOLUTE_DEVIATION`, `MAX_RECOGNISED_RATE`, `MIN_LATENCY_REQUESTS_PER_ARM`, `MAX_COLD_RECOMMENDATION_P95_MS`, `MAX_WARM_RECOMMENDATION_P95_MS`, and then `MIN_PAIRWISE_ACCURACY`, `MIN_DSP_PAIRWISE_ACCURACY`, `MIN_JEV_ONLY_PAIRWISE_ACCURACY`, `MIN_LIFT_OVER_RANDOM`, `MIN_LIFT_OVER_DSP` and `MIN_TOP1_MARGIN`; #19 is re-run after it, under the new run key |
| Checkable exit condition | #19 re-runs under the new `run_key` with ≥ `MIN_EVALUATORS = 5` counted evaluators, ≥ `MIN_HELDOUT_PAIRS = 300` rated pairs and ≥ `MIN_HELDOUT_QUERIES = 60` eligible queries, and ≥ 1 `source="interface"` Jev outcome; the re-run report exists under the new run key and its `## State` is `comparison reported` |

Every other unmet prerequisite sits in `## Blockers`. On this R2 path the primary experiment is the one that supplies the first validated producer ratings and the first live Jev outcomes; `collect more data` is not a named experiment.

## Blockers

| Blocker | Owner role | Required evidence / exit condition | Issue |
| --- | --- | --- | --- |
| No held-out pair list: the sampler, the split manifest and the evaluator assignment have not landed, so the split-manifest digest and the pair-list digest are `not recorded`; 0 of 300 rated pairs and 0 of 60 eligible held-out queries | Engineer | A split manifest (schema_version 1.0) and a pair list whose digest equals the manifest's, plus an evaluator assignment giving every pair ≥ 2 distinct evaluators | [#65](https://github.com/gmphto/tera/issues/65) |
| No producer evaluator panel: 0 of `MIN_EVALUATORS = 5` counted evaluators, 0 sessions | User (recruitment) and orchestrator (briefing) | ≥ 5 counted evaluators briefed on the frozen protocol, so the session in `## Next experiment` can run | [#66](https://github.com/gmphto/tera/issues/66) |
| No validated rating session: 0 valid ratings consumed; #18 is closed with no session artifact | Producer panel; orchestrator must re-open or re-file #18 before this task closes | One session whose `validate` exit status is zero, 0 excluded sessions, and ≥ `MIN_RATINGS_PER_PAIR = 2` valid ratings per pair | [#18](https://github.com/gmphto/tera/issues/18) |
| No live Jev outcome: 0 of ≥1 `source="interface"` — `no_live_jev_outcomes`; `TERA_JEV_ENDPOINT` / `TERA_JEV_API_KEY` are not set and the #14 live check is recorded UNVERIFIED | User (credentials) | Credentials present and one credentialed live run, so a `source="interface"` outcome and a real observed model version exist | [#68](https://github.com/gmphto/tera/issues/68); origin of the UNVERIFIED report: [#14](https://github.com/gmphto/tera/issues/14) |
| No analysis manifest: 2 of 2 declared analysis manifests are absent — `analysis_manifest_missing`; #9 is closed with no follow-up issue | Engineer; orchestrator must file or re-open the follow-up before this task closes | One complete analysis manifest per held-out sample used, each with a single `analysis_version` | [#9](https://github.com/gmphto/tera/issues/9) |
| Pool shortfall against the plan target: `MIN_POOL_KICKS` and `MIN_POOL_BASSES` are met (100 of 80 and 46 of 8), but the pool is 54 basses short of the plan target, which constrains the target design and the literal top-10 budget | User (library access) and engineer | A pool that can carry the target design and the 11-rated-candidate budget without re-deriving the split, seed or assignment | [#67](https://github.com/gmphto/tera/issues/67) |
| No rating budget for the literal top-10 gates: 0 of 0 eligible queries hold 11 rated candidates, so `MIN_TOP_K_MEAN`, `MIN_TOP_K_MEAN_LIFT_OVER_RANDOM` and `MIN_TOP_K_MEAN_LIFT_OVER_DSP` are `not applicable at this design` | Orchestrator | ≥ `MIN_RATED_CANDIDATES_FOR_TOP10_GATE = 11` rated candidates on each of ≥ `MIN_HELDOUT_QUERIES = 60` eligible queries — at least 660 rated pairs and about 1320 ratings | [#69](https://github.com/gmphto/tera/issues/69) |
| No latency samples: 0 of `MIN_LATENCY_REQUESTS_PER_ARM = 30` per arm, and the retrieval stage is `not_implemented` | Engineer | ≥ 30 valid latency samples per published arm over ≥ 10 distinct query kicks, measured by the #19 runner once the session lands; retrieval is tracked by #25 and is a limit of the latency rows, never 0 ms | [#25](https://github.com/gmphto/tera/issues/25) |

Two blocker issues (#18 and #9) are closed, so the orchestrator must re-open them or file follow-ups before this task closes; the acceptance criteria require an open issue for each deferred prerequisite, and this record does not create one.

## Phase 1 authorization

Phase 1 remains gated — insufficient evidence

No storage (#21), scanner (#22), API (#27/#28), latency tuning (#37) or desktop UI (#30) work may start from this record.

## Out of scope

- The comparison itself, its metrics, verdicts, run key, run artifacts and report: [#19](https://github.com/gmphto/tera/issues/19) owns them and this record consumes one committed report read-only.
- The protocol's frozen constants, thresholds, seeds, metrics, sampling rules, split rules and record fields: [#16](https://github.com/gmphto/tera/issues/16).
- The pair sampler, split manifest and evaluator assignment: [#65](https://github.com/gmphto/tera/issues/65).
- The pair-rating utility, playback, resume and export: [#17](https://github.com/gmphto/tera/issues/17); the producer session: [#18](https://github.com/gmphto/tera/issues/18); recruitment beyond the current panel: [#66](https://github.com/gmphto/tera/issues/66).
- Growing or repairing the evaluation pool and its 54-bass shortfall against the plan target: [#67](https://github.com/gmphto/tera/issues/67).
- Live Jev credentials, the assumed TypeSafe HTTP mapping and the real model versions: [#68](https://github.com/gmphto/tera/issues/68), with [#14](https://github.com/gmphto/tera/issues/14) reporting the live check UNVERIFIED.
- The held-out rating budget that makes the literal top-10 gates evaluable: [#69](https://github.com/gmphto/tera/issues/69).
- Hybrid ranking implementation and its versioned weights: [#15](https://github.com/gmphto/tera/issues/15).
- Phase 1 product implementation of any kind: storage [#21](https://github.com/gmphto/tera/issues/21), scanner [#22](https://github.com/gmphto/tera/issues/22), APIs [#27](https://github.com/gmphto/tera/issues/27)/[#28](https://github.com/gmphto/tera/issues/28), desktop UI [#30](https://github.com/gmphto/tera/issues/30) and latency tuning [#37](https://github.com/gmphto/tera/issues/37).
- Retrieval before #25 is `not_implemented` in the latency rows; normalised feature retrieval is owned by [#25](https://github.com/gmphto/tera/issues/25).
- Retention reporting and its observation window: [#36](https://github.com/gmphto/tera/issues/36); first-milestone audition and retention evidence: [#39](https://github.com/gmphto/tera/issues/39).
- Preference data, personalisation evaluation and reranking: [#58](https://github.com/gmphto/tera/issues/58), [#59](https://github.com/gmphto/tera/issues/59).
- Editing `_docs/plan.md` or any other product scope document: this record decides no DSP-only scope, so it makes no plan-alignment edit and requires no plan-alignment follow-up.
- Any code, test, fixture, dataset, manifest, config or dependency change.
