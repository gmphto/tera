/**
 * The song-context editor (issue #32).
 *
 * Three fields, each in one of three states. The panel shows the stored value
 * beside the unsaved draft so a producer can tell them apart, and a failed save
 * never renders as saved.
 */

import {
  CONTEXT_FIELDS,
  draftIsSavable,
  type ContextDraft,
  type ContextField,
  type FieldDraft,
} from "./state/contextDraft";
import type { PaletteDraftState } from "./state/paletteSlice";

const LABELS: Record<ContextField, string> = {
  tempo: "Tempo (BPM)",
  key: "Key",
  genre: "Genre",
};

const STORED_SENTENCE: Record<string, string> = {
  known: "Stored value",
  unknown: "Stored: explicitly unknown",
  unset: "Stored: not set",
};

export interface ContextEditorProps {
  stored: { tempo: { state: string }; key: { state: string }; genre: { state: string } } | null;
  draft: PaletteDraftState;
  saving: boolean;
  onField(field: ContextField, next: FieldDraft): void;
  onState(field: ContextField, state: FieldDraft["state"]): void;
  onSave(): void;
  onRetry(): void;
}

export function ContextEditor({
  stored,
  draft,
  saving,
  onField,
  onState,
  onSave,
  onRetry,
}: ContextEditorProps) {
  const values = draft.values;
  const dirty = draft.dirty || draft.saveState === "error";
  return (
    <section className="context" data-testid="palette-context" data-draft={dirty ? "dirty" : "clean"}>
      <h3 className="context__heading">Song context</h3>
      <p className="context__note">
        These values are declared by you, not measured. A value you enter is used by the tempo and
        key locks.
      </p>
      {CONTEXT_FIELDS.map((field) => {
        const fieldDraft = values?.[field] ?? { state: "unset" as const, value: "" };
        const storedState = stored?.[field]?.state ?? "unset";
        return (
          <div
            key={field}
            className="context__field"
            data-testid={`palette-context-${field}`}
            data-context-state={fieldDraft.state}
          >
            <label htmlFor={`context-${field}`}>{LABELS[field]}</label>
            <input
              id={`context-${field}`}
              type="text"
              value={fieldDraft.value}
              disabled={fieldDraft.state !== "known"}
              onChange={(event) => onField(field, { state: "known", value: event.target.value })}
            />
            <select
              aria-label={`${LABELS[field]} state`}
              value={fieldDraft.state}
              onChange={(event) => onState(field, event.target.value as FieldDraft["state"])}
            >
              <option value="known">declared</option>
              <option value="unknown">unknown</option>
              <option value="unset">not set</option>
            </select>
            <span className="context__stored">{STORED_SENTENCE[storedState] ?? storedState}</span>
          </div>
        );
      })}
      {draft.saveState === "error" ? (
        <div data-testid="palette-context-save-error">
          <p data-testid="palette-context-error" data-error-field={draft.errorField ?? ""}>
            {draft.errorCode === "revision_conflict"
              ? "The palette changed while you were editing. Your draft is kept; save again to apply it."
              : "The context was refused. Your draft is kept and nothing was saved."}
          </p>
          <button type="button" data-testid="palette-context-save-retry" onClick={onRetry}>
            Save again
          </button>
        </div>
      ) : null}
      <button
        type="button"
        data-testid="palette-context-save"
        disabled={saving || values === null || !draftIsSavable(values ?? emptyDraft)}
        onClick={onSave}
      >
        Save context
      </button>
    </section>
  );
}

const emptyDraft: ContextDraft = {
  tempo: { state: "unset", value: "" },
  key: { state: "unset", value: "" },
  genre: { state: "unset", value: "" },
};
