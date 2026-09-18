# Instrument Palette Intelligence backlog

Source: `_docs/plan.md`. Tasks are ordered for delivery; each description states its scope, inputs, and completion evidence so it can be handed off without reading other tasks. Implementation dependencies still apply; use small fixtures or contract doubles where an integration is not ready.

Complete the feasibility decision before investing in the desktop UI. Later phases are conditional on proving the standalone kick-to-bass workflow; producer evaluation tasks cover one bounded collection or review session and must report missing evidence rather than assume success.

## 1. Set up an empty project with a passing test
Goal: Establish a minimal project that can run one passing test.
Description: Create an empty Python backend project with a test runner and one smoke test that verifies the package imports, without adding product behavior. Document the setup and test commands, and confirm they work from a clean checkout.

## 2. Define the kick-to-bass data contracts
Goal: Give analysis and ranking a shared, versioned input and output format.
Description: Define contracts for kick and bass samples, optional song context, measured features, per-dimension judgments, and ranked results. Specify units, missing-value behavior, confidence ranges, and separate similarity from compatibility; provide valid and invalid fixtures that demonstrate validation.

## 3. Check DSP feasibility on representative WAV files
Goal: Choose a working audio analysis dependency set before building extractors.
Description: Evaluate Essentia as the primary DSP candidate alongside SoundFile, NumPy, and SciPy on a small mono/stereo WAV fixture set on the development platform. Record installation, distribution constraints, supported formats, and any specific capability requiring an alternative, with a reproducible result for each fixture.

## 4. Implement WAV loading and validation
Goal: Read usable audio consistently and reject unusable files clearly.
Description: Build a local WAV reader that returns samples, channel count, sample rate, and duration using the agreed analysis dependencies. Cover mono, stereo, different sample rates, silence, empty audio, and corrupt files with explicit handling, preserving the original audio files.

## 5. Extract loudness measurements
Goal: Measure sample loudness without relying on model judgment.
Description: Add RMS, peak, crest factor, and LUFS where the clip duration supports a meaningful measurement to the versioned audio features. Verify expected values using controlled signals and represent unsupported or silent cases explicitly.

## 6. Extract spectral measurements
Goal: Describe the frequency distribution of each kick and bass.
Description: Calculate spectral centroid, roll-off, and energy in sub, bass, low-mid, mid, high-mid, and high bands from decoded audio. Document band boundaries and normalization, and verify band placement with synthetic tones at several sample rates.

## 7. Extract transient and envelope measurements
Goal: Describe how sharply a sample starts and how its energy decays.
Description: Calculate transient strength and position plus attack and decay durations using a documented envelope method. Check impulses, slow attacks, long tails, and silence against expected behavior, exposing uncertain measurements instead of inventing values.

## 8. Extract pitch and reliable key estimates
Goal: Provide tonal facts with an explicit reliability signal.
Description: Estimate fundamental frequency and musical key where the audio supports them, preserving unknown results for noisy or ambiguous kicks and basses. Verify known tones and ambiguous examples, and define the reliability threshold used by tonal filtering and scoring.

## 9. Build a resumable batch analysis command
Goal: Analyze a folder of WAV files once per content and analysis version.
Description: Combine the audio reader and feature extractors into a command that writes a versioned feature manifest with content fingerprints, per-file status, and errors. Show progress, reuse unchanged results, and demonstrate recovery after interruption without restarting completed analyses.

## 10. Prepare the feasibility sample manifest
Goal: Establish a reproducible evaluation pool of approximately 100 kicks and 100 basses.
Description: Create an importable manifest with sample IDs, local paths, role labels, and provenance using available samples the evaluators are permitted to use. Validate readability, role counts, and duplicate content, and explicitly report any collection shortfall without treating synthetic fixtures as producer evaluation data.

## 11. Implement deterministic candidate filters
Goal: Remove objectively unsuitable bass candidates before expensive scoring.
Description: Given one kick, optional song context, and analyzed sample records, retain bass-role candidates and apply documented DSP suitability rules. Return an exclusion reason for each filtered candidate and verify that unknown pitch or key alone does not cause an unsupported rejection.

