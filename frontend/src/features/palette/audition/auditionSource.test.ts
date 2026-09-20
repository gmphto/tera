/**
 * The shell boundary and its closed error vocabulary (issue #34).
 *
 * Every code criterion 7 names is produced here, from a fake shell, so the
 * mapping is a test rather than a claim: the refusal the shell sends, the
 * client's own file-name and file-status pre-checks, and an answer that is not
 * bytes at all. `decode_failed` and `playback_blocked` belong to the player and
 * are covered there.
 */

import { describe, expect, it } from "vitest";

import {
  AUDITION_ERROR_CODES,
  type AuditionErrorCode,
} from "./auditionTypes";
import {
  AUDITION_ROOT_LIMIT,
  MAX_AUDITION_BYTES,
  AuditionSourceError,
  auditionCodeOf,
  auditionErrorSentence,
  auditionErrorState,
  createAuditionSource,
  inspectFileName,
  type AuditionShell,
} from "./auditionSource";

const TRACK = { sampleId: `sha256:${"a".repeat(64)}`, fileName: "candidate-001.wav" };

interface Call {
  command: string;
  args: Record<string, unknown> | undefined;
}

function shellOf(reply: (command: string) => unknown): { shell: AuditionShell; calls: Call[] } {
  const calls: Call[] = [];
  return {
    calls,
    shell: {
      async invoke<T>(command: string, args?: Record<string, unknown>): Promise<T> {
        calls.push({ command, args });
        const answer = reply(command);
        // A refusal is an Error or a `{code}` envelope; anything else is payload.
        const refused =
          answer instanceof Error ||
          (typeof answer === "object" &&
            answer !== null &&
            typeof (answer as { code?: unknown }).code === "string");
        if (refused) {
          throw answer;
        }
        return answer as T;
      },
    },
  };
}

async function codeOf(run: () => Promise<unknown>): Promise<AuditionErrorCode | "none"> {
  try {
    await run();
    return "none";
  } catch (error) {
    expect(error).toBeInstanceOf(AuditionSourceError);
    return (error as AuditionSourceError).code;
  }
}

describe("the error vocabulary", () => {
  it("gives every code exactly one sentence and one state", () => {
    const states = new Set<string>();
    for (const code of AUDITION_ERROR_CODES) {
      const sentence = auditionErrorSentence(code);
      expect(sentence.length).toBeGreaterThan(10);
      expect(sentence).not.toContain("undefined");
      const state = auditionErrorState(code);
      expect(["blocked", "stopped", "not-recorded"]).toContain(state);
      states.add(state);
    }
    // All three documented outcomes are used, so none is a placeholder.
    expect(states).toEqual(new Set(["blocked", "stopped", "not-recorded"]));
    expect(auditionErrorState("not_recorded")).toBe("not-recorded");
    expect(auditionErrorState("decode_failed")).toBe("stopped");
    expect(auditionErrorState("playback_blocked")).toBe("blocked");
  });

  it("reads a code out of a refusal, and falls back to the shell being absent", () => {
    expect(auditionCodeOf({ code: "content_mismatch", message: "x" })).toBe("content_mismatch");
    expect(auditionCodeOf("too_large")).toBe("too_large");
    expect(auditionCodeOf(new Error("boom"))).toBe("shell_unavailable");
    expect(auditionCodeOf(undefined)).toBe("shell_unavailable");
    expect(auditionCodeOf({ code: "not-a-code" })).toBe("shell_unavailable");
  });

  it("never shows a sentence that claims a loudness match", () => {
    for (const code of AUDITION_ERROR_CODES) {
      const sentence = auditionErrorSentence(code).toLowerCase();
      expect(sentence).not.toContain("loudness");
      expect(sentence).not.toContain("normalis");
      expect(sentence).not.toContain("level match");
      expect(sentence).not.toContain("same level");
    }
    // The one sentence that says "match" is about a file's bytes, not a level.
    expect(auditionErrorSentence("content_mismatch")).toContain("bytes");
  });
});

describe("the client's file-name rule", () => {
  it("accepts one plain .wav component in any case", () => {
    expect(inspectFileName("kick.wav")).toBeNull();
    expect(inspectFileName("Kick One Shot.WAV")).toBeNull();
    expect(inspectFileName("a.b.wav")).toBeNull();
  });

  it("refuses anything that is not a single component", () => {
    for (const name of ["", "..", ".", "a/b.wav", "a\\b.wav", "C:a.wav", "kick\u0000.wav",
                        "kick\n.wav", "x".repeat(256)]) {
      expect(inspectFileName(name)).toBe("invalid_file_name");
    }
  });

  it("refuses an extension the shell will not play", () => {
    expect(inspectFileName("kick.mp3")).toBe("unsupported_extension");
    expect(inspectFileName("looperman-l-3056971-0164649-jugu-bouncy-drums.wav")).toBeNull();
  });
});

