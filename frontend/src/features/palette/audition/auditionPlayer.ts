/**
 * The audition's playback engine (issue #34).
 *
 * Two detached `audio` elements, created through an injected factory and never
 * appended to a document: one candidate, one kick. Every `play` or `switchTo`
 * takes a monotone `requestSeq`, and anything that resolves after a newer
 * request has taken the sequence is abandoned — its object URL is revoked, its
 * element's `src` is cleared, and it never reaches the UI and never emits an
 * audition event. Switching halts the current sound before the next one starts,
 * so two candidates can never overlap, and one `stop()` releases everything.
 *
 * A playback is "started" by the element's own `playing` event, not by the
 * `play()` promise resolving: a refused `play()` is `playback_blocked`, an
 * element `error` is `decode_failed`, and neither is an audition.
 */

import type { AuditionErrorCode, AuditionMode, AuditionStatus, AuditionTrack } from "./auditionTypes";
import { auditionErrorState } from "./auditionSource";
import { gainForMode } from "./levelMatch";

/** The part of an `audio` element this module drives. */
export interface AuditionElement {
  src: string;
  currentTime: number;
  volume: number;
  paused: boolean;
  play(): Promise<void>;
  pause(): void;
  load(): void;
  addEventListener(type: string, listener: (event: unknown) => void): void;
  removeEventListener(type: string, listener: (event: unknown) => void): void;
}

/** The one DOM factory this module needs, injected so no test needs a document. */
export interface AuditionElementFactory {
  createElement(tagName: "audio"): AuditionElement;
}

export interface AuditionPlaybackRequest {
  track: AuditionTrack;
  mode: AuditionMode;
  /** The selected kick to play beside the candidate, or null when there is none. */
  kick: AuditionTrack | null;
  /** `performance.now()` at the press that asked for this playback. */
  pressedAt?: number;
}

/** What one started playback reports to the outcome layer. */
export interface StartedAudition {
  track: AuditionTrack;
  mode: AuditionMode;
  pressedAt: number;
  latencyMs: number;
}

export interface AuditionPlayerOptions {
  createElement: AuditionElementFactory["createElement"];
  createObjectURL(blob: Blob): string;
  revokeObjectURL(url: string): void;
  now(): number;
  /** Resolves one track's bytes; the shell source is the app's implementation. */
  load(track: AuditionTrack): Promise<ArrayBuffer>;
  /** Called once per playback that actually started. */
  onStarted?(started: StartedAudition): void;
  /** Called on every status change, so the control renders one place's truth. */
  onStatus?(status: AuditionStatus, errorCode: AuditionErrorCode | null): void;
}

export interface AuditionSnapshot {
  status: AuditionStatus;
  mode: AuditionMode;
  track: AuditionTrack | null;
  errorCode: AuditionErrorCode | null;
  requestSeq: number;
  /** How many elements were created; never more than two. */
  elements: number;
  /** Every object URL this player created and has not revoked. */
  liveUrls: string[];
  playing: AuditionMode | null;
  latencyMs: number | null;
}

interface Slot {
  readonly role: "candidate" | "kick";
  readonly element: AuditionElement;
  /** The sequence the currently loaded `src` belongs to, or 0. */
  seq: number;
  url: string | null;
}