## 12. Implement the DSP-only compatibility baseline
Goal: Rank kick-to-bass pairs using reproducible measured rules.
Description: Map frequency overlap, transient interaction, and reliable tonal relationships to named compatibility dimensions using analyzed sample fixtures. Produce a deterministic ranking with versioned weights, missing-feature behavior, stable ties, and concrete reasons that can be evaluated independently of Jev.

## 13. Define narrow Jev questions and response validation
Goal: Specify typed musical judgments that do not ask Jev to measure audio facts.
Description: Define questions and validated outputs for frequency, transient, tonal, rhythmic, texture, and arrangement fit using supplied DSP facts and optional song context. Specify score labels, probabilities, confidence, model and prompt versions, and behavior when context is insufficient; include malformed-response fixtures.

## 14. Build the bounded Jev scoring adapter
Goal: Obtain validated compatibility judgments for a limited candidate batch.
Description: Connect the Jev decision contract to the available TypeSafe Jev interface, sending only necessary feature and context data while keeping audio and local paths on the device. Add bounded timeouts and retries, cancellation, and explicit unavailable or invalid-result handling, with contract doubles and one real integration check when credentials are available.

## 15. Implement hybrid ranking and uncertainty handling
Goal: Combine DSP and Jev judgments under product-owned rules.
Description: Given measured baseline results and validated Jev decisions, apply documented, versioned dimension weights and deterministic tie-breaking to produce ranked bass recommendations. Keep confidence separate from compatibility, explain which evidence affected the result, and return alternatives or an explicit DSP-only fallback for uncertain or unavailable decisions.

## 16. Define the blind evaluation protocol
Goal: Make quality and latency decisions before inspecting experimental results.
Description: Write a protocol for poor, acceptable, good, and excellent pair ratings, blinded presentation, and reproducible training/tuning and held-out splits that avoid sample leakage. Set explicit minimum ranking lift, acceptable recommendation latency, retention targets, and evidence requirements for comparing random, DSP-only, Jev-only, and hybrid results.

## 17. Create a minimal pair-rating workflow
Goal: Collect producer labels without building the full product UI.
Description: Build a small local evaluation utility that plays a kick and bass pair, accepts the four rating labels, and saves pair IDs, anonymous evaluator ID, and rating. Randomize presentation reproducibly, conceal the ranking method, and support resuming a partially completed rating session.

## 18. Run one producer labeling session
Goal: Produce a validated batch of human compatibility ratings.
Description: Use the local pair-rating workflow and the agreed blind protocol to collect one scheduled session of ratings from the feasibility sample pool. Check missing and duplicate labels, preserve evaluator disagreement, and report coverage and remaining recruitment needs without fabricating uncollected ratings.

## 19. Build the ranking comparison report
Goal: Compare random, DSP-only, Jev-only, and hybrid recommendations reproducibly.
Description: Run all four systems on the same held-out labeled data, recording top-10 quality, uncertainty, coverage, and cold and warm latency against the agreed thresholds. Keep Jev-only scores free of DSP compatibility-rule contributions while allowing measured facts as inputs, and save dataset and model versions with the report.

## 20. Record the feasibility decision
Goal: Decide whether the evidence supports Jev and further product work.
Description: Review the comparison report against the predefined quality and latency thresholds and document a proceed, revise, or stop decision with supporting measurements. If labels or integrations are insufficient, name the exact next experiment; if Jev adds no meaningful lift, explicitly decide whether to pursue a DSP-only scope instead of claiming the hybrid milestone passed.

## 21. Add SQLite sample and analysis storage
Goal: Persist the local library and versioned analysis results.
Description: Create migrations and repositories for samples, sample features, tags, pack identity, and analysis versions, keeping original paths local. Verify round-trip storage, uniqueness, transaction rollback, and preservation of existing records across an upgrade using a temporary database.

## 22. Add incremental folder scanning
Goal: Keep the sample index current without repeating unchanged work.
Description: Scan a selected local folder for supported WAV files and reconcile additions, edits, duplicate content, moved files, and missing files with SQLite records. Queue analysis only where content or analysis version changed, and return an import summary that makes inaccessible files recoverable.

