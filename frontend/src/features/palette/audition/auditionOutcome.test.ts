/**
 * One event per playback that started, and none for the ones that did not
 * (issue #34).
 *
 * The recorder is driven here through the real player, so the two rules that
 * matter are tested together: the body is #29's ten keys copied from the card's
 * own run, and a playback that never reached its `playing` event — a refused
 * play, a stop before it started, a load a newer press abandoned — sends
 * nothing.
 */

import { describe, expect, it } from "vitest";

import type { AuditionOutcomeRef, AuditionTrack } from "./auditionTypes";
import {
  AUDITION_EVENT_KEYS,
  auditionEventFor,
  createAuditionRecorder,
  type AuditionEvent,
} from "./auditionOutcome";
import { createAuditionPlayer, type AuditionElement } from "./auditionPlayer";

const CANDIDATE: AuditionTrack = { sampleId: `sha256:${"a".repeat(64)}`, fileName: "candidate-001.wav" };
const OTHER: AuditionTrack = { sampleId: `sha256:${"b".repeat(64)}`, fileName: "candidate-002.wav" };

const OUTCOME: AuditionOutcomeRef = {
  projectId: "project-001",
  paletteId: "palette-001",
  paletteRevision: 6,
  runId: "a".repeat(64),
  candidateId: CANDIDATE.sampleId,
  rankingVersion: "hybrid-ranking-v1",
  mode: "dsp-only",
  candidateAnalysisVersion: "b".repeat(64),
};

class FakeElement implements AuditionElement {
  src = "";
  currentTime = 0;
  volume = 1;
  paused = true;
  refusePlay = false;
  private readonly listeners = new Map<string, ((event: unknown) => void)[]>();

  play(): Promise<void> {
    if (this.refusePlay) {
      return Promise.reject(new Error("NotAllowedError"));
    }
    this.paused = false;
    return Promise.resolve();
  }

  pause(): void {
    this.paused = true;
  }

  load(): void {}

  addEventListener(type: string, listener: (event: unknown) => void): void {
    this.listeners.set(type, [...(this.listeners.get(type) ?? []), listener]);
  }

  removeEventListener(): void {}

  emit(type: string): void {
    for (const listener of this.listeners.get(type) ?? []) {
      listener({ type });
    }
  }
}

/** A recorder wired to a real player, with a counted sender. */
function bench(options: { fail?: boolean; refusePlay?: boolean; load?: () => Promise<ArrayBuffer> } = {}) {
  const sent: AuditionEvent[] = [];
  const elements: FakeElement[] = [];
  let ids = 0;
  const recorder = createAuditionRecorder({
    send: async (event) => {
      sent.push(event);
      if (options.fail === true) {
        throw Object.assign(new Error("refused"), { code: "database_locked" });
      }
      return { created: sent.length === 1 };
    },
    newEventId: () => {
      ids += 1;
      return `event-${String(ids).padStart(3, "0")}`;
    },
  });
  const player = createAuditionPlayer({
    createElement: () => {
      const element = new FakeElement();
      element.refusePlay = options.refusePlay === true;
      elements.push(element);
      return element;
    },
    createObjectURL: () => `blob:tera/${elements.length}`,
    revokeObjectURL: () => {},
    now: () => 0,
    load: options.load ?? (async () => new Uint8Array([82, 73, 70, 70]).buffer),
    onStarted: ({ track }) => {
      void recorder.record({ ...OUTCOME, candidateId: track.sampleId });
    },
  });
  return { player, recorder, sent, elements, ids: () => ids };
}

const request = (track: AuditionTrack) => ({ track, mode: "candidate" as const, kick: null });

describe("the event body", () => {
  it("is exactly #29's ten keys for an audition", () => {
    const event = auditionEventFor({ clientEventId: "event-001", outcome: OUTCOME });
    expect(Object.keys(event).sort()).toEqual([...AUDITION_EVENT_KEYS].sort());
    expect(AUDITION_EVENT_KEYS).toHaveLength(10);
    expect(event).toEqual({
      client_event_id: "event-001",
      event_type: "auditioned",
      project_id: "project-001",
      palette_id: "palette-001",
      candidate_id: CANDIDATE.sampleId,
      run_id: "a".repeat(64),
      palette_revision: 6,
      ranking_version: "hybrid-ranking-v1",
      mode: "dsp-only",
      candidate_analysis_version: "b".repeat(64),
    });
  });

  it("copies the run identity it was given, never a newer one", () => {
    const older = { ...OUTCOME, runId: "c".repeat(64), paletteRevision: 4, mode: "hybrid" };
    const event = auditionEventFor({ clientEventId: "event-001", outcome: older });
    expect(event.run_id).toBe("c".repeat(64));
    expect(event.palette_revision).toBe(4);
    expect(event.mode).toBe("hybrid");
  });
});

