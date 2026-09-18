# Deterministic candidate admission

`backend.palette.compatibility.filter_candidates(kick, candidates, *, policy,
availability, context=None)` is pure local policy. It accepts validated schema-1
`Sample` objects, a sequence of candidate Samples, optional `SongContext`, an
explicit `FilterPolicy` and caller-provided availability keyed by sample ID.
It never opens paths, refreshes availability, calls Jev, changes DSP features,
measures similarity or calculates compatibility/ranking scores.

```python
from backend.palette.compatibility import Availability, FilterPolicy, filter_candidates

result = filter_candidates(
    selected_kick, [candidate_a, candidate_b],
    policy=FilterPolicy(),
    availability={
        selected_kick.sample_id: Availability.AVAILABLE,
        candidate_a.sample_id: Availability.AVAILABLE,
        candidate_b.sample_id: Availability.MISSING,
    },
    context=song_context,  # optional validated SongContext
)
# An otherwise usable candidate_a is eligible, including with unknown tempo/key.
# candidate_b has file_missing; omitted availability instead means unverified.
```

`FilterPolicy(tempo_lock=False, exact_key_lock=False)` is the default. Enable
either boolean explicitly to request its admission constraint. Policy version
`candidate-admission-v1` identifies defaults, thresholds, validation and reason
semantics; bump it when any of these change. Results retain configuration and
version. These constants are conservative policy choices, not calibrated musical
truth or proof that excluded keys/tempos are musically incompatible.

## Input errors and usable audio

The selected kick must have kick role, positive frames, a known positive sample
peak, consistent known RMS/peak, and explicitly available file evidence. A missing,
unreadable or unverified kick is an input error, even for an empty candidate list.
Malformed contracts are revalidated at entry. Duplicate candidate IDs are errors;
a single candidate sharing the kick ID is instead a normal `selected_kick`
exclusion. Candidate analysis-version strings may differ.

`FilterInputError` inherits ValueError and exposes a stable `.code` plus an
explanatory message. Codes are `invalid_kick`, `invalid_candidates`,
`duplicate_candidate_id`, `invalid_context`, `invalid_policy`, and
`invalid_availability`. Availability must be a mapping containing only supplied
kick/candidate IDs and actual `Availability` enum values: `AVAILABLE`, `MISSING`,
`UNREADABLE`, `UNKNOWN`. Unknown/unverified evidence is never implicitly available.
Unrelated IDs and invalid state types are inconsistent input. No freshness is
inferred from any supplied state; persistent availability reconciliation is #22.

Usable candidate analysis requires positive frames and known positive linear
peak. Finite overrange peak is allowed. Known RMS above peak is contradictory
unless `math.isclose(rms, peak, rel_tol=1e-12, abs_tol=0)` accounts for floating
roundoff. Positive RMS with zero peak always contradicts the core evidence,
regardless of magnitude. The relative-only tolerance prevents a fixed absolute
epsilon from hiding contradictions in quiet signals. Missing RMS and unknown
crest factor, LUFS, bands, envelope, F0 or key do not fail usability. No preference
threshold is imposed on those features or genre.

## Optional gates

Tempo locking requires known candidate and song tempo, both with confidence
at least 0.80. It accepts `abs(candidate-song)/song <= 0.05`, implemented as
`abs(candidate-song) <= 0.05*song` to avoid overflow in a diagnostic ratio.
The 5% bound is inclusive; no confidence epsilon, half/double-tempo matching or
tempo inference is performed. Supporting facts retain native BPM/confidence and
the configured relative tolerance rather than a possibly nonfinite ratio.
Missing/low-confidence evidence or absent context skips the gate. Current measured
tempo is `not_implemented`, so current extractor output cannot trigger it.

Exact-key locking requires known candidate and song keys with both confidences
at least 0.80. Tonic and mode must both match. Unknown/low-confidence keys skip
the gate. Kick F0/key and candidate F0 never replace song context or become key
evidence. No context or disabled locks leaves usable uncertain candidates eligible.

## Result and reason schemas

Frozen `FilterResult` contains `policy_version`, `policy`, `kick_id`,
`kick_analysis_version`, sorted `eligible_ids`, sorted `excluded` records, and
sorted `(sample_id, analysis_version)` pairs for every candidate. It preserves
analysis provenance without requiring an arbitrary shared version string.

Each `ExcludedCandidate` has sample ID, its analysis version, and a tuple of
`ExclusionReason(code, facts)`. Facts are immutable sorted key/value pairs of
necessary measured/context evidence; no local paths are returned. All applicable
reasons are reported in this fixed order:

| Order | Code | Supporting facts |
| --- | --- | --- |
| 1 | `selected_kick` or `wrong_role` | Selected ID or actual role; selected-kick takes role precedence |
| 2 | `file_missing`, `file_unreadable`, or `availability_unknown` | Supplied/implicit availability state |
| 3 | `empty_audio` | Zero frame count |
| 4 | `silent_audio` | Zero peak |
| 5 | `unusable_analysis` | Unknown peak and its unavailable reason |
| 6 | `inconsistent_analysis` | Known RMS, peak and relative roundoff allowance |
| 7 | `tempo_mismatch` | Candidate/song BPM, confidences and tolerance |
| 8 | `key_mismatch` | Candidate/song tonic, mode and confidences |

For example, an unavailable zero-frame candidate with zero peak/positive RMS can
carry availability, empty, silent and inconsistent reasons together. A known
positive-peak one-shot with unknown tempo, key and short-envelope features can
remain eligible. Every candidate appears exactly once as eligible or excluded;
candidate IDs and provenance records sort lexically, independently of input order
and earlier calls. Inputs remain unchanged and result records are immutable.

## Validation

Synthetic feature fixtures cover all role/availability/core rules, both lock
defaults, absent and uncertain context, confidence immediately below/at/above
0.80, tempo immediately below/at/above both 5% boundaries, mode/tonic distinctions,
input errors, empty/all-excluded sets, multiple reasons, ordering/state isolation,
overrange and extreme finite values, version provenance and unchanged inputs.
No actual evaluation-library material is required or represented by these tests.

```text
uv run pytest tests/test_candidate_filters.py --basetemp .pytest_cache/filter-focused
uv run pytest --basetemp .pytest_cache/filter-final
```

Focused validation: 59 passed. This policy does not resolve the separately recorded
evaluation-pool shortfall or establish musical ranking quality.