## 23. Add background analysis jobs
Goal: Import larger libraries without blocking browsing or losing progress.
Description: Run the existing batch analysis through a bounded local job queue backed by persisted status, updating SQLite features as files complete. Support cancellation, restart, and per-file retry, and verify that one corrupt sample does not abort the import.

## 24. Add project and palette persistence
Goal: Save a producer's current kick, selected bass, and song context locally.
Description: Create migrations and repository operations for projects, palettes, and palette items with optional tempo, key, and genre. Verify reopening a saved palette, replacing its kick or bass, and handling a library sample whose file has become unavailable.

## 25. Add normalized feature retrieval
Goal: Reduce eligible basses to a bounded shortlist before Jev scoring.
Description: Build a versioned similarity representation from stored audio features and retrieve at most 50–100 candidates after role and DSP filtering. Define missing-feature and normalization behavior, keep retrieval similarity separate from compatibility, and measure candidate recall against the feasibility evaluation set.

## 26. Add versioned decision caching
Goal: Reuse compatible decisions and invalidate stale results correctly.
Description: Store compatibility decisions and decision-model versions using keys covering palette hash, candidate identity and content, analysis version, model and prompt versions, and ranking version. Verify repeat-request hits and invalidation for changed audio, palette context, questions, or weights without discarding unrelated cached entries.

## 27. Expose local library and import APIs
Goal: Let a desktop client browse samples and monitor imports.
Description: Add a local service interface for starting folder imports, querying analysis status, searching and paging samples by role, and reading feature details. Specify validated request and response contracts and a loopback access policy, with integration checks against a temporary library.

## 28. Expose the recommendation API
Goal: Return explainable bass recommendations for a selected kick.
Description: Connect stored palette context, filters, similarity retrieval, cached Jev scoring, and deterministic ranking behind one local recommendation operation. Return score, confidence, measured facts, judgments, warnings, and alternatives; verify cancellation, empty candidate sets, stale kick selection, and unavailable Jev behavior.

## 29. Persist recommendation outcomes
Goal: Record which recommendations producers audition, select, reject, and later remove.
Description: Add outcome storage and local API operations linked to project, palette, recommendation run, candidate, and ranking version. Preserve event order and make retries idempotent so later retention analysis can distinguish rejection from removal after selection.

## 30. Create the minimal desktop shell
Goal: Launch a Tauri, React, and TypeScript client connected to the local service.
Description: Set up Vite, Redux Toolkit for workflow state, and RTK Query for the local API, with a minimal window showing service status. Define development startup and shutdown behavior and verify a visible recoverable state when the Python service is unavailable.

## 31. Add folder import and library browsing UI
Goal: Let a producer import and find kicks and basses.
Description: Build a folder picker, import progress display, and searchable role-filtered library against the local library APIs. Cover empty, loading, partial-error, and missing-file states while keeping browsing responsive during analysis.

## 32. Add kick selection and palette controls
Goal: Let a producer establish the context for bass recommendations.
Description: Display the saved palette, allow one kick to be selected or replaced, and edit optional tempo, key, and genre through palette persistence. Restore context on reopen and ensure an old recommendation response cannot overwrite results for a newly selected kick.

## 33. Add ranked recommendation cards
Goal: Present a useful shortlist with score, confidence, and alternatives.
Description: Render the recommendation API's ordered bass results with clear loading, empty, degraded, and error states plus select and reject actions. Show compatibility and confidence distinctly, surface low-confidence alternatives, and persist selected basses and outcome events.

## 34. Add fast audio audition controls
Goal: Hear a candidate alone or alongside the selected kick with minimal friction.
Description: Implement local playback, stop, candidate switching, keyboard controls, and an explicit level-matched comparison option without modifying source audio. Record audition events and check rapid switching, missing files, stereo samples, and cleanup when leaving the screen.

## 35. Add the compatibility explanation panel
Goal: Explain each recommendation using separate measurements and judgments.
Description: Show the selected candidate's DSP facts, Jev judgments when present, confidence, and specific frequency, transient, tonal, or arrangement warnings from the API. Handle absent features and DSP-only fallback accurately, and verify that displayed reasons match the returned evidence.