export function createAuditionPlayer(options: AuditionPlayerOptions) {
  let status: AuditionStatus = "idle";
  let mode: AuditionMode = "candidate";
  let track: AuditionTrack | null = null;
  let errorCode: AuditionErrorCode | null = null;
  let requestSeq = 0;
  let playing: AuditionMode | null = null;
  let latencyMs: number | null = null;
  let pressedAt = 0;
  let started = false;
  const urls = new Set<string>();
  const slots: Record<"candidate" | "kick", Slot | null> = { candidate: null, kick: null };
  let elements = 0;

  function notify(): void {
    options.onStatus?.(status, errorCode);
  }

  function refuse(code: AuditionErrorCode): void {
    errorCode = code;
    status = auditionErrorState(code) === "blocked" ? "blocked" : "stopped";
    playing = null;
    notify();
  }

  /** The element's own confirmation that sound started. */
  function onPlaying(role: "candidate" | "kick"): void {
    const slot = slots[role];
    if (slot === null || role !== "candidate" || slot.seq !== requestSeq || started) {
      // Only the candidate opens a playback, and only for the sequence that is
      // still current: a late event from an abandoned request is not an audition.
      return;
    }
    if (track === null) {
      return;
    }
    started = true;
    status = "playing";
    playing = mode;
    latencyMs = options.now() - pressedAt;
    notify();
    options.onStarted?.({ track, mode, pressedAt, latencyMs });
  }

  function onError(role: "candidate" | "kick"): void {
    const slot = slots[role];
    if (slot === null || role !== "candidate" || slot.seq !== requestSeq) {
      return;
    }
    release();
    refuse("decode_failed");
  }

  function slotFor(role: "candidate" | "kick"): Slot {
    const existing = slots[role];
    if (existing !== null) {
      return existing;
    }
    const element = options.createElement("audio");
    elements += 1;
    element.addEventListener("playing", () => onPlaying(role));
    element.addEventListener("error", () => onError(role));
    const created: Slot = { role, element, seq: 0, url: null };
    slots[role] = created;
    return created;
  }

  /** Silence and release both elements. Never bumps the sequence. */
  function release(): void {
    for (const slot of [slots.candidate, slots.kick]) {
      if (slot === null) {
        continue;
      }
      slot.element.pause();
      slot.element.src = "";
      slot.element.load();
      if (slot.url !== null) {
        options.revokeObjectURL(slot.url);
        urls.delete(slot.url);
        slot.url = null;
      }
      slot.seq = 0;
    }
    playing = null;
  }

  function urlFor(bytes: ArrayBuffer): string {
    const url = options.createObjectURL(new Blob([bytes], { type: "audio/wav" }));
    urls.add(url);
    return url;
  }

  async function start(request: AuditionPlaybackRequest): Promise<void> {
    // Switching halts the current sound before the next one starts, so the two
    // elements are never both live from two different requests.
    release();
    requestSeq += 1;
    const seq = requestSeq;
    started = false;
    track = request.track;
    mode = request.mode;
    errorCode = null;
    latencyMs = null;
    pressedAt = request.pressedAt ?? options.now();
    status = "loading";
    notify();

    let candidateBytes: ArrayBuffer;
    let kickBytes: ArrayBuffer | null = null;
    try {
      candidateBytes = await options.load(request.track);
      if (request.mode === "comparison" && request.kick !== null) {
        kickBytes = await options.load(request.kick);
      }
    } catch (error) {
      if (seq !== requestSeq) {
        return;
      }
      const code = (error as { code?: unknown } | null)?.code;
      refuse(typeof code === "string" ? (code as AuditionErrorCode) : "shell_unavailable");
      return;
    }
    if (seq !== requestSeq) {
      // An abandoned load owns nothing: it never created a URL, so there is
      // nothing to revoke and nothing to show.
      return;
    }

    const candidate = slotFor("candidate");
    candidate.seq = seq;
    candidate.url = urlFor(candidateBytes);
    candidate.element.src = candidate.url;
    candidate.element.currentTime = 0;
    candidate.element.volume = gainForMode(request.mode);

    let kick: Slot | null = null;
    if (request.mode === "comparison" && kickBytes !== null) {
      kick = slotFor("kick");
      kick.seq = seq;
      kick.url = urlFor(kickBytes);
      kick.element.src = kick.url;
      kick.element.currentTime = 0;
      kick.element.volume = gainForMode(request.mode);
    }

    try {
      await candidate.element.play();
      if (kick !== null && seq === requestSeq) {
        await kick.element.play();
      }
    } catch {
      if (seq !== requestSeq) {
        return;
      }
      release();
      refuse("playback_blocked");
    }
  }

  function stop(): void {
    requestSeq += 1;
    started = false;
    release();
    status = "stopped";
    errorCode = null;
    notify();
  }

  function snapshot(): AuditionSnapshot {
    return {
      status,
      mode,
      track,
      errorCode,
      requestSeq,
      elements,
      liveUrls: [...urls],
      playing,
      latencyMs,
    };
  }

  return {
    /** Start the given candidate, halting whatever is sounding first. */
    play: start,
    /** The same operation, named for the producer switching cards. */
    switchTo: start,
    stop,
    snapshot,
  };
}

export type AuditionPlayer = ReturnType<typeof createAuditionPlayer>;
