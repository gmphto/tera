/**
 * The audition's vocabulary (issue #34).
 *
 * Types and closed literal unions only, so every module in this folder names the
 * same modes, statuses, error codes and record states. Nothing here reads a
 * measurement, a path or the service: an audition decides from the card, the
 * palette state and the shell's answer.
 */

/** What is playing: the candidate alone, or the candidate with the selected kick. */
export const AUDITION_MODES = ["candidate", "comparison"] as const;
export type AuditionMode = (typeof AUDITION_MODES)[number];

/**
 * The panel's closed set of playback states.
 *
 * `loading` covers a source being fetched from the shell or an element being
 * started; `blocked` is a refusal the producer must act on, and it always
 * carries one `AuditionErrorCode`.
 */
export const AUDITION_STATUSES = ["idle", "loading", "playing", "stopped", "blocked"] as const;
export type AuditionStatus = (typeof AUDITION_STATUSES)[number];

/**
 * What one candidate needs to be auditioned, and nothing more.
 *
 * There is no path here: `sampleId` is the `sha256:<64 hex>` content identity and
 * `fileName` is the basename the library read returned. The shell resolves them
 * against a registered root and verifies the bytes against `sampleId`.
 */
export interface AuditionTrack {
  sampleId: string;
  fileName: string;
}

/**
 * The run identity one auditioned event is recorded against.
 *
 * Every field is copied from the card's own batch and run block, never from the
 * newest run: a card rendered from run A records against run A even after a
 * refresh replaced the panel's cards.
 */
export interface AuditionOutcomeRef {
  projectId: string;
  paletteId: string;
  paletteRevision: number;
  runId: string;
  candidateId: string;
  rankingVersion: string;
  mode: string;
  candidateAnalysisVersion: string;
}

/**
 * Every way an audition can fail, closed.
 *
 * `_docs/audition-controls.md` gives each code one client-facing sentence and the
 * one state it produces; nothing else in this folder invents a code, a message
 * or a second vocabulary.
 */
export const AUDITION_ERROR_CODES = [
  "shell_unavailable",
  "no_registered_root",
  "root_invalid",
  "root_limit_reached",
  "invalid_file_name",
  "missing_file",
  "unreadable_file",
  "unsupported_extension",
  "too_large",
  "content_mismatch",
  "playback_blocked",
  "decode_failed",
  "not_recorded",
] as const;
export type AuditionErrorCode = (typeof AUDITION_ERROR_CODES)[number];

/**
 * How the `auditioned` event for the last started playback is doing.
 *
 * `recorded` is only ever set after a 200 or a 201; `not-recorded` is the state a
 * failed write leaves behind, with the identical body retryable, and playback is
 * never affected by either.
 */
export const AUDITION_RECORD_STATES = ["idle", "pending", "recorded", "not-recorded"] as const;
export type AuditionRecordState = (typeof AUDITION_RECORD_STATES)[number];

/** What the controls render, and all the state the audition slice holds. */
export interface AuditionState {
  status: AuditionStatus;
  mode: AuditionMode;
  /** The candidate being auditioned, or null before the first one. */
  track: AuditionTrack | null;
  /** The run identity of the card `track` came from, or null. */
  outcome: AuditionOutcomeRef | null;
  /** The refusal behind a `blocked` status. */
  errorCode: AuditionErrorCode | null;
  /** The last measured start latency, in milliseconds. */
  latencyMs: number | null;
  recordState: AuditionRecordState;
}
