# Instrument Palette Intelligence — Product Plan

## 1. Product goal

Build a local-first tool that helps music producers answer:

> Given the sounds already in this song, what should I audition next?

The product ranks samples and instrument packs by how well they complement the current musical palette. It is a decision-support tool, not an AI composer.

## 2. Product boundary

### Input

- Current palette: selected kick, bass, snare, hats, melodic parts, and other sounds
- Candidate samples from the producer's library
- DSP measurements extracted from those sounds
- Optional song context: tempo, key, genre, and intended role

### Processing

1. DSP measures objective audio properties.
2. Deterministic filters remove unsuitable candidates.
3. Similarity search produces a small candidate set.
4. TypeSafe Jev makes narrow, typed compatibility judgments.
5. Product-owned rules combine the judgments into a final ranking.

### Output

- Ranked samples to audition next
- Compatibility score and confidence
- Specific strengths and conflicts
- Alternative candidates when confidence is low

## 3. Core design rules

### DSP owns facts

Calculate these properties directly:

- Tempo and key
- Fundamental frequency
- LUFS, RMS, peak, and crest factor
- Spectral centroid and roll-off
- Energy by frequency band
- Transient strength and position
- Attack and decay
- Stereo width

Never ask Jev to calculate facts that DSP can measure.

### Jev owns fuzzy judgment

Use Jev for narrow decisions such as:

- Does this bass leave enough low-frequency space for the kick?
- Is the candidate tonally compatible with the song?
- Is its texture redundant with the existing palette?
- Does it fill a useful arrangement role?
- Does adding it improve the palette as a whole?

### Product code owns ranking

Jev returns typed scores and probabilities. Deterministic application code applies weights, confidence thresholds, role rules, and tie-breaking. Do not ask Jev for one opaque overall answer.

### Similarity is not compatibility

Store both values separately. Two basses may be highly similar but poorly compatible because they compete for the same role. A kick and bass may be dissimilar but highly compatible.

## 4. MVP

Prove one workflow first:

> Select a kick, then rank bass samples that complement it.

### MVP scope

- Import 500–2,000 kick and bass samples
- Analyse every sample once
- Select one kick
- Filter and rank bass candidates
- Audition the top results
- Show score, confidence, and concrete reasons
- Record whether each recommendation was auditioned, selected, rejected, or removed later

### Required audio features

- Fundamental frequency
- Sub, bass, low-mid, mid, high-mid, and high-band energy
- Attack and decay
- Transient strength
- Loudness
- Spectral centroid
- Detected key when reliable

### MVP success criteria

- Top-10 recommendations beat random selection in blind human evaluation
- Recommendations appear quickly enough to preserve browsing flow
- Producers keep recommended samples at a useful rate
- Jev materially improves results over DSP rules alone

## 5. Technical experiment before full UI

Build a labelled evaluation set using approximately 100 kicks and 100 basses.

1. Generate or sample kick–bass pairs.
2. Have producers rate pairs as poor, acceptable, good, or excellent.
3. Compare three systems:
   - DSP rules only
   - Jev only
   - DSP plus Jev
4. Measure ranking quality against human ratings.
5. Continue with Jev only if it produces a meaningful lift.

This experiment validates the product's highest-risk assumption before DAW integration or a polished interface is built.

## 6. Ranking pipeline

Do not send an entire library to Jev.

```text
50,000 samples
    ↓
Role and DSP filters
    ↓
Plausible candidates
    ↓
Vector/similarity retrieval
    ↓
Top 50–100 candidates
    ↓
Jev typed scoring
    ↓
Deterministic weighted ranking
    ↓
Top 5–20 recommendations
```

Example product-owned weighting for kick-to-bass ranking:

```ts
const compatibility =
  frequencyFit * 0.3 +
  transientFit * 0.2 +
  tonalFit * 0.2 +
  textureFit * 0.1 +
  arrangementFit * 0.2;
```

Weights must be versioned, evaluated, and adjusted from real producer outcomes.

## 7. Domain model

