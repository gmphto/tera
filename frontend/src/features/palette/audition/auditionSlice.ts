/**
 * The audition slice (issue #34).
 *
 * What the controls render, and the transitions between the five statuses. The
 * heavy lifting is not here: the player owns the elements, the abandoned-request
 * rule and the release, the source owns the shell's refusals, and the recorder
 * owns the event. This slice holds the visible consequence of each and nothing
 * that another module already knows.
 */

import { createSlice, type PayloadAction } from "@reduxjs/toolkit";

import type {
  AuditionErrorCode,
  AuditionMode,
  AuditionOutcomeRef,
  AuditionRecordState,
  AuditionState,
  AuditionStatus,
  AuditionTrack,
} from "../audition/auditionTypes";

const initialState: AuditionState = {
  status: "idle",
  mode: "candidate",
  track: null,
  outcome: null,
  errorCode: null,
  latencyMs: null,
  recordState: "idle",
};

const auditionSlice = createSlice({
  name: "audition",
  initialState,
  reducers: {
    /** A press: the source is being fetched and the elements are being started. */
    auditionBegan(
      state,
      action: PayloadAction<{ track: AuditionTrack; outcome: AuditionOutcomeRef; mode: AuditionMode }>,
    ) {
      state.status = "loading";
      state.mode = action.payload.mode;
      state.track = action.payload.track;
      state.outcome = action.payload.outcome;
      state.errorCode = null;
      state.latencyMs = null;
      state.recordState = "idle";
    },
    /** The player's own status, including a refusal and its code. */
    auditionStatusChanged(
      state,
      action: PayloadAction<{ status: AuditionStatus; errorCode: AuditionErrorCode | null }>,
    ) {
      state.status = action.payload.status;
      state.errorCode = action.payload.errorCode;
      if (action.payload.status !== "playing") {
        state.latencyMs = null;
      }
    },
    /** The element's `playing` event: an audition that actually started. */
    auditionStarted(state, action: PayloadAction<{ mode: AuditionMode; latencyMs: number }>) {
      state.status = "playing";
      state.mode = action.payload.mode;
      state.latencyMs = action.payload.latencyMs;
      state.errorCode = null;
      state.recordState = "pending";
    },
    auditionStopped(state) {
      state.status = "stopped";
      state.latencyMs = null;
      state.recordState = state.recordState === "pending" ? "idle" : state.recordState;
    },
    /** How the `auditioned` event for the last start is doing. */
    auditionRecordChanged(state, action: PayloadAction<AuditionRecordState>) {
      state.recordState = action.payload;
    },
    /** The comparison toggle, before any playback asks for the kick. */
    auditionModeToggled(state) {
      state.mode = state.mode === "comparison" ? "candidate" : "comparison";
    },
  },
});

export const {
  auditionBegan,
  auditionStatusChanged,
  auditionStarted,
  auditionStopped,
  auditionRecordChanged,
  auditionModeToggled,
} = auditionSlice.actions;

export const auditionReducer = auditionSlice.reducer;
export default auditionSlice.reducer;

// ---------------------------------------------------------------------------
// selectors
// ---------------------------------------------------------------------------

interface WithAudition {
  audition: AuditionState;
}

export function selectAudition(state: WithAudition): AuditionState {
  return state.audition;
}

export function selectAuditionStatus(state: WithAudition): AuditionStatus {
  return state.audition.status;
}

export function selectAuditionMode(state: WithAudition): AuditionMode {
  return state.audition.mode;
}

export function selectAuditionError(state: WithAudition): AuditionErrorCode | null {
  return state.audition.errorCode;
}

export function selectAuditionLatency(state: WithAudition): number | null {
  return state.audition.latencyMs;
}

export function selectAuditionRecordState(state: WithAudition): AuditionRecordState {
  return state.audition.recordState;
}

interface AuditionContext {
  projectId: string | null;
  batch: {
    palette: { palette_id: string; revision: number };
    ranking_version: string;
    mode: string;
    results: { candidate_id: string; analysis_version: string }[];
  } | null;
  run: { run_id: string } | null;
}

/**
 * The run identity one card's audition is recorded against, or null.
 *
 * Assembled from the three places that own the parts — the palette's project,
 * the batch the card was rendered from, and that run's id — so the card can
 * render the control without carrying a second copy of any of them, and so a
 * later refresh cannot move an audition onto the newest run.
 *
 * A pure function of its inputs rather than a `useSelector` argument: it returns
 * a fresh object, and a selector that does that re-renders on every action. The
 * control memoises it over the three stable references it reads.
 */
export function auditionOutcomeFor(
  context: AuditionContext,
  candidateId: string,
): AuditionOutcomeRef | null {
  const { batch, run, projectId } = context;
  if (batch === null || run === null || projectId === null) {
    return null;
  }
  const result = batch.results.find((entry) => entry.candidate_id === candidateId);
  if (result === undefined) {
    return null;
  }
  return {
    projectId,
    paletteId: batch.palette.palette_id,
    paletteRevision: batch.palette.revision,
    runId: run.run_id,
    candidateId: result.candidate_id,
    rankingVersion: batch.ranking_version,
    mode: batch.mode,
    candidateAnalysisVersion: result.analysis_version,
  };
}