describe("a started playback", () => {
  it("records exactly one event per start", async () => {
    const run = bench();
    await run.player.play(request(CANDIDATE));
    expect(run.sent).toHaveLength(0);
    run.elements[0].emit("playing");
    await Promise.resolve();
    expect(run.sent).toHaveLength(1);
    expect(run.sent[0].event_type).toBe("auditioned");
    expect(run.sent[0].client_event_id).toBe("event-001");
  });

  it("records a second playback of the same candidate as a new audition", async () => {
    const run = bench();
    await run.player.play(request(CANDIDATE));
    run.elements[0].emit("playing");
    await Promise.resolve();
    await run.player.play(request(CANDIDATE));
    run.elements[0].emit("playing");
    await Promise.resolve();
    expect(run.sent).toHaveLength(2);
    expect(run.sent[0].client_event_id).not.toBe(run.sent[1].client_event_id);
  });

  it("records nothing for a refused play", async () => {
    const run = bench({ refusePlay: true });
    await run.player.play(request(CANDIDATE));
    run.elements[0]?.emit("playing");
    await Promise.resolve();
    expect(run.sent).toHaveLength(0);
    expect(run.player.snapshot().errorCode).toBe("playback_blocked");
  });

  it("records nothing when a stop lands before the element started", async () => {
    const run = bench();
    await run.player.play(request(CANDIDATE));
    run.player.stop();
    run.elements[0].emit("playing");
    await Promise.resolve();
    expect(run.sent).toHaveLength(0);
  });

  it("records nothing for a load a newer press abandoned", async () => {
    const pending: ((bytes: ArrayBuffer) => void)[] = [];
    const run = bench({
      load: () =>
        new Promise<ArrayBuffer>((resolve) => {
          pending.push(resolve);
        }),
    });
    const slow = run.player.play(request(CANDIDATE));
    const quick = run.player.play(request(OTHER));
    pending[1](new Uint8Array([2]).buffer);
    await quick;
    run.elements[0].emit("playing");
    await Promise.resolve();
    expect(run.sent).toHaveLength(1);
    expect(run.sent[0].candidate_id).toBe(OTHER.sampleId);
    pending[0](new Uint8Array([1]).buffer);
    await slow;
    run.elements[0].emit("playing");
    await Promise.resolve();
    expect(run.sent).toHaveLength(1);
  });
});

describe("a failed write", () => {
  it("leaves the event retryable and never reports it recorded", async () => {
    const run = bench({ fail: true });
    await run.player.play(request(CANDIDATE));
    run.elements[0].emit("playing");
    await Promise.resolve();
    await Promise.resolve();
    expect(run.sent).toHaveLength(1);
    expect(run.recorder.pendingEvent()).toEqual(run.sent[0]);
  });

  it("re-sends the identical body on retry, with the same event id", async () => {
    let fail = true;
    const sent: AuditionEvent[] = [];
    const recorder = createAuditionRecorder({
      send: async (event) => {
        sent.push(event);
        if (fail) {
          throw new Error("refused");
        }
        return { created: false };
      },
      newEventId: () => "event-001",
    });
    const first = await recorder.record(OUTCOME);
    expect(first).toMatchObject({ state: "not-recorded", reason: "write_failed" });
    fail = false;
    const second = await recorder.retry();
    expect(second).toMatchObject({ state: "recorded", eventId: "event-001", reason: null });
    expect(sent).toHaveLength(2);
    // Byte for byte the same body: one stored row for one playback.
    expect(sent[1]).toEqual(sent[0]);
    expect(recorder.pendingEvent()).toBeNull();
    expect(await recorder.retry()).toBeNull();
  });

  it("treats the idempotent repeat as success", async () => {
    const recorder = createAuditionRecorder({
      send: async () => ({ created: false }),
      newEventId: () => "event-001",
    });
    await expect(recorder.record(OUTCOME)).resolves.toEqual({
      state: "recorded",
      eventId: "event-001",
      reason: null,
    });
  });

  it("never writes against a guessed project", async () => {
    const sent: AuditionEvent[] = [];
    const recorder = createAuditionRecorder({
      send: async (event) => {
        sent.push(event);
        return {};
      },
      newEventId: () => "event-001",
    });
    for (const projectId of ["", "   "]) {
      const result = await recorder.record({ ...OUTCOME, projectId });
      expect(result).toEqual({ state: "not-recorded", eventId: "event-001", reason: "no_project" });
    }
    expect(sent).toHaveLength(0);
    expect(recorder.pendingEvent()).toBeNull();
  });
});
