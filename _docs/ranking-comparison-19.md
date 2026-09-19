# Ranking comparison report

Comparison of the four frozen arms (random, dsp-only, jev-only, hybrid) over the
held-out split of the frozen protocol. Every value below traces to a recorded
input, seed and version; nothing here is synthesized.

## State
insufficient evidence — no held-out pair list exists (#65 has not landed the sampler, the split manifest or the evaluator assignment); 0 of 300 rated pairs and 0 of 60 eligible held-out queries; no validated #18 rating session exists; no evaluator assignment exists; no #9 analysis manifest exists; no live Jev outcome exists

## Versions and digests

| Item | Value |
| --- | --- |
| Protocol version | tera-eval-protocol-v1 |
| Protocol document | present |
| Protocol document digest | sha256:b11d75fd8113c4a72b35277f5622a9dadbb1f22e8e438c7ebdf4e2e779dda457 |
| Dataset version | fca713b6e113df4d83684f3c630e42602c0dee34b99f45c95022d6fb169d13f5 |
| Dataset identity digest | sha256:6d9fac5fa4cd1ec19d284709c88df09372b855444fa79f5b1be94c931a9ed0aa |
| Split manifest schema version | not recorded |
| Split manifest digest | not recorded |
| Pair list digest | not recorded |
| Analysis version(s) at the recorded run | (none used) |
| RANKING_VERSION | dsp-baseline-v1 |
| DSP weight-table id | dsp-baseline-weights-1 |
| HYBRID_RANKING_VERSION | hybrid-ranking-v1 |
| Hybrid weight-table id | hybrid-weights-1 |
| PROMPT_VERSION | jev-questions-v1 |
| ADAPTER_VERSION | jev-adapter-v1 |
| Observed model version(s) | (none observed) |
| Seeds | ASSIGNMENT_SEED = tera-eval-assignment-16-v1; BOOTSTRAP_SEED = tera-eval-bootstrap-16-v1; ORDER_SEED = tera-eval-order-16-v1; PAIR_SAMPLER_SEED = tera-eval-pairs-16-v1; RANDOM_ARM_SEED = tera-eval-random-16-v1; SPLIT_SEED = tera-eval-split-16-v1 |
| Run key | 0660820cf549b65ffc2fd9afefd746169bf8f913ba5e3715e996a0677b56c09e |

## Eligibility accounting

| Count | Value | Threshold it is compared with |
| --- | --- | --- |
| pool kicks | 100 | MIN_POOL_KICKS = 80 |
| pool basses / sub-basses | 46 | MIN_POOL_BASSES = 8 |
| pool selected records | 146 | - |
| pool reserves | 23 | - |
| tuning kicks / basses | 0 / 0 | TUNING_FRACTION = 0.25 |
| held-out kicks / basses | 0 / 0 | - |
| sampled pairs | 0 of 300 | MIN_HELDOUT_PAIRS = 300 |
| sampled query kicks | 0 of 60 | MIN_HELDOUT_QUERIES = 60 |
| eligible held-out queries | 0 of 0 | MIN_HELDOUT_QUERIES = 60 |
| rated pairs | 0 of 0 |
| pair_coverage | numerator 0, denominator 0 (undefined) |
| counted evaluators | 0 of 5 | MIN_EVALUATORS = 5 |
| sessions (validated / tooling / excluded) | 0 / 0 / 0 | - |
| valid ratings consumed | 0 | - |
| analysis manifests present | 0 of 2 | - |
| analysed samples / used samples | 0 / 0 | - |
| unresolved used samples | 0 | - |
| dataset records used / synthetic | 0 / 0 | provenance_kind = real_library_sample |

| Arm | Eligible queries | Unscored share | Evidence source | Status |
| --- | --- | --- | --- | --- |
| random | 0 of 0 | undefined (0 rated candidates) | none | insufficient (no_held_out_query) |
| dsp-only | 0 of 0 | undefined (0 rated candidates) | none | insufficient (no_held_out_query) |
| jev-only | 0 of 0 | undefined (0 rated candidates) | none | insufficient (no_held_out_query) |
| hybrid | 0 of 0 | undefined (0 rated candidates) | none | insufficient (no_held_out_query) |

Intersection of the four arms' eligible queries: 0 of 0 sampled queries. Every arm received the same eligible candidate set per query (the sampled candidates that pass filter_candidates with FilterPolicy()); an arm ordering a different set is rejected with candidate_set_mismatch instead of being compared.

## Quality

Arm mechanics, latency and determinism are reported here and below; none of that
evidence carries a rating, so this section cannot support any lift or quality
claim.

### Arm mechanics

| Arm | Version | Weight table / rule | Verdict | Mode counts | Jev status counts | Identical orderings | Arm error codes |
| --- | --- | --- | --- | --- | --- | --- | --- |
| random | dsp-baseline-v1 (declared; no ordering was produced) | random-draw (declared) | not supported | (no ordering produced) | (no ordering produced) | (no ordering produced) | none |
| dsp-only | dsp-baseline-v1 (declared; no ordering was produced) | dsp-baseline-weights-1 (declared) | insufficient | (no ordering produced) | (no ordering produced) | (no ordering produced) | none |
| jev-only | jev-only-v1 (declared; no ordering was produced) | equal-1/6-weights (declared) | insufficient | (no ordering produced) | (no ordering produced) | (no ordering produced) | none |
| hybrid | hybrid-ranking-v1 (declared; no ordering was produced) | hybrid-weights-1 (declared) | insufficient | (no ordering produced) | (no ordering produced) | (no ordering produced) | none |

The random arm's analytic expectations are pairwise accuracy 0.50, top-1 margin 0.00 and a top-k mean equal to the rated subset's mean; every random value above is reported beside them.

Jev-only variants: not reported (no live Jev outcomes); when they disagree the Jev-only gate is inconclusive, never the better of the two.

Jev evidence: real TypeSafe Jev integration: UNVERIFIED - TERA_JEV_ENDPOINT/TERA_JEV_API_KEY are not set. The #14 live integration check (tests/test_jev_integration.py) is skipped and is recorded UNVERIFIED, never as a pass. A Jev-dependent gate with no live outcome is insufficient with reason no_live_jev_outcomes, never not supported.

### Gates

| Constant (= value) | Class | Arms gated | Observed value | 95% interval | Verdict | Shortfall |
| --- | --- | --- | --- | --- | --- | --- |
| MIN_POOL_KICKS = 80 | minimum (gate) | random, dsp-only, jev-only, hybrid | 100 of 80 pool kicks | not applicable (evidence minimum) | met | - |
| MIN_POOL_BASSES = 8 | minimum (gate) | random, dsp-only, jev-only, hybrid | 46 of 8 pool basses | not applicable (evidence minimum) | met | - |
| MIN_EVALUATORS = 5 | minimum (gate) | random, dsp-only, jev-only, hybrid | 0 of 5 counted evaluators | not applicable (evidence minimum) | insufficient | 0 of 5 counted evaluators |
| MIN_RATINGS_PER_PAIR = 2 | minimum (gate) | random, dsp-only, jev-only, hybrid | 0 of 0 sampled pairs hold 2 valid ratings | not applicable (evidence minimum) | insufficient | 0 of 0 sampled pairs rated |
| MIN_HELDOUT_PAIRS = 300 | minimum (gate) | random, dsp-only, jev-only, hybrid | 0 of 300 sampled pairs | not applicable (evidence minimum) | insufficient | 0 of 300 rated pairs |
| MIN_HELDOUT_QUERIES = 60 | minimum (gate) | random, dsp-only, jev-only, hybrid | 0 of 60 eligible held-out queries | not applicable (evidence minimum) | insufficient | 0 of 60 eligible held-out queries |
| MIN_PAIR_COVERAGE = 0.8 | minimum (gate) | random, dsp-only, jev-only, hybrid | not computable (0 of 0 sampled pairs) | not applicable (evidence minimum) | insufficient | 0 of 0 sampled pairs rated |
| MIN_AGREEMENT_PAIRS = 40 | minimum (gate) | random, dsp-only, jev-only, hybrid | 0 of 40 pairs with at least two valid ratings | not applicable (evidence minimum) | insufficient | 0 of 40 pairs with at least two valid ratings |
| MAX_MEAN_ABSOLUTE_DEVIATION = 1.0 | minimum (gate) | random, dsp-only, jev-only, hybrid | not computable (0 of 40 reportable pairs) | not applicable (evidence minimum) | insufficient | 0 of 40 pairs with at least two valid ratings |
| MIN_SCORED_SHARE_PER_QUERY = 0.5 | minimum (gate) | random, dsp-only, jev-only, hybrid | no eligible query | not applicable (evidence minimum) | insufficient | 0 of 0 eligible queries |
| MAX_RECOGNISED_RATE = 0.2 | minimum (gate) | random, dsp-only, jev-only, hybrid | 0 sessions to check | not applicable (evidence minimum) | insufficient | - |
| MIN_LATENCY_REQUESTS_PER_ARM = 30 | minimum (gate) | random, dsp-only, jev-only, hybrid | 0 of 30 valid samples on the least-sampled arm | not applicable (evidence minimum) | insufficient | 0 of 30 valid latency samples per arm |
| MIN_DSP_PAIRWISE_ACCURACY = 0.55 | minimum (gate) | dsp-only | not computed | not computed | insufficient | 0 of 60 eligible held-out queries |
| MIN_JEV_ONLY_PAIRWISE_ACCURACY = 0.55 | minimum (gate) | jev-only | not reported (no live Jev outcomes) | not reported | insufficient | no_live_jev_outcomes |
| MIN_PAIRWISE_ACCURACY = 0.6 | minimum (gate) | hybrid | not reported (no live Jev outcomes) | not reported | insufficient | no_live_jev_outcomes |
| MIN_LIFT_OVER_RANDOM = 0.1 | minimum (gate) | random | +0.0000 | [0.0000, 0.0000] | not supported | structurally zero: the identical arm |
| MIN_LIFT_OVER_RANDOM = 0.1 | minimum (gate) | dsp-only | not computed | not computed | insufficient | 0 of 60 paired eligible queries |
| MIN_LIFT_OVER_RANDOM = 0.1 | minimum (gate) | jev-only | not reported (no live Jev outcomes) | not reported | insufficient | no_live_jev_outcomes |
| MIN_LIFT_OVER_RANDOM = 0.1 | minimum (gate) | hybrid | not reported (no live Jev outcomes) | not reported | insufficient | no_live_jev_outcomes |
| MIN_LIFT_OVER_DSP = 0.1 | minimum (gate) | hybrid | not reported (no live Jev outcomes) | not reported | insufficient | no_live_jev_outcomes |
| MIN_TOP1_MARGIN = 0.3 | minimum (gate) | hybrid | not reported (no live Jev outcomes) | not reported | insufficient | no_live_jev_outcomes |
| MIN_TOP_K_MEAN = 2.0 | minimum (gate, literal top-10 only) | random | not computed (k = 5 is descriptive) | not computed | insufficient | 0 of 0 eligible queries hold 11 rated candidates |
| MIN_TOP_K_MEAN_LIFT_OVER_DSP = 0.15 | minimum (gate, literal top-10 only) | random | not computed (k = 5 is descriptive) | not computed | insufficient | 0 of 0 eligible queries hold 11 rated candidates |
| MIN_TOP_K_MEAN = 2.0 | minimum (gate, literal top-10 only) | dsp-only | not computed (k = 5 is descriptive) | not computed | insufficient | 0 of 0 eligible queries hold 11 rated candidates |
| MIN_TOP_K_MEAN_LIFT_OVER_RANDOM = 0.3 | minimum (gate, literal top-10 only) | dsp-only | not computed (k = 5 is descriptive) | not computed | insufficient | 0 of 0 eligible queries hold 11 rated candidates |
| MIN_TOP_K_MEAN = 2.0 | minimum (gate, literal top-10 only) | jev-only | not computed (k = 5 is descriptive) | not computed | insufficient | 0 of 0 eligible queries hold 11 rated candidates |
| MIN_TOP_K_MEAN_LIFT_OVER_RANDOM = 0.3 | minimum (gate, literal top-10 only) | jev-only | not computed (k = 5 is descriptive) | not computed | insufficient | 0 of 0 eligible queries hold 11 rated candidates |
| MIN_TOP_K_MEAN_LIFT_OVER_DSP = 0.15 | minimum (gate, literal top-10 only) | jev-only | not computed (k = 5 is descriptive) | not computed | insufficient | 0 of 0 eligible queries hold 11 rated candidates |
| MIN_TOP_K_MEAN = 2.0 | minimum (gate, literal top-10 only) | hybrid | not computed (k = 5 is descriptive) | not computed | insufficient | 0 of 0 eligible queries hold 11 rated candidates |
| MIN_TOP_K_MEAN_LIFT_OVER_RANDOM = 0.3 | minimum (gate, literal top-10 only) | hybrid | not computed (k = 5 is descriptive) | not computed | insufficient | 0 of 0 eligible queries hold 11 rated candidates |
| MIN_TOP_K_MEAN_LIFT_OVER_DSP = 0.15 | minimum (gate, literal top-10 only) | hybrid | not computed (k = 5 is descriptive) | not computed | insufficient | 0 of 0 eligible queries hold 11 rated candidates |
| MAX_COLD_RECOMMENDATION_P95_MS = 2000 | minimum (gate) | random, dsp-only, jev-only, hybrid | not measured (least-sampled arm 0 of 30 valid samples) | not applicable (no complete sample set) | insufficient | 0 of 30 valid latency samples per arm |
| MAX_WARM_RECOMMENDATION_P95_MS = 500 | minimum (gate) | random, dsp-only, jev-only, hybrid | not measured (least-sampled arm 0 of 30 valid samples) | not applicable (no complete sample set) | insufficient | 0 of 30 valid latency samples per arm |
| TARGET_EVALUATORS = 8 | target (non-gating) | random, dsp-only, jev-only, hybrid | descriptive | not applicable (target class) | not evaluated (target class) | - |
| TARGET_RATINGS_PER_PAIR = 3 | target (non-gating) | random, dsp-only, jev-only, hybrid | descriptive | not applicable (target class) | not evaluated (target class) | - |
| TARGET_HELDOUT_QUERIES = 75 | target (non-gating) | random, dsp-only, jev-only, hybrid | descriptive | not applicable (target class) | not evaluated (target class) | - |
| TARGET_PAIRWISE_ACCURACY = 0.68 | target (non-gating) | random, dsp-only, jev-only, hybrid | descriptive | not applicable (target class) | not evaluated (target class) | - |
| TARGET_LIFT_OVER_RANDOM = 0.18 | target (non-gating) | random, dsp-only, jev-only, hybrid | descriptive | not applicable (target class) | not evaluated (target class) | - |
| TARGET_LIFT_OVER_DSP = 0.15 | target (non-gating) | random, dsp-only, jev-only, hybrid | descriptive | not applicable (target class) | not evaluated (target class) | - |
| TARGET_TOP1_MARGIN = 0.5 | target (non-gating) | random, dsp-only, jev-only, hybrid | descriptive | not applicable (target class) | not evaluated (target class) | - |
| TARGET_TOP_K_MEAN = 2.3 | target (non-gating) | random, dsp-only, jev-only, hybrid | descriptive | not applicable (target class) | not evaluated (target class) | - |
| TARGET_COLD_RECOMMENDATION_P95_MS = 1000 | target (non-gating) | random, dsp-only, jev-only, hybrid | descriptive | not applicable (target class) | not evaluated (target class) | - |
| TARGET_WARM_RECOMMENDATION_P95_MS = 250 | target (non-gating) | random, dsp-only, jev-only, hybrid | descriptive | not applicable (target class) | not evaluated (target class) | - |
| MAX_AUDITION_START_P95_MS = 250 | minimum (gate, Phase 1, #39) | none (not a comparison gate) | not evaluated here | not applicable | not evaluated (out of scope for #19) | - |
| RETENTION_WINDOW_DAYS = 14 | fixed (not a gate) | none (not a comparison gate) | not evaluated here | not applicable | not evaluated (out of scope for #19) | - |
| MIN_RETENTION_RATE = 0.2 | minimum (gate, Phase 1) | none (not a comparison gate) | not evaluated here | not applicable | not evaluated (out of scope for #19) | - |
| TARGET_RETENTION_RATE = 0.3 | target (non-gating, Phase 1) | none (not a comparison gate) | not evaluated here | not applicable | not evaluated (out of scope for #19) | - |
| MIN_RETENTION_SELECTIONS = 30 | minimum (gate, Phase 1) | none (not a comparison gate) | not evaluated here | not applicable | not evaluated (out of scope for #19) | - |
| MIN_TUNING_PAIRS = 40 | minimum (gate) | none (not a comparison gate) | not evaluated here | not applicable | not evaluated (no tuning claim is made) | - |

queries_with_11_rated_candidates: 0 of 0 eligible queries. With k = 5 the top-k means are descriptive values, not top-10 values, and no k = 5 mean is compared with a top-10 constant.

## Uncertainty

Bootstrap: 10000 resamples of the eligible query kicks with replacement under BOOTSTRAP_SEED = tera-eval-bootstrap-16-v1; each draw is seeded from sha256(seed, metric, draw index), and the interval is the 95% percentile interval (linear interpolation between order statistics at position (n - 1) * fraction).

| Gated metric | Point estimate | 95% interval | Queries |
| --- | --- | --- | --- |
| pairwise_accuracy (random) | not computed | not computed | 0 |
| top1_margin (random) | not computed | not computed | 0 |
| pairwise_accuracy (dsp-only) | not computed | not computed | 0 |
| top1_margin (dsp-only) | not computed | not computed | 0 |
| pairwise_accuracy (jev-only) | not reported (no live Jev outcomes) | not reported (no live Jev outcomes) | not reported (no live Jev outcomes) |
| top1_margin (jev-only) | not reported (no live Jev outcomes) | not reported (no live Jev outcomes) | not reported (no live Jev outcomes) |
| pairwise_accuracy (hybrid) | not reported (no live Jev outcomes) | not reported (no live Jev outcomes) | not reported (no live Jev outcomes) |
| top1_margin (hybrid) | not reported (no live Jev outcomes) | not reported (no live Jev outcomes) | not reported (no live Jev outcomes) |
| pairwise_accuracy lift dsp-only over random | not computed | not computed | 0 |
| pairwise_accuracy lift jev-only over random | not reported (no live Jev outcomes) | not reported | not reported |
| pairwise_accuracy lift hybrid over random | not reported (no live Jev outcomes) | not reported | not reported |
| pairwise_accuracy lift random over dsp-only | not computed | not computed | 0 |
| pairwise_accuracy lift jev-only over dsp-only | not reported (no live Jev outcomes) | not reported | not reported |
| pairwise_accuracy lift hybrid over dsp-only | not reported (no live Jev outcomes) | not reported | not reported |

The minimum detectable difference is the protocol's predeclared table: about 0.08 accuracy points for pairwise_accuracy (1.96 x 0.30 / sqrt(60)) and about 0.25 rating points for top1_margin (1.96 x 1.00 / sqrt(60)). Every gated lift is set at or above that floor (+0.10 over random, +0.10 over DSP, +0.30 top-1 margin). The top-k gates are not evaluable at this design and are reported insufficient.

Determinism: re-running the recorded command reproduces run.json (sha256:666a7325a0eb301864c187742410153dfc2679134e0a1dd19f2bd147efc4faed), records.json (sha256:07d4f37f87500ef1b4e27618053e30f20941ba89b11709121981cf9c16e34ee6) and this report byte for byte. The run key covers the protocol version, the dataset version and identity digest, the split manifest version and digest, the pair-list digest, every seed, the analysis and ranking versions and weight-table ids, ADAPTER_VERSION, PROMPT_VERSION and the observed model versions.

## Agreement

| Measure | Value | Threshold |
| --- | --- | --- |
| pairs with at least two valid ratings | 0 of 40 | MIN_AGREEMENT_PAIRS = 40 |
| mean absolute deviation between evaluators | not computable | MAX_MEAN_ABSOLUTE_DEVIATION = 1.0 |
| exact-agreement share | not computable | - |
| per-evaluator means | no evaluator has a valid rating | - |

Leave-one-evaluator-out: no evaluator holds a rating, so no variant exists.

## Latency

| Item | Value |
| --- | --- |
| OS | Windows 11 |
| CPU | Intel64 Family 6 Model 140 Stepping 1, GenuineIntel |
| CPU count | 8 |
| Python | 3.13.5 |
| Process state | cold: one fresh child process per cold sample; warm: the identical request repeated in-process on a memo hit |
| Memo | evaluation-scoped memo; not the #26 product decision cache; key fields arm, query, candidates, dataset_version, analysis_version, ranking_version; hits 0, misses 0 |
| Cold sample command | python -m backend.evaluation.comparison latency-request --request <run-dir>/latency/<request>.json --out <run-dir>/latency/<request>.out.json |
| Timed path | the runner's request function only: the client, IPC and audition start (MAX_AUDITION_START_P95_MS = 250) are excluded |
| Retrieval stage | not_implemented |


| Arm | Valid samples | Distinct query kicks | Cold p95 | Warm p95 | Reasons |
| --- | --- | --- | --- | --- | --- |
| random | 0 of 30 | 0 of 10 | not measured | not measured | latency_samples_below_minimum, latency_query_kicks_below_minimum |
| dsp-only | 0 of 30 | 0 of 10 | not measured | not measured | latency_samples_below_minimum, latency_query_kicks_below_minimum |
| jev-only | not reported (no live Jev outcomes) | 0 of 10 | not reported (no live Jev outcomes) | not reported (no live Jev outcomes) | latency_samples_below_minimum, latency_query_kicks_below_minimum |
| hybrid | not reported (no live Jev outcomes) | 0 of 10 | not reported (no live Jev outcomes) | not reported (no live Jev outcomes) | latency_samples_below_minimum, latency_query_kicks_below_minimum |


| Stage | p50 | p95 | Samples |
| --- | --- | --- | --- |
| feature_load | not measured | not measured | 0 |
| filter | not measured | not measured | 0 |
| retrieval | not_implemented | not_implemented | 0 |
| jev | not measured | not measured | 0 |
| ranking | not measured | not measured | 0 |
| total | not measured | not measured | 0 |

Cold and warm totals are compared with MAX_COLD_RECOMMENDATION_P95_MS = 2000 and MAX_WARM_RECOMMENDATION_P95_MS = 500; the targets TARGET_COLD_RECOMMENDATION_P95_MS = 1000 and TARGET_WARM_RECOMMENDATION_P95_MS = 250 are non-gating. Per-stage p50 and p95 for feature load, filter, retrieval, Jev calls (Jev arms only) and ranking are recorded in the private run artifact, whose measured table is captured at latency.json and reused so a re-run reproduces this report byte for byte (--fresh-latency measures again).

## Evidence gaps

| Check | Code | Expected | Actual |
| --- | --- | --- | --- |
| split_manifest | split_manifest_missing | schema_version 1.0 and split_manifest_digest | absent |
| pair_list | pair_list_missing | schema_version 1.0 and split_manifest_digest = the split manifest's | absent |
| pair_membership | missing_prerequisite | every sampled pair's kick and bass are held-out members of the split | 0 of 0 pairs checkable |
| evaluator_assignment | assignment_missing | every pair assigned to at least 2 distinct evaluators | absent |
| pair_counts | pair_counts_below_minimum | 300 sampled pairs over 60 held-out query kicks | 0 of 300 sampled pairs; 0 of 60 query kicks |
| rated_pairs | ratings_missing | every rated pair is a sampled pair | 0 sessions, 0 rated pairs |
| analysis_manifests | analysis_manifest_missing | every held-out sample used resolves to one complete entry with a single analysis_version | 2 of 2 declared analysis manifests are absent |

## Deviations

### Session rows

No session exists, so no session row carries a validate exit status.

- unscored candidates: random 0 of 0 rated candidates scored; dsp-only 0 of 0 rated candidates scored; jev-only not reported (no live Jev outcomes); hybrid not reported (no live Jev outcomes)
- identical orderings: random 0 query pairs; dsp-only 0 query pairs; jev-only not reported (no live Jev outcomes); hybrid not reported (no live Jev outcomes)
- unrated queries: 0
- the #65 split-manifest and evaluator-assignment schemas are this runner's declared expectation, because #65 has not landed: schema_version 1.0, dataset_version, the three seeds, tuning and held_out with kicks and basses, and the manifest's recorded split_manifest_digest for the manifest; schema_version 1.0, assignment_seed, split_manifest_digest and assignments (pair id to evaluator ids) for the assignment. This runner checks that the pair list's digest equals the manifest's recorded digest, and it never re-derives a manifest digest, a seed, a split, an assignment or a pair list
- no threshold was lowered, no metric was changed and no denominator was redefined for this run
- (latency: cold samples run as one fresh child process per sample; the exact spawned command is recorded in the private run artifact and the report carries its template so no private path is published)

No rating was fabricated and no private record left the device.

## No-held-out-tuning attestation

No tuning used held-out results: no held-out rating, arm output, score or rank was read while the split, seeds and versions were chosen.

No tuning claim is made for this run: the published weight tables and constants are used unchanged, and no tuning rating set was read.

## Reproduction

Run from the repository root:

~~~
python -m backend.evaluation.comparison preflight
python -m backend.evaluation.comparison report
~~~

Both commands read only the declared inputs and write only _docs/ranking-comparison-19.md and .local-evaluation/ranking-comparison/runs/0660820cf549b65ffc2fd9afefd746169bf8f913ba5e3715e996a0677b56c09e/. Private paths stay on the device; this report carries aggregate counts, published version, seed, weight-table and digest strings and the hardware class only.

## Out of scope

- Setting, changing or reinterpreting a threshold, metric, seed, sampling rule, split rule or record field: #16 owns them.
- The pair-rating utility, playback, resume, corrections, status and export: #17.
- Collecting, recruiting for or running producer sessions: #18.
- The pair sampler, split manifest and evaluator assignment: #65.
- The proceed / revise / stop decision: #20.
- Product latency work (#37), the recommendation API (#28), retrieval (#25), the decision cache (#26) and audition start (#39).
- Unscored or failed candidates in the recommendation contracts (#62); bounded parallel Jev dispatch (#64).
- Retention (#36) and personalisation evaluation (#58, #59).
- Growing the pool (#67), recruiting a standing panel (#66) and the literal top-10 budget (#69).
- Confirming the assumed TypeSafe Jev HTTP mapping and the real model versions: #14 reports the live check UNVERIFIED; tracked as #68.
