/**
 * The audition's keyboard bindings (issue #34).
 *
 * The mapping is by `event.code`, so it is the physical key and not the layout's
 * letter. The handler belongs to the controls element alone and never to
 * `document` or `window`, so an audition cannot collide with the search field
 * #31 owns: a key pressed in a text entry is left completely alone, and a space
 * pressed on one of the control's own buttons is left to the button, which
 * activates natively and would otherwise toggle twice.
 *
 * `preventDefault()` is called for exactly the keys this module handles, and for
 * no other key: an unhandled key is returned as null without being consumed.
 */

/** What a binding asks the player to do; the control is the only dispatcher. */
export const AUDITION_COMMANDS = ["toggle", "toggle-comparison", "replay", "stop"] as const;
export type AuditionCommand = (typeof AUDITION_COMMANDS)[number];

/** The four bindings, as the control's visible legend shows them. */
export const AUDITION_KEYS = [
  { code: "Space", key: "Space", command: "toggle", action: "play or stop this candidate" },
  { code: "KeyC", key: "C", command: "toggle-comparison", action: "compare with the selected kick" },
  { code: "KeyR", key: "R", command: "replay", action: "replay from the start" },
  { code: "Escape", key: "Esc", command: "stop", action: "stop and release" },
] as const satisfies readonly {
  code: string;
  key: string;
  command: AuditionCommand;
  action: string;
}[];

/** What a key event has to expose; a real `KeyboardEvent` satisfies it. */
export interface AuditionKeyEvent {
  code: string;
  repeat?: boolean;
  target?: unknown;
  preventDefault(): void;
}

const BINDINGS: Record<string, AuditionCommand> = {
  Space: "toggle",
  KeyC: "toggle-comparison",
  KeyR: "replay",
  Escape: "stop",
};

const TEXT_ENTRY_TAGS = ["input", "textarea", "select"] as const;

function tagNameOf(node: unknown): string | null {
  if (typeof node !== "object" || node === null) {
    return null;
  }
  const tag = (node as { tagName?: unknown }).tagName;
  return typeof tag === "string" ? tag.toLowerCase() : null;
}

/**
 * Whether the event belongs to something that takes text.
 *
 * Structural rather than `instanceof`, so the module needs no DOM: a real element
 * answers `isContentEditable` for its whole subtree, and the attribute is read as
 * the fallback a test double can supply.
 */
export function isTextEntry(target: unknown): boolean {
  const tag = tagNameOf(target);
  if (tag !== null && (TEXT_ENTRY_TAGS as readonly string[]).includes(tag)) {
    return true;
  }
  if (typeof target !== "object" || target === null) {
    return false;
  }
  if ((target as { isContentEditable?: unknown }).isContentEditable === true) {
    return true;
  }
  const read = (target as { getAttribute?: unknown }).getAttribute;
  if (typeof read === "function") {
    const value = (read as (name: string) => unknown).call(target, "contenteditable");
    return typeof value === "string" && value.toLowerCase() !== "false";
  }
  return false;
}

/** Whether the target is a button, or sits inside one. */
export function isInsideButton(target: unknown): boolean {
  let node: unknown = target;
  for (let depth = 0; depth < 32 && typeof node === "object" && node !== null; depth += 1) {
    if (tagNameOf(node) === "button") {
      return true;
    }
    node = (node as { parentElement?: unknown }).parentElement;
  }
  return false;
}

/**
 * The command one key event asks for, or null for a key this control ignores.
 *
 * Null means the event was not consumed: a held key, a key in a text entry, a
 * space on one of the control's own buttons, and every unbound key all return
 * null and call nothing.
 */
export function commandForAuditionKey(event: AuditionKeyEvent): AuditionCommand | null {
  if (event.repeat === true) {
    return null;
  }
  if (isTextEntry(event.target)) {
    return null;
  }
  const command = BINDINGS[event.code];
  if (command === undefined) {
    return null;
  }
  if (event.code === "Space" && isInsideButton(event.target)) {
    // The button's own activation is the toggle; handling the key as well would
    // toggle twice for one press.
    return null;
  }
  event.preventDefault();
  return command;
}
