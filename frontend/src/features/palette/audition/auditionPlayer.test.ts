/**
 * The player's contract, driven by a fake element (issue #34).
 *
 * The fake is the only DOM this needs: it records every `src`, `volume`,
 * `currentTime`, `pause()`, `load()` and `play()`, and it fires `playing` and
 * `error` when the test says so. That is what makes the two hard cases provable
 * rather than asserted — an abandoned request releasing its URL and emitting
 * nothing, and exactly one audition event per started playback.
 */

import { describe, expect, it } from "vitest";

import type { AuditionMode, AuditionTrack } from "./auditionTypes";
import {
  createAuditionPlayer,
  type AuditionElement,
  type StartedAudition,
} from "./auditionPlayer";
import { AUDITION_COMPARISON_GAIN_DB } from "./levelMatch";

const CANDIDATE: AuditionTrack = { sampleId: `sha256:${"a".repeat(64)}`, fileName: "candidate-001.wav" };
const OTHER: AuditionTrack = { sampleId: `sha256:${"b".repeat(64)}`, fileName: "candidate-002.wav" };
const KICK: AuditionTrack = { sampleId: `sha256:${"c".repeat(64)}`, fileName: "kick-001.wav" };

class FakeElement implements AuditionElement {
  src = "";
  currentTime = 0;
  volume = 1;
  paused = true;
  readonly calls: string[] = [];
  readonly listeners = new Map<string, ((event: unknown) => void)[]>();
  /** Set to make the next `play()` reject, as a blocked autoplay would. */
  refusePlay = false;

  play(): Promise<void> {
    this.calls.push("play");
    if (this.refusePlay) {
      return Promise.reject(new Error("NotAllowedError"));
    }
    // A real element's `play()` settles as buffering starts; `playing` is the
    // event that means sound is actually out, and only the test fires that.
    this.paused = false;
    return Promise.resolve();
  }

  pause(): void {
    this.calls.push("pause");
    this.paused = true;
  }

  load(): void {
    this.calls.push("load");
  }

  addEventListener(type: string, listener: (event: unknown) => void): void {
    const existing = this.listeners.get(type) ?? [];
    existing.push(listener);
    this.listeners.set(type, existing);
  }

  removeEventListener(type: string, listener: (event: unknown) => void): void {
    this.listeners.set(type, (this.listeners.get(type) ?? []).filter((one) => one !== listener));
  }

  emit(type: string): void {
    for (const listener of this.listeners.get(type) ?? []) {
      listener({ type });
    }
  }
}

function harness(
  options: { load?: (track: AuditionTrack) => Promise<ArrayBuffer>; refusePlay?: boolean } = {},
) {
  const created: FakeElement[] = [];
  const urls: string[] = [];
  const revoked: string[] = [];
  const events: StartedAudition[] = [];
  const statuses: string[] = [];
  let clock = 1000;
  const bytes = new Uint8Array([82, 73, 70, 70]).buffer;
  const player = createAuditionPlayer({
    createElement: () => {
      const element = new FakeElement();
      element.refusePlay = options.refusePlay === true;
      created.push(element);
      return element;
    },
    createObjectURL: () => {
      const url = `blob:tera/${urls.length + 1}`;
      urls.push(url);
      return url;
    },
    revokeObjectURL: (url) => {
      revoked.push(url);
    },
    now: () => clock,
    load: options.load ?? (async () => bytes),
    onStarted: (started) => events.push(started),
    onStatus: (status) => statuses.push(status),
  });
  return {
    player,
    created,
    urls,
    revoked,
    events,
    statuses,
    advance: (ms: number) => {
      clock += ms;
    },
  };
}

const request = (track: AuditionTrack, mode: AuditionMode = "candidate") => ({
  track,
  mode,
  kick: mode === "comparison" ? KICK : null,
});