describe("registering a root", () => {
  it("returns the shell's id and label", async () => {
    const { shell, calls } = shellOf(() => ({ rootId: "root-001", rootLabel: "library" }));
    const source = createAuditionSource(shell);
    await expect(source.registerAuditionRoot("C:/tmp/library")).resolves.toEqual({
      rootId: "root-001",
      rootLabel: "library",
    });
    expect(calls).toEqual([{ command: "register_audition_root", args: { path: "C:/tmp/library" } }]);
  });

  it("maps a refusal by its code, and a shapeless answer to root_invalid", async () => {
    // Tauri rejects with what the command returned: a `{code}` envelope, a bare
    // code string, or an Error whose message is one. A foreign failure has no
    // code and means the shell itself is not usable.
    const coded = shellOf(() => ({ code: "root_limit_reached" }));
    expect(await codeOf(() => createAuditionSource(coded.shell).registerAuditionRoot("C:/x")))
      .toBe("root_limit_reached");
    const asError = shellOf(() => new Error("root_limit_reached"));
    expect(await codeOf(() => createAuditionSource(asError.shell).registerAuditionRoot("C:/x")))
      .toBe("root_limit_reached");
    const foreign = shellOf(() => new TypeError("window.__TAURI_INTERNALS__ is undefined"));
    expect(await codeOf(() => createAuditionSource(foreign.shell).registerAuditionRoot("C:/x")))
      .toBe("shell_unavailable");
    const shapeless = shellOf(() => ({ rootId: "root-001" }));
    expect(await codeOf(() => createAuditionSource(shapeless.shell).registerAuditionRoot("C:/x")))
      .toBe("root_invalid");
    expect(await codeOf(() => createAuditionSource(shellOf(() => null).shell)
      .registerAuditionRoot("   "))).toBe("root_invalid");
  });

  it("forgets a root through its own command", async () => {
    const { shell, calls } = shellOf(() => null);
    await createAuditionSource(shell).releaseAuditionSource("root-001");
    expect(calls).toEqual([{ command: "forget_audition_root", args: { rootId: "root-001" } }]);
  });
});

describe("loading a source", () => {
  const bytes = new Uint8Array([82, 73, 70, 70]).buffer;

  it("asks for the track's id and basename, and returns the bytes unchanged", async () => {
    const { shell, calls } = shellOf(() => bytes);
    const loaded = await createAuditionSource(shell).loadAuditionSource({
      track: TRACK,
      fileStatus: "present",
    });
    expect(new Uint8Array(loaded)).toEqual(new Uint8Array(bytes));
    expect(calls).toEqual([{
      command: "read_audition_source",
      args: { sampleId: TRACK.sampleId, fileName: TRACK.fileName },
    }]);
  });

  it("forwards a root preference without inventing one", async () => {
    const { shell, calls } = shellOf(() => bytes);
    await createAuditionSource(shell).loadAuditionSource({ track: TRACK, rootId: "root-002" });
    expect(calls[0].args).toEqual({
      sampleId: TRACK.sampleId,
      fileName: TRACK.fileName,
      rootId: "root-002",
    });
    const bare = shellOf(() => bytes);
    await createAuditionSource(bare.shell).loadAuditionSource({ track: TRACK, rootId: null });
    expect("rootId" in (bare.calls[0].args ?? {})).toBe(false);
  });

  it("refuses before the shell when the library already knows the file is gone", async () => {
    const { shell, calls } = shellOf(() => bytes);
    for (const status of ["missing", "stale", "unknown"]) {
      expect(await codeOf(() => createAuditionSource(shell).loadAuditionSource({
        track: TRACK,
        fileStatus: status,
      }))).toBe("missing_file");
    }
    expect(calls).toEqual([]);
  });

  it("refuses a bad name before the shell", async () => {
    const { shell, calls } = shellOf(() => bytes);
    expect(await codeOf(() => createAuditionSource(shell).loadAuditionSource({
      track: { ...TRACK, fileName: "sub/dir/kick.wav" },
    }))).toBe("invalid_file_name");
    expect(await codeOf(() => createAuditionSource(shell).loadAuditionSource({
      track: { ...TRACK, fileName: "kick.mp3" },
    }))).toBe("unsupported_extension");
    expect(calls).toEqual([]);
  });

  it("maps every shell refusal to its own code", async () => {
    const cases: [AuditionErrorCode, unknown][] = [
      ["no_registered_root", { code: "no_registered_root" }],
      ["root_invalid", { code: "root_invalid" }],
      ["root_limit_reached", { code: "root_limit_reached" }],
      ["missing_file", { code: "missing_file" }],
      ["unreadable_file", { code: "unreadable_file" }],
      ["too_large", { code: "too_large" }],
      ["content_mismatch", { code: "content_mismatch" }],
      ["unsupported_extension", { code: "unsupported_extension" }],
      ["invalid_file_name", { code: "invalid_file_name" }],
      ["shell_unavailable", new TypeError("window.__TAURI_INTERNALS__ is undefined")],
    ];
    for (const [expected, refusal] of cases) {
      const { shell } = shellOf(() => refusal as Error);
      expect(await codeOf(() => createAuditionSource(shell).loadAuditionSource({ track: TRACK })))
        .toBe(expected);
    }
  });

  it("reports an answer that is not bytes as unreadable", async () => {
    for (const answer of ["not bytes", 42, null, { bytes: [] }]) {
      const { shell } = shellOf(() => answer);
      expect(await codeOf(() => createAuditionSource(shell).loadAuditionSource({ track: TRACK })))
        .toBe("unreadable_file");
    }
  });

  it("keeps the shell's limits as the shell documents them", () => {
    expect(AUDITION_ROOT_LIMIT).toBe(16);
    expect(MAX_AUDITION_BYTES).toBe(67_108_864);
  });
});
