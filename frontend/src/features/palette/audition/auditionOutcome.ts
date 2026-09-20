/**
 * The audition's outcome write (issue #34).
 *
 * One `auditioned` event per playback that actually started, sent through #29's
 * route with exactly the ten keys it validates. The identity is the card's own
 * run, never the newest one: a card rendered from run A records against run A
 * even after a refresh replaced the panel's cards.
 *
 * The recorder exists for one reason: `client_event_id` is minted once when a
 * playback starts and every retry re-sends the identical body, so a retry
 * collapses to one stored row. Playback never waits on this, and the audition is
 * never reported as recorded before the write answered.
 */

import type { AuditionOutcomeRef, AuditionRecordState } from "./auditionTypes";
import type { OutcomeArgs } from "../api/recommendationsApi";

/** #29's event type for an audition; it is one of the four it accepts. */
export const AUDITION_EVENT_TYPE = "auditioned";

/** The exact ten keys `POST /outcomes` validates for an audition. */
export const AUDITION_EVENT_KEYS = [
  "client_event_id",
  "event_type",
  "project_id",
  "palette_id",
  "candidate_id",
  "run_id",
  "palette_revision",
  "ranking_version",
  "mode",
  "candidate_analysis_version",
] as const;

export interface AuditionEvent {
  client_event_id: string;
  event_type: typeof AUDITION_EVENT_TYPE;
  project_id: string;
  palette_id: string;
  candidate_id: string;
  run_id: string;
  palette_revision: number;
  ranking_version: string;
  mode: string;
  candidate_analysis_version: string;
}

export interface AuditionRecordResult {
  state: AuditionRecordState;
  /** The id this attempt used; the same value on every retry of one start. */
  eventId: string;
  /** Why it is not recorded, or null when it is. */
  reason: "no_project" | "write_failed" | null;
}

/** The body for one audition, copied from the card's own run identity. */
export function auditionEventFor(input: {
  clientEventId: string;
  outcome: AuditionOutcomeRef;
}): AuditionEvent {
  const { outcome } = input;
  return {
    client_event_id: input.clientEventId,
    event_type: AUDITION_EVENT_TYPE,
    project_id: outcome.projectId,
    palette_id: outcome.paletteId,
    candidate_id: outcome.candidateId,
    run_id: outcome.runId,
    palette_revision: outcome.paletteRevision,
    ranking_version: outcome.rankingVersion,
    mode: outcome.mode,
    candidate_analysis_version: outcome.candidateAnalysisVersion,
  };
}

/**
 * One wire event as the api's own argument type.
 *
 * The route's endpoint takes the camelCase argument shape, and this is the one
 * place the two are mapped, so there is no second body builder anywhere in the
 * audition code.
 */
export function outcomeArgsFor(event: AuditionEvent): OutcomeArgs {
  return {
    clientEventId: event.client_event_id,
    eventType: event.event_type,
    projectId: event.project_id,
    paletteId: event.palette_id,
    candidateId: event.candidate_id,
    runId: event.run_id,
    paletteRevision: event.palette_revision,
    rankingVersion: event.ranking_version,
    mode: event.mode,
    candidateAnalysisVersion: event.candidate_analysis_version,
  };
}

/**
 * The recorder, bound to one sender and one id source.
 *
 * `send` is the app's `POST /outcomes`; it resolves for the 2xx that is a
 * success — #29 answers 201 for a new row and 200 for the repeat — and rejects
 * for anything else, which is the only way this becomes `not-recorded`.
 */
export function createAuditionRecorder(options: {
  send(event: AuditionEvent): Promise<unknown>;
  newEventId(): string;
}) {
  let pending: AuditionEvent | null = null;

  function result(state: AuditionRecordState, eventId: string,
                  reason: AuditionRecordResult["reason"]): AuditionRecordResult {
    return { state, eventId, reason };
  }

  async function deliver(event: AuditionEvent): Promise<AuditionRecordResult> {
    try {
      await options.send(event);
      pending = null;
      return result("recorded", event.client_event_id, null);
    } catch {
      pending = event;
      return result("not-recorded", event.client_event_id, "write_failed");
    }
  }

  return {
    /** Begin one audition: a fresh id, and a body a retry re-sends verbatim. */
    async record(outcome: AuditionOutcomeRef): Promise<AuditionRecordResult> {
      const eventId = options.newEventId();
      if (typeof outcome.projectId !== "string" || outcome.projectId.trim().length === 0) {
        // #32 did not expose a project, so there is nothing to write the event
        // against: guessing one, or defaulting it, would record the audition on
        // a project the producer never chose.
        pending = null;
        return result("not-recorded", eventId, "no_project");
      }
      return deliver(auditionEventFor({ clientEventId: eventId, outcome }));
    },
    /** Re-send the event still waiting, byte for byte, with the same id. */
    async retry(): Promise<AuditionRecordResult | null> {
      if (pending === null) {
        return null;
      }
      return deliver(pending);
    },
    /** The event that has not been recorded yet, or null. */
    pendingEvent(): AuditionEvent | null {
      return pending;
    },
  };
}

export type AuditionRecorder = ReturnType<typeof createAuditionRecorder>;