describe("the elements", () => {
  it("creates at most two detached elements, reusing them", async () => {
    const bench = harness();
    await bench.player.play(request(CANDIDATE, "comparison"));
    bench.created[0].emit("playing");
    await bench.player.play(request(OTHER, "comparison"));
    bench.created[0].emit("playing");
    expect(bench.created).toHaveLength(2);
    expect(bench.created[0]).not.toBe(bench.created[1]);
    expect(bench.player.snapshot().elements).toBe(2);
    // Nothing is ever appended: the only way this module reaches a document is
    // the injected factory, and it is called exactly twice.
    expect(bench.created.every((element) => element.src.startsWith("blob:tera/"))).toBe(true);
  });

  it("plays one element at unity alone", async () => {
    const bench = harness();
    await bench.player.play(request(CANDIDATE));
    expect(bench.created).toHaveLength(1);
    expect(bench.created[0].volume).toBe(1);
    expect(bench.created[0].currentTime).toBe(0);
    expect(bench.created[0].src).toBe(bench.urls[0]);
  });

  it("plays both elements at the one fixed comparison gain", async () => {
    const bench = harness();
    await bench.player.play(request(CANDIDATE, "comparison"));
    expect(bench.created).toHaveLength(2);
    for (const element of bench.created) {
      expect(element.volume).toBe(10 ** (AUDITION_COMPARISON_GAIN_DB / 20));
      expect(element.currentTime).toBe(0);
    }
    expect(bench.urls).toHaveLength(2);
  });
});

describe("starting", () => {
  it("is the element's own playing event, not the play() promise", async () => {
    const bench = harness();
    const running = bench.player.play(request(CANDIDATE));
    await running;
    expect(bench.player.snapshot().status).toBe("loading");
    expect(bench.events).toHaveLength(0);
    bench.advance(120);
    bench.created[0].emit("playing");
    expect(bench.player.snapshot().status).toBe("playing");
    expect(bench.events).toHaveLength(1);
    expect(bench.events[0]).toMatchObject({ track: CANDIDATE, mode: "candidate", latencyMs: 120 });
  });

  it("reports a refused play as blocked, and never as an audition", async () => {
    const bench = harness({ refusePlay: true });
    await bench.player.play(request(CANDIDATE));
    expect(bench.player.snapshot().status).toBe("blocked");
    expect(bench.player.snapshot().errorCode).toBe("playback_blocked");
    expect(bench.events).toHaveLength(0);
    // The refused element is released rather than left holding a URL.
    expect(bench.player.snapshot().liveUrls).toEqual([]);
    expect(bench.revoked).toEqual(["blob:tera/1"]);
  });

  it("reports an element error as decode_failed, and never as an audition", async () => {
    const bench = harness();
    await bench.player.play(request(CANDIDATE));
    bench.created[0].emit("error");
    expect(bench.player.snapshot().errorCode).toBe("decode_failed");
    expect(bench.player.snapshot().status).toBe("stopped");
    expect(bench.events).toHaveLength(0);
    expect(bench.player.snapshot().liveUrls).toEqual([]);
    expect(bench.revoked).toEqual(["blob:tera/1"]);
  });

  it("carries a source refusal's own code through", async () => {
    const bench = harness({
      load: async () => {
        throw Object.assign(new Error("gone"), { code: "content_mismatch" });
      },
    });
    await bench.player.play(request(CANDIDATE));
    expect(bench.player.snapshot().errorCode).toBe("content_mismatch");
    expect(bench.created).toHaveLength(0);
  });
});

