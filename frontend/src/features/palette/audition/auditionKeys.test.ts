/**
 * The keyboard contract, including the keys it must leave alone (issue #34).
 *
 * Each binding is checked, plus the four cases that must not be handled: a held
 * key, a key pressed in a text entry, a space on one of the control's own
 * buttons, and an unbound key. A handled key must call `preventDefault()`; an
 * ignored key must not.
 */

import { describe, expect, it } from "vitest";

import {
  AUDITION_COMMANDS,
  AUDITION_KEYS,
  commandForAuditionKey,
  isInsideButton,
  isTextEntry,
} from "./auditionKeys";

function press(code: string, options: { repeat?: boolean; target?: unknown } = {}) {
  const calls: string[] = [];
  const command = commandForAuditionKey({
    code,
    repeat: options.repeat,
    target: options.target,
    preventDefault: () => calls.push("preventDefault"),
  });
  return { command, prevented: calls.length === 1 };
}

/** A node chain as the DOM would hand it over, without needing one. */
function element(tagName: string, parent: unknown = null, extra: object = {}) {
  return { tagName, parentElement: parent, ...extra };
}

describe("commandForAuditionKey", () => {
  it("maps each documented binding to its command", () => {
    expect(press("Space").command).toBe("toggle");
    expect(press("KeyC").command).toBe("toggle-comparison");
    expect(press("KeyR").command).toBe("replay");
    expect(press("Escape").command).toBe("stop");
  });

  it("consumes exactly the keys it handles", () => {
    for (const binding of AUDITION_KEYS) {
      const result = press(binding.code);
      expect(result.command).toBe(binding.command);
      expect(result.prevented).toBe(true);
    }
    for (const code of ["KeyA", "Enter", "ArrowDown", "F5", "Tab", "KeyV"]) {
      const result = press(code);
      expect(result.command).toBeNull();
      expect(result.prevented).toBe(false);
    }
  });

  it("ignores a held key", () => {
    for (const binding of AUDITION_KEYS) {
      const result = press(binding.code, { repeat: true });
      expect(result.command).toBeNull();
      expect(result.prevented).toBe(false);
    }
  });

  it("ignores a key pressed in something that takes text", () => {
    for (const target of [
      element("input"),
      element("textarea"),
      element("select"),
      element("div", null, { isContentEditable: true }),
      element("div", null, {
        getAttribute: (name: string) => (name === "contenteditable" ? "" : null),
      }),
    ]) {
      expect(isTextEntry(target)).toBe(true);
      for (const binding of AUDITION_KEYS) {
        const result = press(binding.code, { target });
        expect(result.command).toBeNull();
        expect(result.prevented).toBe(false);
      }
    }
  });

  it("still handles a key pressed on a non-text control", () => {
    const target = element("div");
    expect(isTextEntry(target)).toBe(false);
    expect(press("KeyC", { target }).command).toBe("toggle-comparison");
    expect(press("Escape", { target }).command).toBe("stop");
  });

  it("leaves space to a button's own activation", () => {
    const button = element("button");
    const insideButton = element("span", button);
    expect(isInsideButton(button)).toBe(true);
    expect(isInsideButton(insideButton)).toBe(true);
    expect(isInsideButton(element("div"))).toBe(false);
    const onButton = press("Space", { target: button });
    expect(onButton.command).toBeNull();
    expect(onButton.prevented).toBe(false);
    const inButton = press("Space", { target: insideButton });
    expect(inButton.command).toBeNull();
    expect(inButton.prevented).toBe(false);
    // Every other binding is still handled on a focused button.
    expect(press("KeyR", { target: button }).command).toBe("replay");
  });

  it("publishes one legend entry per command", () => {
    expect(AUDITION_KEYS).toHaveLength(AUDITION_COMMANDS.length);
    expect(new Set(AUDITION_KEYS.map((entry) => entry.command))).toEqual(
      new Set(AUDITION_COMMANDS),
    );
    for (const entry of AUDITION_KEYS) {
      expect(entry.key.length).toBeGreaterThan(0);
      expect(entry.action.length).toBeGreaterThan(0);
    }
  });
});
