/**
 * The kick picker (issue #32).
 *
 * One bounded kick-role list, spent from the library route #31 already uses: it
 * asks for a single page of `role=kick` per interaction and never paged its own
 * way through the library -- the browse screen owns that.
 */

import { useEffect, useState } from "react";

import { useGetSamplesQuery } from "../palette/api/libraryApi";
import type { SampleQueryArgs } from "../palette/api/types";

const KICK_QUERY: SampleQueryArgs = { roles: ["kick"], text: null, limit: 50, cursor: null };

export interface KickPickerProps {
  open: boolean;
  selectedSampleId: string | null;
  onSelect(sampleId: string): void;
  onClose(): void;
}

export function KickPicker({ open, selectedSampleId, onSelect, onClose }: KickPickerProps) {
  const [armed, setArmed] = useState(false);
  const query = useGetSamplesQuery(KICK_QUERY, { skip: !open });

  useEffect(() => {
    if (!open) {
      setArmed(false);
      return;
    }
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        onClose();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  if (!open) {
    return null;
  }
  const rows = query.data?.items ?? [];
  return (
    <div
      className="picker"
      data-testid="kick-picker"
      role="dialog"
      aria-label="Choose a kick"
      data-armed={armed ? "true" : "false"}
    >
      <p className="picker__title">Choose a kick</p>
      {query.isError ? (
        <p data-testid="kick-picker-error">The library could not be read. Retry from the library panel.</p>
      ) : null}
      {!query.isError && query.isSuccess && rows.length === 0 ? (
        <p data-testid="kick-picker-empty">No kicks are in the library yet. Import a folder first.</p>
      ) : null}
      <ul className="picker__rows">
        {rows.map((item) => (
          <li key={item.sample_id}>
            <button
              type="button"
              data-testid="kick-picker-option"
              data-sample-id={item.sample_id}
              aria-pressed={item.sample_id === selectedSampleId}
              onFocus={() => setArmed(true)}
              onKeyDown={(event) => {
                if (event.key === "Enter") {
                  onSelect(item.sample_id);
                }
              }}
              onClick={() => onSelect(item.sample_id)}
            >
              {item.file_name}
              {item.file_status === "present" ? "" : ` (${item.file_status})`}
            </button>
          </li>
        ))}
      </ul>
      <button type="button" data-testid="kick-picker-close" onClick={onClose}>
        Close
      </button>
    </div>
  );
}
