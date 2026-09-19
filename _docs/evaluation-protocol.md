## Protocol version and change control

`PROTOCOL_VERSION = "tera-eval-protocol-v1"`. **Freeze date: 2026-09-19.**

This protocol was frozen on the freeze date, before any comparison result exists. It decides how
kick-bass pairs are rated and blinded, how tuning and held-out partitions are kept sample-disjoint,
which ranking-lift, latency and retention numbers count as pass, fail or insufficient evidence for
random, DSP-only, Jev-only and hybrid ranking, and what [#17](https://github.com/gmphto/tera/issues/17)
must record and [#19](https://github.com/gmphto/tera/issues/19) must report.

The document is public-safe. It quotes aggregate counts already published in
`_docs/evaluation-pool.md` and `_docs/compressed-sample-preparation.md` and version
strings already published in `_docs/dsp-baseline.md`, `_docs/jev-questions.md`,
`_docs/jev-adapter.md` and `_docs/hybrid-ranking.md`, and nothing else. It contains
no sample id, local path, pack name, hash value, audio, evaluator identity or private diagnostic.
The split manifest and the detailed rating records stay under the gitignored `.local-evaluation/`
directory; the manifest is written by #17/#18, never by this document.

### Change log

| Version | Date | Change | Reason |
| --- | --- | --- | --- |
| `tera-eval-protocol-v1` | 2026-09-19 | Initial freeze: rating anchors, evaluator minimums, pool split rule and worked arithmetic, arm-independent pair sampler, blinding and session procedure, aggregation and disagreement rules, the four arms, metric formulas, uncertainty and verdicts, thresholds, latency measurement, Phase 1 retention, and the #17 and #19 record contracts | [#16](https://github.com/gmphto/tera/issues/16) requires the rating, blinding, splitting and evidence rules to be decided before any comparison result is inspected |

### Change control rule

* Any edit to a rating anchor, a sampling rule, a split rule, a metric, a seed or a threshold
  requires a new `PROTOCOL_VERSION` and a fresh held-out run.
* An edit made after any comparison result has been inspected may never be reported under the old
  version.
* Before any result exists, a predeclared value may be replaced only by recording the replacement
  and its rationale in the change log above; the replacement then binds every later run.
* Every run reports the `PROTOCOL_VERSION` it used. #19 may not lower a threshold, change a
  metric or re-sample after seeing a result; if it must, it is a new protocol version and a fresh
  held-out run.

### Frozen constants

Every value predeclared under Constraints appears once below, with its unit or scale and its
minimum/target class.

| Name | Value | Unit / scale | Class |
| --- | --- | --- | --- |
| `PROTOCOL_VERSION` | `tera-eval-protocol-v1` | version string | version |
| `SPLIT_SEED` | `tera-eval-split-16-v1` | seed string | seed |
| `PAIR_SAMPLER_SEED` | `tera-eval-pairs-16-v1` | seed string | seed |
| `ASSIGNMENT_SEED` | `tera-eval-assignment-16-v1` | seed string | seed |
| `ORDER_SEED` | `tera-eval-order-16-v1` | seed string | seed |
| `RANDOM_ARM_SEED` | `tera-eval-random-16-v1` | seed string | seed |
| `BOOTSTRAP_SEED` | `tera-eval-bootstrap-16-v1` | seed string | seed |
| `TUNING_FRACTION` | 0.25 | share of a role's unique samples | rule |
| `MIN_POOL_KICKS` | 80 | pool kicks | minimum |
| `MIN_POOL_BASSES` | 8 | pool basses/sub-basses | minimum |
| `MIN_EVALUATORS` | 5 | counted evaluators | minimum |
| `TARGET_EVALUATORS` | 8 | counted evaluators | target |
| `MIN_RATINGS_PER_PAIR` | 2 | valid ratings | minimum |
| `TARGET_RATINGS_PER_PAIR` | 3 | valid ratings | target |
| `MIN_HELDOUT_QUERIES` | 60 | held-out query kicks | minimum |
| `TARGET_HELDOUT_QUERIES` | 75 | held-out query kicks | target |
| `RATED_CANDIDATES_PER_QUERY` | 5 | rated candidates per query | rule |
| `MIN_HELDOUT_PAIRS` | 300 (60 x 5) | held-out pairs | minimum (derived) |
| `MIN_TUNING_PAIRS` | 40 (only if a tuning claim is made) | tuning pairs | minimum |
| `MIN_PAIR_COVERAGE` | 0.80 | rated share of sampled pairs | minimum |
| `MIN_AGREEMENT_PAIRS` | 40 | pairs with at least two valid ratings | minimum |
| `MAX_MEAN_ABSOLUTE_DEVIATION` | 1.00 | rating points (0-3 scale) | minimum |
| `MAX_RECOGNISED_RATE` | 0.20 | flagged share of a session | minimum |
| `MIN_SCORED_SHARE_PER_QUERY` | 0.50 | scored share of the rated subset | minimum |
| `MIN_RATED_CANDIDATES_FOR_TOP10_GATE` | 11 | rated candidates per query | rule |
| `MIN_LATENCY_REQUESTS_PER_ARM` | 30 | latency measurements per arm | minimum |
| `BOOTSTRAP_RESAMPLES` | 10000 | resamples | rule |
| `MIN_PAIRWISE_ACCURACY` | 0.60 | accuracy points | minimum |
| `TARGET_PAIRWISE_ACCURACY` | 0.68 | accuracy points | target |
| `MIN_LIFT_OVER_RANDOM` | +0.10 | accuracy points | minimum |
| `TARGET_LIFT_OVER_RANDOM` | +0.18 | accuracy points | target |
| `MIN_LIFT_OVER_DSP` | +0.10 | accuracy points | minimum |
| `TARGET_LIFT_OVER_DSP` | +0.15 | accuracy points | target |
| `MIN_DSP_PAIRWISE_ACCURACY` | 0.55 | accuracy points | minimum |
| `MIN_JEV_ONLY_PAIRWISE_ACCURACY` | 0.55 | accuracy points | minimum |
| `MIN_TOP1_MARGIN` | +0.30 | rating points (0-3 scale) | minimum |
| `TARGET_TOP1_MARGIN` | +0.50 | rating points (0-3 scale) | target |
| `MIN_TOP_K_MEAN` | 2.00 | rating points (0-3 scale) | minimum (literal top-10 only) |
| `TARGET_TOP_K_MEAN` | 2.30 | rating points (0-3 scale) | target |
| `MIN_TOP_K_MEAN_LIFT_OVER_RANDOM` | +0.30 | rating points (0-3 scale) | minimum (literal top-10 only) |
| `MIN_TOP_K_MEAN_LIFT_OVER_DSP` | +0.15 | rating points (0-3 scale) | minimum (literal top-10 only) |
| `MAX_COLD_RECOMMENDATION_P95_MS` | 2000 | milliseconds, p95 | minimum |
| `TARGET_COLD_RECOMMENDATION_P95_MS` | 1000 | milliseconds, p95 | target |
| `MAX_WARM_RECOMMENDATION_P95_MS` | 500 | milliseconds, p95 | minimum |
| `TARGET_WARM_RECOMMENDATION_P95_MS` | 250 | milliseconds, p95 | target |
| `MAX_AUDITION_START_P95_MS` | 250 | milliseconds, p95 | minimum (Phase 1, #39) |
| `RETENTION_WINDOW_DAYS` | 14 | days | rule |
| `MIN_RETENTION_RATE` | 0.20 | retained share of completed selections | minimum (Phase 1) |
| `TARGET_RETENTION_RATE` | 0.30 | retained share of completed selections | target (Phase 1) |
| `MIN_RETENTION_SELECTIONS` | 30 | completed selections | minimum (Phase 1) |

The class column uses the classes the protocol declares: **version**, **seed** and **rule** values
constrain how a run is computed and are never compared against a result; a **minimum** is a gate
(a result below it, or an evidence count below it, is never a pass); a **target** is non-gating and
is reported for direction, not for pass/fail. A derived value is written with the arithmetic that
produces it. Published version strings this protocol pins are listed in
Section "Systems under comparison"; they come from landed work and are not predeclared here.

## Definitions

* **pair** - one role-confirmed kick times one role-confirmed bass or sub-bass from one frozen
  dataset version. Only the kick side is the query; the bass side is never a query.
* **pair id** - the ordered `(kick_sample_id, bass_sample_id)` plus `dataset_version`.
  The pair id is the only identity a rating is attached to; it is never exported in a committed
  report.
* **content identity** - the dataset's deterministic identity of a sample's decoded content.
  Mappings that decode to identical content share one content identity and one representative; a
  duplicate mapping never adds a second query, candidate or pair, and a pair never holds two samples
  with the same content identity.
* **eligible pair** - a pair whose kick and bass are both role-confirmed, readable, analysed at the
  recorded analysis version and in the same partition. A sample that is unreadable, has an
  unconfirmed role or has no analysis never forms a pair or a candidate.
* **query kick** - the kick of a pair, and the query side of one candidate set. A query kick draws
  candidates only from its own partition.
* **candidate set** - for one query kick, the arm-independent set of held-out basses the sampler
  draws (`RATED_CANDIDATES_PER_QUERY = 5`, or all held-out basses when fewer than five
  exist). All four arms order this same set for that query.
* **rated subset** - the candidates of one query whose pair clears the rating floor of
  Section "Aggregation and disagreement" (at least `MIN_RATINGS_PER_PAIR = 2` valid
  ratings after every exclusion). Metrics are computed only on the rated subset.
* **rating** - one recorded judgment of one presentation of one pair: exactly one of the four scale
  labels of Section "Rating scale and anchors", mapped to 0-3. `skip` is a separate
  recorded state and is never a rating.
* **evaluator** - a producer who hears and rates pairs and satisfies the counted-evaluator criteria
  of Section "Evaluators".
* **session** - one evaluator's run of the rating utility over one assigned subset, identified by
  `session_id` and the anonymous `evaluator_id` and bounded by
  `started_at` and `finished_at`.
* **tuning partition** - the kicks and basses the split assigns to tuning. Tuning pairs may be used
  only to build or tune an arm, and only with the `MIN_TUNING_PAIRS = 40` floor if a
  tuning claim is made.
* **held-out partition** - every kick and bass the split does not assign to tuning. Every primary
  metric of this protocol uses held-out pairs only.
* **random**, **dsp-only**, **jev-only**, **hybrid** - the four arm names, each pinned in
  Section "Systems under comparison". No other arm name may appear in a comparison.

Pool floors: `MIN_POOL_KICKS = 80` and `MIN_POOL_BASSES = 8`. A pool
below either floor is insufficient evidence for the primary comparison, not a failure: the
comparison is not run to a pass/fail verdict and the shortfall is reported.

## Rating scale and anchors

The scale has exactly four labels, mapped to 0-3. **good-or-better** means a rating value of at
least 2 (`good` or `excellent`).

| Label | Value | Anchor: the pair heard as a two-element kick + bass loop, in isolation, at one fixed monitoring gain |
| --- | --- | --- |
| `poor` | 0 | Not usable together: the low end fights for the same band and/or the onsets collide; the producer would not build with this pairing. |
| `acceptable` | 1 | Usable but not sought: the loop functions, but something is audibly compromised (masking, density, timing); the producer would accept it only if nothing better were available. |
| `good` | 2 | Deliberately kept: the loop works and the producer would keep it in the project and choose it again. |
| `excellent` | 3 | Exceptional: the two elements sound made for each other; the producer would reach for this pairing first. |

* One presentation carries exactly one label, so the four-label scale cannot produce a tie.
* `skip` is a separate recorded state with a reason from the fixed set
  `failed-playback`, `cannot-judge`, `recognised`,
  `other`. It is never a rating and a `skip` is never scored as
  `poor`.
* The evaluator judges the pair, not the sample on its own, not the pack it came from and not the
  genre: two similar-sounding elements are not thereby a good pair, and a good sample is not
  thereby a good pairing.

## Evaluators

A **counted evaluator** is a producer who can hear the pairs on the session's monitoring and who had
no involvement in implementing or tuning the arm being judged. Any other participant's ratings are
recorded but not counted.

| Name | Value | Rationale |
| --- | --- | --- |
| `MIN_EVALUATORS` | 5 | Five distinct producers is the fewest that can give every sampled pair its second rating while keeping the session affordable. |
| `TARGET_EVALUATORS` | 8 | Eight producers is the recruitment target that gives every pair a third rating at the target design. |
| `MIN_RATINGS_PER_PAIR` | 2 | Two distinct evaluators per pair is the floor at which the pair aggregate is a property of the pair rather than of one ear. |
| `TARGET_RATINGS_PER_PAIR` | 3 | A third rating makes the per-pair median robust to one outlier. |

Conflict rules:

* Every participant declares any involvement in implementing or tuning an arm before the session.
  A declared involvement excludes that participant from the count for that arm's claim, and from the
  primary comparison of that arm; the exclusion is reported.
* A participant who has seen any arm's scores, rankings or recommendation lists for sampled pairs is
  not counted for those pairs.
* Evaluators do not see one another's ratings, or any aggregate, before the session closes.

Dropout rules:

* A session that ends early keeps every completed rating; its unfinished pairs return to the
  unassigned pool and are re-assigned under the same assignment rule.
* A session below a minimum (fewer than `MIN_EVALUATORS` counted evaluators, or fewer
  than `MIN_RATINGS_PER_PAIR` valid ratings on a pair) is recorded as insufficient
  evidence, never as pass or fail.
* No evaluator's ratings may be counted twice: one evaluator contributes at most one valid rating per
  pair (a later answered presentation for the same pair supersedes the earlier one through
  `correction_of`), and one evaluator counts once toward the evaluator total however
  many sessions that evaluator runs.

## Pool, splits and leakage control

### The pool as it is

* The current pool is the #61 preparation
  (`_docs/compressed-sample-preparation.md`): **100 selected kicks and 46 basses**,
  plus 23 kick reserves, with the **54-bass shortfall** against the plan's 100/100 target recorded
  explicitly (`_docs/plan.md` section 5 asks for approximately 100 kicks and 100
  basses).
* The #10 dataset remains the other version (`_docs/evaluation-pool.md`): **81 kicks
  and 3 basses**, with deficits of 19 kicks and 97 basses. It is named here as the earlier version;
  it does not support this protocol's minimum design and is not mixed with the current pool.
* The shortfall is a documented fact, not a hidden one: the protocol runs on the 100/46 pool and
  reports the 54-bass shortfall beside every result.
* Pool floors: `MIN_POOL_KICKS = 80` and `MIN_POOL_BASSES = 8`. The
  current pool clears both (100 at or above 80; 46 at or above 8). If either floor were missed, the
  primary comparison would be **insufficient evidence, not fail**.

### Worked split arithmetic

The split uses `TUNING_FRACTION = 0.25` and
`SPLIT_SEED = "tera-eval-split-16-v1"`. For a role with `N` unique
samples, the tuning count is `min(N - 1, max(1, ceil(0.25 x N)))` and the rest are
held-out.

| Role | Pool `N` | Tuning | Held-out | Arithmetic |
| --- | ---: | ---: | ---: | --- |
| kicks | 100 | **25** | **75** | `ceil(0.25 x 100) = 25`; `min(100 - 1, 25) = 25`; `100 - 25 = 75` |
| basses / sub-basses | 46 | **12** | **34** | `ceil(0.25 x 46) = ceil(11.5) = 12`; `min(46 - 1, 12) = 12`; `46 - 12 = 34` |

Membership, stated explicitly: the **tuning partition holds 25 kicks and 12 basses** (300 distinct
tuning pairs are available, well above `MIN_TUNING_PAIRS = 40`), and the **held-out
partition holds 75 kicks and 34 basses**. The minimum design draws
`MIN_HELDOUT_QUERIES = 60` of the 75 held-out kicks and leaves 15 unused; the target
design draws `TARGET_HELDOUT_QUERIES = 75` and uses all of them. Both partitions clear
the minimum design: 75 held-out kicks is at or above 60, and 34 held-out basses is at or above the
five candidates a query must draw.

What the top-10 gate would cost: the literal top-10 gate needs at least
`MIN_RATED_CANDIDATES_FOR_TOP10_GATE = 11` rated candidates per query. The current 34
held-out basses can supply that, but only at a much larger rating budget: 60 queries x 11 candidates
= 660 sampled pairs and at least 1320 ratings (about 264 per evaluator at 5 evaluators), against 300
pairs and 600 ratings for the minimum design. The minimum design therefore reports the top-k mean
metrics but does not gate on them.

### Split rule and its guarantee

* For a role with `N` at least 2 unique samples, the tuning count is
  `min(N - 1, max(1, ceil(TUNING_FRACTION x N)))` and every other sample is held-out.
  The `N - 1` cap guarantees at least one held-out sample per role. A role with
  `N = 1` puts its only sample in held-out.
* A content-identity duplicate always takes its representative's partition and never crosses the
  split; the duplicate is never counted as a second sample.
* The split manifest is written before any tuning run, and its digest is recorded in every session
  record and in the #19 report. The manifest stays under `.local-evaluation/`.
* No tuning run may read a held-out rating. A tuning run that reads one invalidates the held-out
  result: the comparison is void, and a new `PROTOCOL_VERSION` with a fresh held-out
  run is required.
* Same-pack pairs (a kick and a bass whose recorded pack provenance is the same) are allowed, but
  every metric is reported twice: with them and without them. A conclusion that flips when they are
  removed is inconclusive.

## Pair sampling

The sampler is arm-independent and reproducible.

| Name | Value | Class |
| --- | --- | --- |
| `PAIR_SAMPLER_SEED` | `tera-eval-pairs-16-v1` | seed |
| `MIN_HELDOUT_QUERIES` | 60 | minimum |
| `TARGET_HELDOUT_QUERIES` | 75 | target |
| `RATED_CANDIDATES_PER_QUERY` | 5 (or all held-out basses when fewer than five exist) | rule |
| `MIN_HELDOUT_PAIRS` | 300, derived as `60 x 5` | minimum (derived) |
| `MIN_TUNING_PAIRS` | 40, required only if a tuning claim is made | minimum |
| `ASSIGNMENT_SEED` | `tera-eval-assignment-16-v1` | seed |
| `ORDER_SEED` | `tera-eval-order-16-v1` | seed |

### Per-query draw rule

The draw depends only on the seed, the dataset version and the candidate identities. It never reads
an arm's output, a score, a rank or a rating.

1. Order the held-out kicks by the key `h_query(q)` =
   SHA-256(`PAIR_SAMPLER_SEED`, dataset version, `"query"`,
   `q.content_identity`), ties broken by content identity ascending, and take the first
   `MIN_HELDOUT_QUERIES` (or `TARGET_HELDOUT_QUERIES` for the target
   design) as the query kicks.
2. For each query kick `q` in that order, order the held-out basses by the key
   `h_pair(q, b)` = SHA-256(`PAIR_SAMPLER_SEED`, dataset version,
   `"pair"`, `q.content_identity`, `b.content_identity`),
   ties broken by content identity ascending, and take the first
   `RATED_CANDIDATES_PER_QUERY` (or all held-out basses when fewer than five exist).
   Each query draws distinct basses; the same bass may appear in several queries.
3. A draw is repeatable from the seed alone: the same dataset version and the same candidate
   identities produce the same pairs on any machine.

### Coverage guarantee and its deterministic adjustment

Each held-out bass appears in at least one sampled query whenever the slot budget allows, that is
whenever `queries x RATED_CANDIDATES_PER_QUERY` is at least the number of held-out
basses (at the minimum design 60 x 5 = 300 against 34 basses, so the guarantee is active).

After the seeded draw, any held-out bass that appears in no sampled pair is repaired in ascending
content-identity order. For an uncovered bass, compute for every still-replaceable slot the key
SHA-256(`PAIR_SAMPLER_SEED`, dataset version, `"repair"`, query kick
identity, slot index, bass content identity) and replace the slot with the smallest key whose
candidate is not the last remaining occurrence of a bass that is already covered; ties are broken by
query kick identity then slot index ascending. A replacement that would uncover another bass is not
made, so repair never trades one uncovered bass for another. If no replaceable slot exists, the bass
stays uncovered and the coverage shortfall is reported as a count; it is not hidden. The repaired
draw is still a function of the seed, the dataset version and the candidate identities alone, and it
is persisted with the split manifest.

### Balanced assignment

`ASSIGNMENT_SEED` orders the counted evaluators in a seeded permutation, and sampled
pairs are assigned in a canonical order (query kick identity ascending, then bass content identity
ascending). Each pair is assigned the first `MIN_RATINGS_PER_PAIR = 2` distinct
evaluators (or `TARGET_RATINGS_PER_PAIR = 3` at the target design) that have the fewest
assigned pairs so far, skipping an evaluator already assigned to that pair, with the seeded
permutation breaking ties. This gives every sampled pair at least
`MIN_RATINGS_PER_PAIR` distinct evaluators and keeps per-evaluator loads within one
pair of one another. Order of presentation inside a session is a separate seeded permutation
(Section "Blinding and session procedure").

### Rating burden

| Design | Queries | Pairs | Ratings per pair | Ratings | Evaluators | Ratings per evaluator |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| minimum | 60 | 300 | 2 | 600 | 5 | about 120 |
| target | 75 | 375 | 3 | 1125 | 8 | about 141 |

300 pairs x 2 ratings = 600 ratings, about 120 per evaluator at 5 evaluators; 375 pairs x 3 ratings
= 1125 ratings, about 141 per evaluator at 8 evaluators (1125 / 8 = 140.625). A pair that has not
reached the floor is unrated and covered by Section "Aggregation and disagreement".

## Blinding and session procedure

### What is concealed

Arm identity, score, rank, file name, local path, pack name and any signal that the pair was
machine-recommended are concealed before, during and after rating. They appear nowhere in the
rating utility's presentation, in its session records or exports, or in any feedback shown to the
evaluator.

No feedback is shown after a rating: no correctness, no score, no comparison, no aggregate and no
progress share that would reveal another evaluator's answer.

### Presentation order, playback and skip

* Order is a persisted seeded permutation under `ORDER_SEED = "tera-eval-order-16-v1"`.
  Within it, two consecutive presentations do not share the query kick wherever the assignment
  permits it (that is, whenever at least two query kicks still have unshown pairs); the permutation
  is persisted with the session so a resume continues the same order.
* Playback uses one fixed documented gain for the whole session
  (`playback_gain_db`, recorded with the monitoring description) and no per-sample
  normalisation.
* A failed or incomplete playback can never become a rating. A rating whose
  `playback_completed` is false is invalid.
* `skip` carries a reason from the fixed set `failed-playback`,
  `cannot-judge`, `recognised`, `other`.
* A presentation the evaluator recognises is flagged. The flagged rating is excluded from the pair
  aggregate, and a session whose flagged share exceeds
  `MAX_RECOGNISED_RATE = 0.20` is excluded from the primary metric and re-run on a
  different pair subset or by a different evaluator.

### Evaluator instruction text

The utility shows this text in full before the first presentation, and the same text is recorded
with the session.

> **What you are doing.** You are rating kick-bass pairs for a local music-production tool. The tool
> is deciding whether machine-ranked pairing suggestions are worth keeping, and it needs your ear,
> not your guess.
>
> **What you hear.** One presentation at a time: one kick and one bass or sub-bass, played as a
> short two-element loop, in isolation. Every presentation plays at the same fixed monitoring gain
> for this session; set your monitoring once, before you start, and do not change it while the
> session runs. If the monitoring or the level must change, stop and start a new session.
>
> **What you do not see.** Nothing tells you where the two samples came from, which pack or folder
> they are in, how the pair was chosen, whether a machine recommended it, or how any system scored
> or ranked it. You will not be shown any score, ranking, feedback or another evaluator's answer,
> before or after you rate.
>
> **What you judge.** The pair: how the kick and the bass work together in the loop. Do not judge
> the sample on its own, the pack, the genre, or how similar the two elements are. Two elements that
> sound alike are not thereby a good pair.
>
> **The four labels.** Choose exactly one per presentation:
>
> * **poor (0)** - not usable together. The two elements fight for the same band and/or their
>   onsets collide; you would not build with this pairing.
> * **acceptable (1)** - usable but not sought. The loop works, but something is compromised
>   (masking, density, timing); you would accept it only if nothing better were available.
> * **good (2)** - deliberately kept. The loop works; you would keep it in the project and choose
>   it again.
> * **excellent (3)** - exceptional. The two elements sound made for each other; you would reach
>   for this pairing first.
>
> **If you cannot rate it.** Choose **skip** and exactly one reason: **failed-playback** (the
> playback failed, stopped or did not finish), **cannot-judge** (you heard the whole pair but cannot
> form one of the four labels), **recognised** (you recognise the sample or the pairing, so your
> judgment is not blind), or **other**. A skip is not a rating: it is never counted as poor and
> never enters a metric.
>
> **If you recognise something.** Flag the presentation. A recognised presentation is not a blind
> judgment: its rating is excluded from the pair's aggregate, and a session where more than 20% of
> presentations are flagged is excluded from the primary metric and re-run with a different subset
> or a different evaluator.
>
> **Playback must finish.** A presentation whose playback fails or does not complete cannot be
> rated; the only answer available is skip with failed-playback, and a rating recorded without a
> completed playback is invalid.
>
> **Time and stopping.** Rate at the pace that keeps your judgment stable. There is no timer and no
> target count. Stopping early keeps every rating you completed, and resuming later continues the
> same order without repeating a completed answer.

## Aggregation and disagreement

### The three-level computation

1. **Per-pair aggregate** - the median of the pair's valid rating values, taking the lower middle
   value on an even count (so 2 and 3 give 2, never 2.5). Valid means: one label of the four,
   `playback_completed` true, not flagged as recognised, and not superseded by a
   correction.
2. **Per-query metrics** - every metric of Section "Metrics" is computed for one query kick over
   the rated subset of that query.
3. **Per-arm metric** - the mean of the per-query values over the queries eligible for that metric
   and that arm.

Validity floor: `MIN_RATINGS_PER_PAIR = 2`. A pair below the floor is unrated, leaves
the rated subset, and may leave its query with too few rated candidates for a metric, which makes
that query ineligible for that metric. The removal is reported as a count.

### Disagreement measures

| Measure | Definition |
| --- | --- |
| Mean absolute deviation between evaluators on the same pair | Over each pair with at least two valid ratings, the mean of `abs(r_i - r_j)` over every ordered pair of distinct evaluators on that pair; averaged over the pairs in scope. On a two-rating pair this is `abs(r_1 - r_2)`. |
| Exact-agreement share | The share of evaluated pairs whose valid ratings are all equal. |
| Per-evaluator means | The mean of each evaluator's valid rating values, reported per evaluator. |

Reportability gates: `MAX_MEAN_ABSOLUTE_DEVIATION = 1.00` and
`MIN_AGREEMENT_PAIRS = 40`. A comparison whose mean absolute deviation is above 1.00
rating point is inconclusive; a comparison with fewer than 40 pairs carrying at least two valid
ratings is not reportable and is insufficient evidence.

A deviating evaluator or session is reported and never silently dropped: the primary metric is
recomputed with and without it, both values are reported, and a conclusion that flips is
inconclusive.

## Systems under comparison

Common rules for all four arms:

* All four arms order the same eligible candidate set per query (Section "Definitions").
* An unscored candidate is placed deterministically at the tail of that arm's ordering, sorted by
  the arm's documented tie-break, and is never counted in a metric.
* A query where an arm scores less than
  `MIN_SCORED_SHARE_PER_QUERY = 0.50` of the rated subset is ineligible for that arm.
* A comparison uses the intersection of the eligible queries of the compared arms; the intersection
  size is reported.
* Only `source = "interface"` Jev outcomes are live evidence. A
  `source = "double"` outcome can never support a lift claim
  (`_docs/jev-adapter.md`).

Published version pins, quoted from landed work:

| Pin | Value | Source |
| --- | --- | --- |
| `RANKING_VERSION` | `dsp-baseline-v1` | `_docs/dsp-baseline.md` (#12) |
| DSP weight-table id | `dsp-baseline-weights-1` | `_docs/dsp-baseline.md` (#12) |
| `PROMPT_VERSION` | `jev-questions-v1` | `_docs/jev-questions.md` (#13) |
| `ADAPTER_VERSION` | `jev-adapter-v1` | `_docs/jev-adapter.md` (#14) |
| `HYBRID_RANKING_VERSION` | `hybrid-ranking-v1` | `_docs/hybrid-ranking.md` (#15) |
| Hybrid weight-table id | `hybrid-weights-1` | `_docs/hybrid-ranking.md` (#15) |
| `CONFIDENCE_THRESHOLD` | 0.80 | `_docs/filter-policy.md` (same 0.80 the DSP baseline applies to key and fundamental confidence) |

### The four arms

* **`random`** - under `RANDOM_ARM_SEED = "tera-eval-random-16-v1"`, a
  documented per-query seeded draw without replacement on the same eligible population (the query's
  candidate set). It is repeatable from the seed, and every result is reported beside the arm's
  analytic expectation (chance 0.50 for pairwise accuracy, 0.00 for top-1 margin).
* **`dsp-only`** - `backend.palette.ranking.rank_candidates` from #12
  with `RANKING_VERSION = "dsp-baseline-v1"` and weight table
  `dsp-baseline-weights-1`, and no Jev input of any kind. Its ordering, missing-feature
  handling, confidence value and `candidate_id` ascending tie-break are the #12 rules
  unchanged.
* **`jev-only`** - the six #13 contract dimensions only (`frequency`,
  `transient`, `tonal`, `rhythmic`, `texture`,
  `arrangement`), with the label values `very-poor` 0, `poor`
  0.25, `neutral` 0.5, `good` 0.75 and `excellent` 1.0,
  combined with equal 1/6 weights renormalised over the available dimensions. Abstentions and
  unavailable dimensions are excluded, and the covered-weight confidence (the renormalised weight
  covering the available dimensions) is recorded for every candidate. Ties are broken
  deterministically by `bass_sample_id` ascending. No DSP compatibility rule
  contributes to this arm's score; measured facts may be inputs to the #13 questions.
  One predeclared probability-weighted variant is reported beside the primary variant as a
  robustness check: the dimension score is the probability-weighted mean
  `sum(p_label x value(label))` over the five labels of the answered question,
  combined with the same equal 1/6 weights and the same exclusions and tie-break. Neither variant is
  replaced after a result is seen, and neither variant is a separate gate; if the two variants
  disagree on a gated conclusion, the comparison is inconclusive.
* **`hybrid`** - #15's versioned weights and confidence handling:
  `HYBRID_RANKING_VERSION = "hybrid-ranking-v1"` with weight table
  `hybrid-weights-1`. The hybrid gate is **insufficient** - never `pass` or
  `fail` - until a #15 ranking version exists; at this freeze a #15 ranking version
  exists (`hybrid-ranking-v1`, #15 landed), so #19 evaluates the gate normally when the
  run names the version it used. A run that cannot name the hybrid ranking version it used records
  the hybrid gate as insufficient.

## Metrics

Every metric is a formula over recorded ratings; no metric may be redefined after a result is seen.

| Metric | Formula | Denominator | Eligibility |
| --- | --- | --- | --- |
| `pairwise_accuracy(S)` | Share of decided within-query candidate pairs that the arm orders correctly: for each unordered pair of distinct candidates in the rated subset with different aggregate ratings, the arm is correct when it ranks the higher-rated candidate first. Chance is 0.50. | Decided candidate pairs over the eligible queries | At least 3 rated candidates in the query; a query with no decided pair is recorded with its count and contributes no value to the mean |
| `top1_margin(S)` | Rating of the arm's top-ranked candidate in the rated subset minus the mean rating of the rated subset, per query, then the mean over eligible queries. Random expectation is 0.00. | Eligible queries | At least 2 rated candidates in the query |
| `topk_mean(S)` | Mean aggregate rating of the arm's top `k` candidates in the rated subset, with `k = min(10, number of rated candidates in the query)` | Eligible queries | Non-discriminating when `k` equals the rated-subset size; gated only when every eligible query has at least `MIN_RATED_CANDIDATES_FOR_TOP10_GATE = 11` rated candidates |
| Per-pair lift | For `pairwise_accuracy`, the per-pair difference `1[S correct] - 1[B correct]` on each decided pair | Decided candidate pairs | Same as `pairwise_accuracy` |
| Per-query paired difference | For every lift, `d(q) = metric_S(q) - metric_B(q)` for all eligible queries of both compared arms | Intersection of eligible queries | Both arms eligible on the query |
| `pair_coverage` | Rated pairs divided by sampled pairs | Sampled pairs over the queries in scope | Minimum `MIN_PAIR_COVERAGE = 0.80` |
| Unscored share | One minus the scored share, where the scored share of query `q` for arm `S` is the number of candidates in the rated subset that `S` scored, divided by the rated-subset size | Rated subset | A query is ineligible for an arm when the scored share is below `MIN_SCORED_SHARE_PER_QUERY = 0.50` |
| Disagreement measures | Section "Aggregation and disagreement" | Pairs with at least two valid ratings | `MIN_AGREEMENT_PAIRS = 40` reportable pairs |

Aggregation order: valid rating set per pair, then per-pair aggregate, then per-query metric over
the rated subset, then per-arm mean over eligible queries, then the per-query paired differences and
the lift against the comparison arm, then the cluster-bootstrap interval of
Section "Uncertainty and decision rules".

Further rules:

* A score tie is resolved only by the arm's documented tie-break (`candidate_id`
  ascending for `dsp-only` and `hybrid` via #12/#15, and
  `bass_sample_id` ascending for `jev-only`).
* A query where two arms produce the identical ordering is recorded and contributes a zero
  difference rather than being dropped.
* No skipped, failed, below-floor or recognised rating ever enters a metric.
* Every gate is reported with its point estimate, its 95% interval and the minimum or target it is
  compared with.

## Uncertainty and decision rules

### Cluster bootstrap

`BOOTSTRAP_RESAMPLES = 10000` and
`BOOTSTRAP_SEED = "tera-eval-bootstrap-16-v1"`. The bootstrap resamples query kicks
with replacement (10000 resamples of the eligible query set) and recomputes every per-query metric
on each resample, so dependence inside a query and a bass appearing in several queries are
preserved. The interval is the 95% percentile interval: the 2.5th and 97.5th percentiles of the
resampled values.

### Minimum detectable difference at the minimum design

With `n = MIN_HELDOUT_QUERIES = 60` eligible queries and a per-query paired-difference
standard deviation `sigma`, the detection floor is about
`1.96 x sigma / sqrt(60)`:

| Gated metric | Assumed per-query paired-difference SD | Minimum detectable difference | Gate at or above it |
| --- | ---: | ---: | --- |
| `pairwise_accuracy` | 0.30 accuracy points | about 0.08 accuracy points (1.96 x 0.30 / 7.746 = 0.0759) | `MIN_LIFT_OVER_RANDOM = +0.10`, `MIN_LIFT_OVER_DSP = +0.10` |
| `top1_margin` | 1.00 rating point | about 0.25 rating points (1.96 x 1.00 / 7.746 = 0.2530) | `MIN_TOP1_MARGIN = +0.30` |

Every gate that applies at the minimum design is set at or above that detection floor. The
`topk_mean` gates are not evaluated at the minimum design: with five rated candidates
per query, `k` equals the rated-subset size, and the literal top-10 gate applies only
when every eligible query carries at least
`MIN_RATED_CANDIDATES_FOR_TOP10_GATE = 11` rated candidates. If that gate is ever
switched on, its own evidence design must be recomputed: if the top-k paired-difference standard
deviation were also 1.00 rating point,
`MIN_TOP_K_MEAN_LIFT_OVER_DSP = +0.15` is below the 0.25 rating-point floor implied by
60 queries (about 171 eligible queries would be needed, more than the 75 held-out kicks the pool
holds), so at the current budget that gate would be under-powered and could not be called
`supported` on noise-free evidence alone. The minimum design therefore reports top-k
means without gating on them.

### The four verdicts

* **supported** - the point estimate meets the minimum **and** the 95% interval excludes 0 in the
  favourable direction (for a lift, the whole interval is above 0).
* **not supported** - the 95% interval lies entirely below the minimum.
* **inconclusive** - anything else (the interval includes 0 without lying entirely below the
  minimum, or the point estimate is below the minimum while the interval reaches it).
* **insufficient** - an evidence minimum is unmet (pool floors, evaluator minimums, query or pair
  counts, pair coverage floor, agreement floor, latency sample floor, retention selection floor).

Global rule: the protocol **passes** only when every gate is `supported`; it
**fails** when any gate is `not supported`; otherwise it is insufficient evidence. A
gate that cannot be evaluated at all is insufficient, never a pass.

### What makes a result inconclusive or insufficient rather than negative

None of the conditions below is a `not supported` verdict; each is an
`inconclusive` or `insufficient` result, and each is reported by name:

1. The interval includes 0 - inconclusive.
2. `pair_coverage` below `MIN_PAIR_COVERAGE = 0.80` - inconclusive.
3. Fewer counted evaluators than `MIN_EVALUATORS = 5` - insufficient.
4. Fewer eligible queries than `MIN_HELDOUT_QUERIES = 60` - insufficient.
5. Fewer rated candidates than `RATED_CANDIDATES_PER_QUERY = 5` on a query -
   inconclusive for the comparison, with the shortfall reported per query.
6. Mean absolute deviation above `MAX_MEAN_ABSOLUTE_DEVIATION = 1.00` - inconclusive.
7. More than `MAX_RECOGNISED_RATE = 0.20` of a session's presentations flagged -
   the session is excluded from the primary metric and re-run; the comparison is inconclusive until
   the re-run lands, and insufficient when the replacement leaves fewer counted evaluators than the
   minimum.
8. No live Jev adapter (missing credentials, or double-only outcomes) - insufficient for every
   Jev-dependent gate (`jev-only` and the hybrid lift over DSP).
9. A missing dataset, split, analysis, ranking, prompt, model or seed value - insufficient.
10. A pool below `MIN_POOL_KICKS = 80` or
    `MIN_POOL_BASSES = 8` - insufficient, not fail.
11. Two gated metrics that disagree on the same comparison (one supported, one not supported on the
    same evidence) - inconclusive.
12. A same-pack or with/without-evaluator recomputation that flips a conclusion - inconclusive.

## Thresholds

Every value predeclared under Constraints appears below with the scale it is measured on, its gate
class and a one-sentence rationale. A **minimum (gate)** decides pass or fail; a **target** is
non-gating and reported for direction; a version, seed or rule is fixed and is not a gate.

| Name | Value | Scale | Gate class | Rationale |
| --- | --- | --- | --- | --- |
| `PROTOCOL_VERSION` | `tera-eval-protocol-v1` | version string | fixed (not a gate) | Identifies the frozen rule set every stored run claims; changing any anchor, rule, metric, seed or threshold needs a new value and a fresh held-out run. |
| `SPLIT_SEED` | `tera-eval-split-16-v1` | seed string | fixed (not a gate) | Makes the tuning/held-out split reproducible from the frozen pool. |
| `PAIR_SAMPLER_SEED` | `tera-eval-pairs-16-v1` | seed string | fixed (not a gate) | Makes the arm-independent pair draw reproducible from sample identities alone. |
| `ASSIGNMENT_SEED` | `tera-eval-assignment-16-v1` | seed string | fixed (not a gate) | Makes evaluator-to-pair assignment balanced and reproducible. |
| `ORDER_SEED` | `tera-eval-order-16-v1` | seed string | fixed (not a gate) | Makes the blinded presentation permutation reproducible so a resume continues the same order. |
| `RANDOM_ARM_SEED` | `tera-eval-random-16-v1` | seed string | fixed (not a gate) | Makes the random baseline auditable and repeatable. |
| `BOOTSTRAP_SEED` | `tera-eval-bootstrap-16-v1` | seed string | fixed (not a gate) | Makes the resampled confidence intervals reproducible. |
| `TUNING_FRACTION` | 0.25 | share of a role's unique samples | fixed (not a gate) | A quarter of each role may be tuned on, leaving three quarters untouched for the primary metric. |
| `MIN_POOL_KICKS` | 80 | pool kicks | minimum (gate) | Below 80 available kicks the pool cannot carry the 60-query minimum design with any reserve. |
| `MIN_POOL_BASSES` | 8 | pool basses/sub-basses | minimum (gate) | Below 8 basses a query cannot draw five distinct candidates with any room for coverage. |
| `MIN_EVALUATORS` | 5 | counted evaluators | minimum (gate) | Five producers is the fewest that gives every sampled pair its second rating within one affordable session. |
| `TARGET_EVALUATORS` | 8 | counted evaluators | target (non-gating) | Eight producers is the recruitment target that gives every pair a third rating at the target design. |
| `MIN_RATINGS_PER_PAIR` | 2 | valid ratings | minimum (gate) | Two evaluators per pair is the floor at which a pair's aggregate is not one person's opinion. |
| `TARGET_RATINGS_PER_PAIR` | 3 | valid ratings | target (non-gating) | A third rating makes the per-pair median robust to one outlier. |
| `MIN_HELDOUT_QUERIES` | 60 | held-out query kicks | minimum (gate) | 60 queries is the evidence size at which the stated minimum lifts are above the detection floor. |
| `TARGET_HELDOUT_QUERIES` | 75 | held-out query kicks | target (non-gating) | 75 queries uses the whole held-out kick partition and doubles the minimum design's evidence. |
| `RATED_CANDIDATES_PER_QUERY` | 5 | rated candidates per query | fixed (not a gate) | A fixed candidate-set size of five keeps every query comparable and every pair ratable at the minimum budget. |
| `MIN_HELDOUT_PAIRS` | 300 (60 x 5) | held-out pairs | minimum (gate) | The minimum design's pair count follows directly from 60 queries at five rated candidates each. |
| `MIN_TUNING_PAIRS` | 40 | tuning pairs | minimum (gate) | Only a tuning claim needs a floor, and 40 pairs is the smallest set that can compare two tuning choices without overfitting. |
| `MIN_PAIR_COVERAGE` | 0.80 | rated share of sampled pairs | minimum (gate) | Below 80% rated pairs the rated subset no longer represents the sampled candidate population. |
| `MIN_AGREEMENT_PAIRS` | 40 | pairs with at least two valid ratings | minimum (gate) | Disagreement measures need at least 40 multi-rated pairs before a rate is reportable. |
| `MAX_MEAN_ABSOLUTE_DEVIATION` | 1.00 | rating points (0-3 scale) | minimum (gate) | Above one rating point the evaluators disagree too much for the pair aggregate to carry a comparison. |
| `MAX_RECOGNISED_RATE` | 0.20 | flagged share of a session | minimum (gate) | A session where more than a fifth of presentations are recognised is no longer blind enough to count. |
| `MIN_SCORED_SHARE_PER_QUERY` | 0.50 | scored share of the rated subset | minimum (gate) | An arm that scores less than half the rated subset is not comparable on that query and is made ineligible. |
| `MIN_RATED_CANDIDATES_FOR_TOP10_GATE` | 11 | rated candidates per query | fixed (not a gate) | Eleven is the fewest rated candidates at which a top-10 mean is smaller than the subset and therefore discriminating. |
| `MIN_LATENCY_REQUESTS_PER_ARM` | 30 | latency measurements per arm | minimum (gate) | Thirty measurements is the fewest from which a per-arm p95 is stable, spread over at least 10 query kicks. |
| `BOOTSTRAP_RESAMPLES` | 10000 | resamples | fixed (not a gate) | Ten thousand resamples makes the 2.5th and 97.5th percentiles stable to the reported precision. |
| `MIN_PAIRWISE_ACCURACY` | 0.60 | accuracy points | minimum (gate) | Above chance by a margin a producer can feel, and the floor a hybrid pass must clear. |
| `TARGET_PAIRWISE_ACCURACY` | 0.68 | accuracy points | target (non-gating) | The product-quality goal for ordering held-out pairs correctly. |
| `MIN_LIFT_OVER_RANDOM` | +0.10 | accuracy points | minimum (gate) | A lift over random must exceed the about 0.08 accuracy-point detection floor of the minimum design. |
| `TARGET_LIFT_OVER_RANDOM` | +0.18 | accuracy points | target (non-gating) | The lift over random that would justify the recommendation pipeline on its own. |
| `MIN_LIFT_OVER_DSP` | +0.10 | accuracy points | minimum (gate) | Jev must add at least the detection floor over DSP rules alone, the plan's highest-risk assumption. |
| `TARGET_LIFT_OVER_DSP` | +0.15 | accuracy points | target (non-gating) | The lift over DSP that would justify carrying a remote judgment service. |
| `MIN_DSP_PAIRWISE_ACCURACY` | 0.55 | accuracy points | minimum (gate) | The DSP baseline must be above chance before a lift over it means anything. |
| `MIN_JEV_ONLY_PAIRWISE_ACCURACY` | 0.55 | accuracy points | minimum (gate) | Jev alone must also be above chance, so a hybrid win cannot come from one arm carrying noise. |
| `MIN_TOP1_MARGIN` | +0.30 | rating points (0-3 scale) | minimum (gate) | The top recommendation must beat the query's average by more than the about 0.25 rating-point detection floor. |
| `TARGET_TOP1_MARGIN` | +0.50 | rating points (0-3 scale) | target (non-gating) | A half-point margin puts the top recommendation between the good and excellent anchors on average. |
| `MIN_TOP_K_MEAN` | 2.00 | rating points (0-3 scale) | minimum (gate, literal top-10 only) | The top ten should average good-or-better; it gates only when every eligible query has at least 11 rated candidates. |
| `TARGET_TOP_K_MEAN` | 2.30 | rating points (0-3 scale) | target (non-gating) | A 2.30 top-ten mean is the product goal between good and excellent. |
| `MIN_TOP_K_MEAN_LIFT_OVER_RANDOM` | +0.30 | rating points (0-3 scale) | minimum (gate, literal top-10 only) | The top ten must beat random's top ten by a third of a rating point; it gates only under the 11-candidate rule. |
| `MIN_TOP_K_MEAN_LIFT_OVER_DSP` | +0.15 | rating points (0-3 scale) | minimum (gate, literal top-10 only) | The top ten must beat DSP's top ten; it gates only under the 11-candidate rule and its evidence budget must be recomputed before it is switched on. |
| `MAX_COLD_RECOMMENDATION_P95_MS` | 2000 | milliseconds, p95 | minimum (gate) | Two seconds is the longest a cold recommendation may take before it breaks the producer's flow. |
| `TARGET_COLD_RECOMMENDATION_P95_MS` | 1000 | milliseconds, p95 | target (non-gating) | One second cold is the comfort goal for a first recommendation. |
| `MAX_WARM_RECOMMENDATION_P95_MS` | 500 | milliseconds, p95 | minimum (gate) | Half a second warm is the longest a repeated request may take once the decision cache is hit. |
| `TARGET_WARM_RECOMMENDATION_P95_MS` | 250 | milliseconds, p95 | target (non-gating) | A quarter second warm is the goal that keeps browsing and audition feel immediate. |
| `MAX_AUDITION_START_P95_MS` | 250 | milliseconds, p95 | minimum (gate, Phase 1, #39) | Audition must start within a quarter second; defined here and measured in #39. |
| `RETENTION_WINDOW_DAYS` | 14 | days | fixed (not a gate) | Two weeks is long enough for a kept recommendation to prove itself and short enough to report in Phase 1. |
| `MIN_RETENTION_RATE` | 0.20 | retained share of completed selections | minimum (gate, Phase 1) | One in five completed selections retained is the floor for a recommendation loop worth continuing. |
| `TARGET_RETENTION_RATE` | 0.30 | retained share of completed selections | target (non-gating, Phase 1) | Three in ten retained is the Phase 1 goal for a recommendation loop producers keep using. |
| `MIN_RETENTION_SELECTIONS` | 30 | completed selections | minimum (gate, Phase 1) | Below 30 completed selections a retention rate is not reportable, so a small sample cannot be read as success or failure. |

Consistency rule: a hybrid `pass` requires the accuracy gate, the lift-over-DSP gate
and the top-1 margin gate to be `supported` together. A single
`not supported` gate among them makes the hybrid result fail; any other combination
that is not all-supported is insufficient evidence.

## Latency measurement

* **cold** - a new process, the analysed features present, and no decision cached for that request.
* **warm** - the identical request repeated with the decision cache hit.
* Measurement points: request receipt, then the boundaries of feature load, filter, retrieval, Jev
  calls and ranking, then the ranked recommendation set is ready. Per-stage timings and the
  end-to-end time are recorded for every request; the reported number is the p95 per arm and, for
  stages, the p95 per stage.
* `MIN_LATENCY_REQUESTS_PER_ARM = 30` measurements per arm, spread over at least 10
  query kicks, at both cold and warm state.
* Recorded with every latency result: hardware, operating system, the protocol, dataset, analysis,
  ranking, prompt, model and adapter versions, and the process state (cold start or warm, cache
  state, concurrent load).
* Jev time is included whenever the arm uses Jev.
* A request that cannot start is not a latency sample, and no measurement is invented for it.
* An unavailable or timed-out Jev service makes that arm's latency `insufficient`,
  never passing.
* `MAX_AUDITION_START_P95_MS = 250` is defined here and measured in #39; the
  recommendation p95 thresholds (`MAX_COLD_RECOMMENDATION_P95_MS = 2000`,
  `MAX_WARM_RECOMMENDATION_P95_MS = 500`) are measured in the #19 comparison run.

## Retention protocol (Phase 1)

* `RETENTION_WINDOW_DAYS = 14`: a selection is retained when the selected sample is
  still present in a palette or project at the end of the 14th day after the selection.
* Selection event: a producer keeps a recommended sample. Repeated plays, repeated drags and
  re-openings are not new selections; a selection is de-duplicated by (user, sample content
  identity, window start), so one user and one sample count once in one window.
* Denominator: selections whose window has completed at report time (made at least 14 days before
  the report cutoff). A selection whose window has not completed is **censored**: it is reported
  separately as a count and is never counted as retained and never enters the denominator.
* `MIN_RETENTION_SELECTIONS = 30` completed selections before a rate is reportable;
  below it the retention result is insufficient evidence, not a failure.
* `MIN_RETENTION_RATE = 0.20` minimum and
  `TARGET_RETENTION_RATE = 0.30` target, measured as the retained share of the
  completed selections in the denominator.
* #36 owns the retention report and the observation-window implementation; #39 owns the
  first-milestone evidence. Phase 0's pair comparison makes no retention claim, and no retention
  number from an incomplete window may be reported as a result.

## Evidence #17 must record

The rating utility records two record types. The export is machine-readable with a documented
schema; the schema is versioned with the `protocol_version` it was written under.

Session record fields: `session_id`, anonymous `evaluator_id`,
`protocol_version`, `dataset_version`, split-manifest digest,
`order_seed`, `playback_gain_db`, monitoring description,
`started_at`, `finished_at`.

Rating record fields: `pair_id`, `kick_sample_id`,
`bass_sample_id`, `presentation_index`, `rating` (one of
the four labels or null), `skip`, `skip_reason` (one of the fixed set
when `skip` is true), `recognised`, `playback_completed`,
`responded_at` with its local offset, and `correction_of` naming the
superseded record when a later presentation of the same pair replaces an earlier answer.

Rules: paths, file names, pack names, arm identity, scores and ranks are never recorded or
exported; a rating with `playback_completed = false` is invalid; and an interrupted
session resumes without losing ratings and without duplicating a completed answer.

## Evidence #19 must report

* Versions and digests: `protocol_version`; dataset version and identity digest;
  split-manifest version and digest; analysis version; `RANKING_VERSION` and the
  weight-table id; the Jev `prompt_version` and every observed
  `model_version`; `ADAPTER_VERSION`; and the hybrid ranking version and
  weight-table id when the hybrid arm runs.
* The seeds used: `SPLIT_SEED`, `PAIR_SAMPLER_SEED`,
  `ASSIGNMENT_SEED`, `ORDER_SEED`, `RANDOM_ARM_SEED` and
  `BOOTSTRAP_SEED`.
* The hardware and the reproduction commands.
* Eligibility accounting: pool counts per role, split counts, sampled and rated pair counts,
  per-arm eligible query counts and their intersection, unscored shares, and
  `pair_coverage`.
* A per-arm metric table giving each metric's value, its 95% interval, the minimum or target it is
  compared with, and its verdict.
* The agreement table (mean absolute deviation, exact-agreement share, per-evaluator means).
* The latency table (cold and warm, per arm, per stage).
* An explicit deviations log: every departure from this protocol, with its reason and its effect.
* The no-held-out-tuning attestation: a statement that no tuning run read a held-out rating, with
  the split-manifest digest it refers to.
* The sanitisation rule: aggregate counts and distributions only. No sample id, path, pack name or
  per-pair audio reference appears in the committed report; detailed records stay under
  `.local-evaluation/`.

#19 may not lower a threshold, change a metric or re-sample after seeing a result. If the evidence
does not meet a gate, that is the finding.

## Out of scope

* Rating utility, playback and resume: [#17](https://github.com/gmphto/tera/issues/17).
* Recruiting and running a producer session: [#18](https://github.com/gmphto/tera/issues/18).
* Running the comparison and computing the report: [#19](https://github.com/gmphto/tera/issues/19).
* Proceed/revise/stop decision: [#20](https://github.com/gmphto/tera/issues/20).
* Hybrid ranking implementation and its versioned weights: [#15](https://github.com/gmphto/tera/issues/15).
* Product retention report and the observation-window implementation: [#36](https://github.com/gmphto/tera/issues/36).
* Product latency tuning and optimisation: [#37](https://github.com/gmphto/tera/issues/37).
* First producer milestone evidence (audition latency, retention): [#39](https://github.com/gmphto/tera/issues/39).
* Further pool collection or recovery: [#10](https://github.com/gmphto/tera/issues/10), [#61](https://github.com/gmphto/tera/issues/61) (both closed; a new issue is needed only if the pool must grow).
* Personalisation evaluation: [#58](https://github.com/gmphto/tera/issues/58), [#59](https://github.com/gmphto/tera/issues/59).
* Any code, test, fixture, dataset or dependency change.