```ts
type InstrumentRole =
  | 'kick'
  | 'sub-bass'
  | 'bass'
  | 'snare'
  | 'clap'
  | 'closed-hat'
  | 'open-hat'
  | 'percussion'
  | 'lead'
  | 'chord'
  | 'pad'
  | 'arp'
  | 'texture'
  | 'fx'
  | 'vocal';

interface AudioFeatures {
  durationMs: number;
  tempo?: number;
  key?: MusicalKey;
  fundamentalHz?: number;
  spectralCentroid: number;
  spectralRolloff: number;
  rms: number;
  peak: number;
  crestFactor: number;
  transientStrength: number;
  attackMs: number;
  decayMs: number;
  stereoWidth: number;
  bands: {
    sub: number;
    bass: number;
    lowMid: number;
    mid: number;
    highMid: number;
    high: number;
  };
}

interface Sample {
  id: string;
  path: string;
  filename: string;
  packId?: string;
  role: InstrumentRole;
  features: AudioFeatures;
  tags: SampleTags;
}

interface PaletteState {
  tempo: number;
  key?: MusicalKey;
  genre?: Genre;
  items: PaletteItem[];
}

interface SamplePack {
  id: string;
  name: string;
  vendor?: string;
  sampleIds: string[];
  profile: PackProfile;
}
```

Role-specific rules are required. Kick-to-bass compatibility is different from pad-to-vocal or snare-to-hat compatibility.

## 8. Jev decision contract

Use several narrow decisions rather than one question such as “Does this sample work?”

```ts
type CompatibilityScore =
  | 'very-poor'
  | 'poor'
  | 'neutral'
  | 'good'
  | 'excellent';

interface CompatibilityDecision {
  frequencyFit: CompatibilityScore;
  transientFit: CompatibilityScore;
  tonalFit: CompatibilityScore;
  rhythmicFit: CompatibilityScore;
  textureFit: CompatibilityScore;
  arrangementFit: CompatibilityScore;
}

interface DecisionResult<T> {
  value: T;
  confidence: number;
  probabilities: Record<string, number>;
  modelVersion: string;
}
```

Low-confidence decisions should show alternatives rather than pretend certainty.

## 9. System architecture

### Desktop client

- Tauri
- React and TypeScript
- Vite
- Redux Toolkit for client workflow state
- RTK Query for communication with the local service

### Local analysis service

- Python
- Essentia as the primary DSP candidate
- NumPy and SciPy
- SoundFile for audio access
- Evaluate librosa, madmom, and aubio only where they add clear value

### Storage

- SQLite
- Local file paths remain local
- Analyse samples once and store versioned features
- Cache compatible Jev decisions
- Do not upload the user's sample library

### Main modules

```text
backend/
├── analysis/
│   ├── spectral.py
│   ├── transient.py
│   ├── harmony.py
│   ├── loudness.py
│   └── rhythm.py
├── library/
│   ├── scanner.py
│   ├── indexer.py
│   └── repository.py
├── palette/
│   ├── model.py
│   ├── compatibility.py
│   └── ranking.py
├── intelligence/
│   ├── jev.py
│   ├── questions.py
│   └── decisions.py
└── api/
```

```text
frontend/src/features/palette/
├── api/
├── browser/
├── inspector/
├── recommendations/
├── state/
└── types/
```

## 10. Storage model

Initial tables:

- `samples`
- `sample_features`
- `sample_tags`
- `sample_packs`
- `projects`
- `palettes`
- `palette_items`
- `compatibility_scores`
- `recommendation_outcomes`
- `analysis_versions`
- `decision_model_versions`

Cache keys must include:

```ts
interface DecisionCacheKey {
  paletteHash: string;
  candidateId: string;
  analysisVersion: string;
  decisionModelVersion: string;
  rankingVersion: string;
}
```

## 11. Producer feedback loop

Record behavior, not just explicit ratings:

```ts
interface RecommendationOutcome {
  candidateId: string;
  auditioned: boolean;
  selected: boolean;
  removedAfterSelection: boolean;
}
```