## 36. Add selection removal and retention reporting
Goal: Measure whether recommended basses remain in producers' palettes.
Description: Wire palette removal and replacement to outcome tracking and add a local report of audition, selection, rejection, and retained-selection rates by ranking version. Define the retention observation window and handle repeated auditions and reopened projects without double-counting selections.

## 37. Measure and tune MVP browsing latency
Goal: Keep recommendation and audition delays within the feasibility thresholds.
Description: Benchmark import, cold and warm recommendations, and audition startup on a documented 500–2,000-sample library, reporting per-stage timings. Fix the largest measured bottleneck that fits this session and rerun the same benchmark, recording any remaining threshold failures.

## 38. Package the standalone MVP for the development platform
Goal: Install and launch the desktop workflow without a development environment.
Description: Package the Tauri client and Python service with the chosen DSP runtime for the initial platform, using an appropriate local writable data directory. Verify installation, service lifecycle, database persistence, restart, and a recoverable missing-dependency error on a clean test environment.

## 39. Validate the first producer milestone
Goal: Establish whether the standalone kick-to-bass workflow meets the product plan.
Description: Run one bounded producer session covering folder import, kick selection, ranked basses, auditioning, explanations, selection, and recorded outcomes. Compare the blind top-10 and latency evidence with the agreed thresholds, documenting pass, fail, or insufficient evidence for every milestone criterion before enabling later phases.

## 40. Define drum-role compatibility policies
Goal: Make snare, clap, hat, and percussion scoring explicitly role-specific.
Description: Specify bounded policies and labeled example fixtures for snare, clap, closed hat, open hat, and percussion against an existing kick-and-bass palette. Define useful dimensions, conflicts, and missing-context behavior for each role without assuming the kick-to-bass weights apply.

## 41. Add snare and clap recommendations
Goal: Recommend one backbeat role against a saved palette.
Description: Extend role filtering, ranking, and the existing role picker to support snare and clap using their defined policies and a small fixture library. Verify role-correct candidates, concrete conflict reasons, and preservation of existing kick-to-bass behavior.

## 42. Add hat and percussion recommendations
Goal: Recommend complementary high-frequency drum parts.
Description: Extend the existing recommendation path and role picker for closed hats, open hats, and percussion using their documented policies. Check redundancy and transient conflicts against a saved drum palette and validate one representative example per added role.

## 43. Add full-palette aggregation
Goal: Score the effect of a candidate across all current palette items.
Description: Aggregate role-specific candidate-to-item judgments into an explicit palette-level compatibility result with versioned conflict penalties and redundancy rules. Verify that adding or removing a palette item changes the ranking and cache key predictably, and expose the contributing conflicts in the existing explanation panel.

## 44. Add stereo width analysis
Goal: Supply measured spatial information for broader palette judgments.
Description: Add a documented stereo-width measurement to versioned features and the analysis pipeline, with explicit mono behavior. Validate mono, identical stereo, and decorrelated stereo fixtures and ensure older stored analyses are scheduled for the new feature version.

## 45. Validate harmonic analysis on melodic samples
Goal: Establish reliable tonal evidence before expanding melodic recommendations.
Description: Evaluate the existing pitch and key analysis on a bounded labeled set of chords, leads, pads, arps, vocals, and textures. Improve one demonstrated failure mode or explicitly mark it unsupported, documenting reliability and unknown-value behavior for downstream policies.

## 46. Add tempo and rhythmic analysis
Goal: Describe tempo-aware compatibility where the audio contains reliable timing information.
Description: Extract tempo and rhythmic descriptors for suitable loops while leaving one-shots and ambiguous material explicitly unknown. Verify labeled loop and one-shot fixtures and define how song tempo and detected timing contribute to a versioned rhythmic-fit policy.

## 47. Add chord and pad recommendations
Goal: Recommend harmonic support that fits the current palette.
Description: Add chord and pad policies and role selection using reliable harmonic evidence, stereo width, and full-palette conflicts. Validate a bounded set of compatible, clashing, redundant, and unknown-key examples through the existing API and explanation UI.