describe("switching", () => {
  it("keeps only the newest request, revoking the two it abandoned", async () => {
    const bench = harness();
    // Three presses without awaiting any of them, as a producer clicking fast.
    const first = bench.player.play(request(CANDIDATE));
    const second = bench.player.play(request(OTHER));
    const third = bench.player.play(request(CANDIDATE, "comparison"));
    await Promise.all([first, second, third]);
    expect(bench.player.snapshot().requestSeq).toBe(3);
    // Only the newest request loaded anything.
    expect(bench.urls).toHaveLength(2);
    expect(bench.created).toHaveLength(2);
    bench.created[0].emit("playing");
    expect(bench.events).toHaveLength(1);
    expect(bench.player.snapshot().status).toBe("playing");
    expect(bench.statuses.filter((status) => status === "playing")).toHaveLength(1);
  });

  it("stops the sounding element before starting the next one", async () => {
    const bench = harness();
    await bench.player.play(request(CANDIDATE));
    bench.created[0].emit("playing");
    const firstUrl = bench.created[0].src;
    await bench.player.play(request(OTHER));
    expect(bench.revoked).toContain(firstUrl);
    // One element, reused: it is paused and reloaded before it is given the new
    // source, so the two candidates cannot overlap.
    expect(bench.created[0].calls).toEqual(["play", "pause", "load", "play"]);
    expect(bench.created[0].src).toBe(bench.urls[1]);
    expect(bench.player.snapshot().playing).toBeNull();
  });

  it("ignores a playing event once a newer request has taken the sequence", async () => {
    const pending: ((bytes: ArrayBuffer) => void)[] = [];
    const bench = harness({
      load: () =>
        new Promise<ArrayBuffer>((resolve) => {
          pending.push(resolve);
        }),
    });
    const first = bench.player.play(request(CANDIDATE));
    pending[0](new Uint8Array([1]).buffer);
    await first;
    bench.created[0].emit("playing");
    expect(bench.events).toHaveLength(1);
    // A second press releases the element and is still loading its own source:
    // a `playing` event now belongs to a sequence nobody is waiting for.
    const switching = bench.player.play(request(OTHER));
    bench.created[0].emit("playing");
    expect(bench.events).toHaveLength(1);
    expect(bench.player.snapshot().status).toBe("loading");
    pending[1](new Uint8Array([2]).buffer);
    await switching;
    bench.created[0].emit("playing");
    expect(bench.events).toHaveLength(2);
    expect(bench.events[1].track).toBe(OTHER);
  });

  it("abandons a load that resolves after a newer request", async () => {
    const pending: ((bytes: ArrayBuffer) => void)[] = [];
    const bench = harness({
      load: () =>
        new Promise<ArrayBuffer>((resolve) => {
          pending.push(resolve);
        }),
    });
    const slow = bench.player.play(request(CANDIDATE));
    const quick = bench.player.play(request(OTHER));
    // The newer request resolves first and becomes the playback.
    pending[1](new Uint8Array([2]).buffer);
    await quick;
    bench.created[0].emit("playing");
    expect(bench.events).toHaveLength(1);
    // The older one resolves afterwards and is abandoned outright: it creates
    // nothing, shows nothing and revokes nothing.
    pending[0](new Uint8Array([1]).buffer);
    await slow;
    expect(bench.urls).toHaveLength(1);
    expect(bench.revoked).toEqual([]);
    expect(bench.player.snapshot().track).toBe(OTHER);
  });
});

describe("stopping", () => {
  it("pauses, clears, loads, revokes and moves on", async () => {
    const bench = harness();
    await bench.player.play(request(CANDIDATE));
    bench.created[0].emit("playing");
    const url = bench.created[0].src;
    bench.player.stop();
    const snapshot = bench.player.snapshot();
    expect(snapshot.status).toBe("stopped");
    expect(snapshot.playing).toBeNull();
    expect(snapshot.liveUrls).toEqual([]);
    expect(bench.created[0].calls).toEqual(["play", "pause", "load"]);
    expect(bench.created[0].src).toBe("");
    expect(bench.revoked).toEqual([url]);
    expect(snapshot.requestSeq).toBe(2);
  });

  it("leaves nothing alive when it is called twice", async () => {
    const bench = harness();
    await bench.player.play(request(CANDIDATE, "comparison"));
    bench.player.stop();
    bench.player.stop();
    expect(bench.player.snapshot().liveUrls).toEqual([]);
    expect(bench.revoked).toHaveLength(2);
    expect(new Set(bench.revoked).size).toBe(2);
  });

  it("emits nothing after a stop that races a live request", async () => {
    const bench = harness();
    await bench.player.play(request(CANDIDATE));
    bench.player.stop();
    bench.created[0].emit("playing");
    expect(bench.events).toHaveLength(0);
    expect(bench.player.snapshot().status).toBe("stopped");
  });
});
