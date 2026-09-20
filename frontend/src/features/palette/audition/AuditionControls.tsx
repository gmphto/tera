/**
 * The audition controls on one ranked card (issue #34).
 *
 * A thin shell over three tested modules: the source resolves `{sampleId,
 * fileName}` through the shell, the player owns the elements and the release,
 * and the recorder owns the event. What lives here is the wiring — which press
 * asks for what, where the run identity comes from, and the keyboard — plus the
 * visible readouts the acceptance criteria name.
 *
 * The card supplies nothing but its candidate id: the run identity is read from
 * the batch the card was rendered from, so a later refresh cannot move an
 * audition onto a run that never produced the card.
 */

import { invoke } from "@tauri-apps/api/core";
import { useCallback, useEffect, useMemo, useRef } from "react";

import { useAppDispatch, useAppSelector } from "../../../app/hooks";
import type { RootState } from "../../../app/store";
import { useGetSampleQuery } from "../api/libraryApi";
import { useRecordOutcomeMutation } from "../api/recommendationsApi";
import {
  selectRecommendationBatch,
  selectRecommendationRun,
} from "../state/recommendationsSlice";
import { AUDITION_KEYS, commandForAuditionKey } from "./auditionKeys";
import {
  createAuditionRecorder,
  outcomeArgsFor,
  type AuditionRecorder,
} from "./auditionOutcome";
import { createAuditionPlayer, type AuditionPlayer } from "./auditionPlayer";
import {
  auditionErrorSentence,
  auditionCodeOf,
  createAuditionSource,
} from "./auditionSource";
import {
  auditionBegan,
  auditionModeToggled,
  auditionOutcomeFor,
  auditionRecordChanged,
  auditionStarted,
  auditionStatusChanged,
  selectAudition,
} from "./auditionSlice";
import type { AuditionMode, AuditionOutcomeRef, AuditionTrack } from "./auditionTypes";
import { descriptionForGain } from "./levelMatch";

export interface AuditionControlsProps {
  candidateId: string;
}

/** Whether this page is inside the shell that can read local files. */
export function shellAvailable(): boolean {
  return typeof window !== "undefined" && "__TAURI_INTERNALS__" in window;
}

/** One line per record state, so the producer can tell what was written. */
const RECORD_COPY: Record<string, string> = {
  idle: "No audition recorded yet.",
  pending: "Recording this audition…",
  recorded: "Audition recorded.",
  "not-recorded": "It played, but the audition event was not recorded.",
};