Primary product metric:

> Does the model predict which sounds producers audition, select, and keep?

Personal taste modelling comes later. Keep the first release focused on generic compatibility.

## 12. UI plan

The main screen contains:

- Current palette
- Searchable sample library
- Ranked recommendations
- Fast audio auditioning
- Compatibility details
- Warnings for frequency, transient, tonal, or arrangement conflicts
- Drag-and-drop into FL Studio after the standalone workflow is proven

The explanation panel should show measured facts and decision judgments separately so producers can see what is objective and what is inferred.

## 13. FL Studio integration strategy

### Phase 1: standalone desktop app

Validate recommendation quality without DAW complexity.

### Phase 2: drag and drop

Allow recommended WAV files to be dragged directly into FL Studio.

### Phase 3: thin VST3 bridge

The plugin exposes live project context and talks to the local service. Heavy analysis, storage, indexing, and Jev calls remain outside the plugin.

```text
VST3 bridge → local service → DSP/index/database/Jev
```

## 14. Delivery phases

### Phase 0 — feasibility

- Create labelled kick–bass evaluation data
- Implement baseline DSP rules
- Integrate Jev typed decisions
- Compare DSP, Jev, and hybrid ranking
- Set minimum quality and latency thresholds

### Phase 1 — kick-to-bass MVP

- Folder import and incremental scanning
- Audio feature extraction
- SQLite index
- Kick selection and bass ranking
- Audition controls
- Decision cache
- Outcome tracking

### Phase 2 — drum palette

- Add snare, clap, closed hat, open hat, and percussion
- Introduce full-palette scoring
- Add role-specific compatibility policies

### Phase 3 — melodic palette

- Add chords, leads, pads, arps, vocals, and textures
- Improve key and harmonic analysis
- Add tempo-aware rhythmic compatibility

### Phase 4 — pack intelligence

- Make packs first-class objects
- Rank packs against a project
- Detect pack redundancy
- Recommend complementary pack combinations

### Phase 5 — FL Studio workflow

- Drag-and-drop export
- Thin VST3 bridge
- Live project context
- Validate latency and reliability inside real sessions

### Phase 6 — personalisation

- Learn producer preferences from retained selections
- Re-rank generic compatibility using individual taste
- Keep generic and personalised scores visible and separable

## 15. Main risks and controls

| Risk | Control |
| --- | --- |
| Jev adds no useful musical judgment | Run the Phase 0 comparison before product investment |
| Slow recommendations | Precompute features, use staged retrieval, batch decisions, and cache results |
| Confident but poor recommendations | Use confidence thresholds and human-labelled evaluation |
| Role rules become generic and weak | Maintain separate policies per instrument relationship |
| Explanations misrepresent measurements | Keep DSP facts and model judgments distinct |
| Producers distrust the tool | Support instant auditioning and show alternatives rather than absolute claims |
| Model or feature changes invalidate scores | Version analysis, decision prompts/models, weights, and cache keys |
| Native plugin work consumes the project | Prove the standalone workflow before building VST3 integration |

## 16. Immediate build order

1. Define the kick and bass feature schema.
2. Build batch audio analysis for WAV files.
3. Import 100 kicks and 100 basses.
4. Build deterministic DSP compatibility baselines.
5. Define narrow Jev decision types and questions.
6. Create the human-labelled evaluation set.
7. Compare DSP, Jev, and hybrid ranking.
8. Decide whether Jev earns a place in the product.
9. If it does, build the local SQLite index and ranking API.
10. Build the minimal desktop browser and audition UI.

## 17. First milestone definition of done

The first milestone is complete when a producer can:

1. Import a folder of kicks and basses.
2. Select a kick.
3. Receive ranked bass recommendations within an acceptable delay.
4. Audition the top candidates immediately.
5. See separate DSP facts, Jev judgments, confidence, and warnings.
6. Select a recommendation and have the outcome recorded.
7. Demonstrate through blind evaluation that the hybrid top-10 ranking beats random selection and the DSP-only baseline.