## 48. Add lead and arp recommendations
Goal: Recommend melodic foreground parts with tonal and rhythmic context.
Description: Add lead and arp policies to the shared ranking path, applying tempo-aware evidence only when reliable. Check tonal clashes, rhythmic conflicts, arrangement redundancy, and absent context with role-specific fixtures and visible reasons.

## 49. Add vocal and texture recommendations
Goal: Recommend vocals and textures without forcing unsupported tonal assumptions.
Description: Add vocal and texture policies and role selection using reliable features and full-palette judgments. Verify tonal vocals, unpitched textures, and competing foreground parts, ensuring uncertainty appears as alternatives in the existing UI.

## 50. Add pack membership and profiles
Goal: Describe a sample pack through its analyzed contents.
Description: Extend stored pack identity with editable name, optional vendor, explicit sample membership, and a versioned profile aggregated from roles and audio features. Recompute affected profiles when membership or analysis changes and verify mixed-role and sparsely analyzed packs.

## 51. Rank packs against a project
Goal: Find packs containing useful additions to the current palette.
Description: Aggregate existing sample compatibility results into transparent pack scores that account for role coverage and prevent large packs from winning on size alone. Expose ranked packs and representative samples in a minimal existing-client view, keeping pack similarity separate from project compatibility.

## 52. Detect pack redundancy and complementary pairs
Goal: Explain when two packs overlap or fill each other's gaps.
Description: Compare two versioned pack profiles for role and feature overlap and rank a bounded set of pack pairs for a saved project. Show supporting examples and verify redundant, complementary, and insufficient-data fixtures without requiring ownership or purchase recommendations.

## 53. Add WAV drag-and-drop into FL Studio
Goal: Transfer an auditioned recommendation into an FL Studio session.
Description: Implement native file dragging from a recommendation using the existing local WAV path after the standalone milestone is validated. Check successful import into FL Studio on the supported platform plus missing-file and canceled-drag behavior, without altering the source sample.

## 54. Specify and probe the VST3 context bridge
Goal: Establish which FL Studio context can reach the local service reliably.
Description: Define a small versioned protocol for available tempo, transport, and project context, with explicit behavior for information the host does not expose. Build a bounded feasibility probe and record host behavior and connection requirements before committing to a production bridge.

## 55. Build the minimal VST3 bridge lifecycle
Goal: Load a thin plugin and connect it to the local service safely.
Description: Implement plugin startup, shutdown, protocol negotiation, and reconnection using the agreed bridge protocol while keeping analysis, indexing, storage, and Jev outside the plugin. Verify that service failure does not block the audio processing thread or prevent the host from closing the plugin.

## 56. Send live host context to the palette
Goal: Refresh recommendation context from supported FL Studio host events.
Description: Send the tempo and transport fields proven available by the bridge probe, then reconcile them with the local palette using an explicit manual-override policy. Test stale messages, rapid updates, disconnection, and project changes without triggering unnecessary ranking calls.

## 57. Validate the FL Studio bridge in a real session
Goal: Check host reliability and context latency before releasing integration.
Description: Run a bounded session covering playback, tempo changes, plugin reopen, multiple instances, and local service restart on a recorded FL Studio version. Capture audio interruptions, stale context, and update latency against predefined acceptance limits, then record release blockers with reproducible steps.

## 58. Prepare preference data from retained selections
Goal: Build a local dataset suitable for evaluating individual taste.
Description: Transform recorded auditions, selections, rejections, and later removals into versioned preference examples using the established retention window. Preserve chronology for held-out evaluation, distinguish ignored from rejected samples, and report when a producer has too little evidence for personalization.

## 59. Implement and evaluate a bounded personal reranker
Goal: Test whether individual preferences improve generic compatibility rankings.
Description: Add one simple reranking method over the existing shortlist using locally stored preference examples, preserving the original generic score. Compare it with generic ranking on chronologically held-out outcomes and fall back to generic results when data or measured lift is insufficient.

## 60. Expose optional personalization controls
Goal: Let producers understand and control how taste affects recommendations.
Description: Add an opt-in toggle and separate generic compatibility and personalized ranking indicators to the existing recommendation view. Provide a way to reset learned preferences, and verify that disabling personalization restores the generic ordering without changing the underlying sample library.