export function AuditionControls({ candidateId }: AuditionControlsProps) {
  const dispatch = useAppDispatch();
  const audition = useAppSelector(selectAudition);
  const projectId = useAppSelector(
    (state: RootState) => state.palette.project?.project_id ?? null,
  );
  const batch = useAppSelector(selectRecommendationBatch);
  const run = useAppSelector(selectRecommendationRun);
  const outcome: AuditionOutcomeRef | null = useMemo(
    () => auditionOutcomeFor({ projectId, batch, run }, candidateId),
    [batch, candidateId, projectId, run],
  );
  const kickSampleId = useAppSelector(
    (state: RootState) => state.palette.items.kick?.sample_id ?? null,
  );
  // One library read per sample, cached by the endpoint's own tag: the file name
  // and the stored file status are the only two things playback needs from it.
  const candidateDetail = useGetSampleQuery(candidateId);
  const kickDetail = useGetSampleQuery(kickSampleId ?? "", { skip: kickSampleId === null });
  const [recordOutcome] = useRecordOutcomeMutation();

  const playerRef = useRef<AuditionPlayer | null>(null);
  const recorderRef = useRef<AuditionRecorder | null>(null);
  // The player is created once and outlives a resolution, so the identity it
  // records against is read at the moment a playback starts rather than captured
  // when the player was built.
  const outcomeRef = useRef(outcome);
  outcomeRef.current = outcome;
  // What the player's loader reads: the details currently in hand, by sample.
  const details = useRef(new Map<string, { fileName: string; fileStatus: string }>());
  for (const detail of [candidateDetail.data?.sample, kickDetail.data?.sample]) {
    if (detail !== undefined) {
      details.current.set(detail.sample_id, {
        fileName: detail.file_name,
        fileStatus: detail.file_status,
      });
    }
  }

  const engine = useCallback((): AuditionPlayer => {
    if (playerRef.current !== null) {
      return playerRef.current;
    }
    const source = createAuditionSource({ invoke });
    const recorder = createAuditionRecorder({
      send: async (event) => {
        await recordOutcome(outcomeArgsFor(event)).unwrap();
      },
      newEventId: () => crypto.randomUUID(),
    });
    recorderRef.current = recorder;
    playerRef.current = createAuditionPlayer({
      createElement: (tagName) => document.createElement(tagName),
      createObjectURL: (blob) => URL.createObjectURL(blob),
      revokeObjectURL: (url) => URL.revokeObjectURL(url),
      now: () => performance.now(),
      load: async (track) =>
        source.loadAuditionSource({
          track,
          fileStatus: details.current.get(track.sampleId)?.fileStatus ?? null,
        }),
      onStatus: (status, errorCode) => {
        dispatch(auditionStatusChanged({ status, errorCode }));
      },
      onStarted: ({ track, mode, latencyMs }) => {
        dispatch(auditionStarted({ mode, latencyMs }));
        const identity = outcomeRef.current;
        if (identity === null) {
          return;
        }
        void recorder
          .record({ ...identity, candidateId: track.sampleId })
          .then((recorded) => {
            dispatch(auditionRecordChanged(recorded.state));
          });
      },
    });
    return playerRef.current;
  }, [dispatch, recordOutcome]);

  const start = useCallback(
    async (mode: AuditionMode) => {
      if (outcome === null) {
        dispatch(auditionStatusChanged({ status: "blocked", errorCode: "not_recorded" }));
        return;
      }
      const candidate = details.current.get(candidateId);
      if (candidate === undefined) {
        // The library read has not answered yet, or refused. A refusal is a
        // missing file; anything else is simply not ready to play.
        dispatch(
          auditionStatusChanged(
            candidateDetail.isError
              ? { status: "blocked", errorCode: "missing_file" }
              : { status: "loading", errorCode: null },
          ),
        );
        return;
      }
      const kick =
        mode === "comparison" && kickSampleId !== null
          ? (details.current.get(kickSampleId) ?? null)
          : null;
      const player = engine();
      try {
        const track: AuditionTrack = { sampleId: candidateId, fileName: candidate.fileName };
        dispatch(auditionBegan({ track, outcome, mode }));
        await player.play({
          track,
          mode,
          kick:
            kick === null ? null : { sampleId: kickSampleId as string, fileName: kick.fileName },
          pressedAt: performance.now(),
        });
      } catch (error) {
        dispatch(auditionStatusChanged({ status: "blocked", errorCode: auditionCodeOf(error) }));
      }
    },
    [candidateDetail.isError, candidateId, dispatch, engine, kickSampleId, outcome],
  );

  const stop = useCallback(() => {
    playerRef.current?.stop();
  }, []);

  useEffect(() => () => playerRef.current?.stop(), []);

  useEffect(() => {
    const release = () => playerRef.current?.stop();
    window.addEventListener("pagehide", release);
    return () => window.removeEventListener("pagehide", release);
  }, []);

  const onKeyDown = (event: React.KeyboardEvent<HTMLDivElement>) => {
    const command = commandForAuditionKey(event);
    if (command === null) {
      return;
    }
    if (command === "toggle") {
      if (audition.status === "playing") {
        stop();
      } else {
        void start(audition.mode);
      }
    } else if (command === "toggle-comparison") {
      const next: AuditionMode = audition.mode === "comparison" ? "candidate" : "comparison";
      dispatch(auditionModeToggled());
      void start(next);
    } else if (command === "replay") {
      void start(audition.mode);
    } else {
      stop();
    }
  };

  const shell = shellAvailable();
  const blocked = !shell || audition.status === "blocked";
  const errorCode = audition.errorCode;
  const showError =
    errorCode !== null && (audition.status === "blocked" || audition.status === "stopped");
  const recording = audition.recordState === "not-recorded";

  return (
    <div
      className="rec-audition"
      data-testid="audition-controls"
      tabIndex={0}
      role="group"
      aria-label="Audition this candidate"
      onKeyDown={onKeyDown}
    >
      <div className="rec-audition__actions">
        <button
          type="button"
          data-testid="audition-play"
          disabled={blocked}
          aria-label="Play this candidate on its own"
          aria-describedby={showError ? `${candidateId}-audition-error` : undefined}
          onClick={() => {
            void start(audition.mode);
          }}
        >
          {audition.status === "playing" ? "Playing" : "Play"}
        </button>
        <button
          type="button"
          data-testid="audition-stop"
          aria-label="Stop the audition and release the file"
          onClick={stop}
        >
          Stop
        </button>
        <button
          type="button"
          data-testid="audition-compare"
          aria-pressed={audition.mode === "comparison"}
          disabled={blocked || kickSampleId === null}
          aria-label="Play this candidate together with the selected kick"
          onClick={() => {
            const next: AuditionMode = audition.mode === "comparison" ? "candidate" : "comparison";
            dispatch(auditionModeToggled());
            void start(next);
          }}
        >
          Compare with the kick
        </button>
      </div>
      <p className="rec-audition__status" data-testid="audition-status" data-status={audition.status}>
        {audition.status}
      </p>
      <p className="rec-audition__gain" data-testid="audition-gain">
        {descriptionForGain(audition.mode)}
      </p>
      <p
        className="rec-audition__latency"
        data-testid="audition-latency"
        data-latency-ms={audition.latencyMs === null ? "" : Math.round(audition.latencyMs)}
      >
        {audition.latencyMs === null ? "Start latency not measured yet" : `${Math.round(audition.latencyMs)} ms to start`}
      </p>
      <p
        className="rec-audition__record"
        data-testid="audition-record-state"
        data-record-state={audition.recordState}
      >
        {RECORD_COPY[audition.recordState]}
      </p>
      {recording ? (
        <button
          type="button"
          data-testid="audition-retry-record"
          onClick={() => {
            void recorderRef.current?.retry().then((result) => {
              if (result !== null) {
                dispatch(auditionRecordChanged(result.state));
              }
            });
          }}
        >
          Retry recording the audition
        </button>
      ) : null}
      {showError ? (
        <p
          className="rec-audition__error"
          id={`${candidateId}-audition-error`}
          data-testid="audition-error"
          data-code={errorCode}
          role="alert"
        >
          {auditionErrorSentence(errorCode)}
        </p>
      ) : null}
      <ul className="rec-audition__legend" data-testid="audition-legend">
        {AUDITION_KEYS.map((binding) => (
          <li key={binding.code}>
            <kbd>{binding.key}</kbd> {binding.action}
          </li>
        ))}
      </ul>
    </div>
  );
}
