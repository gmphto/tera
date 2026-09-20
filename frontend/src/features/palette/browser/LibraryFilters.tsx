/**
 * The search, role and page-size controls (issue #31).
 *
 * Typing updates the visible text on every keystroke; the list queries only the
 * committed text, so a burst of keystrokes becomes one request.
 */

import {
  MAX_QUERY_LENGTH,
  PAGE_SIZE_OPTIONS,
  PALETTE_ROLES,
  type PaletteRole,
} from "../api/types";

const ROLE_LABELS: Record<PaletteRole, string> = {
  kick: "Kicks",
  bass: "Basses",
  "sub-bass": "Sub-basses",
};

export interface LibraryFiltersProps {
  text: string;
  roles: PaletteRole[];
  limit: number;
  onText(text: string): void;
  onRoles(roles: PaletteRole[]): void;
  onLimit(limit: number): void;
}

export function LibraryFilters({
  text,
  roles,
  limit,
  onText,
  onRoles,
  onLimit,
}: LibraryFiltersProps) {
  return (
    <div className="filters">
      <p className="filters__field">
        <label htmlFor="library-search-input">Search file names</label>
        <input
          id="library-search-input"
          type="search"
          data-testid="library-search"
          maxLength={MAX_QUERY_LENGTH}
          value={text}
          onChange={(event) => onText(event.target.value)}
        />
      </p>
      <fieldset className="filters__roles" data-testid="library-role-filter">
        <legend>Roles</legend>
        {PALETTE_ROLES.map((role) => (
          <label key={role} className="filters__role">
            <input
              type="checkbox"
              data-role={role}
              checked={roles.includes(role)}
              onChange={(event) =>
                onRoles(
                  event.target.checked
                    ? [...roles, role]
                    : roles.filter((selected) => selected !== role),
                )
              }
            />
            {ROLE_LABELS[role]}
          </label>
        ))}
      </fieldset>
      <p className="filters__field">
        <label htmlFor="library-page-size-select">Page size</label>
        <select
          id="library-page-size-select"
          data-testid="library-page-size"
          value={limit}
          onChange={(event) => onLimit(Number(event.target.value))}
        >
          {PAGE_SIZE_OPTIONS.map((option) => (
            <option key={option} value={option}>
              {option}
            </option>
          ))}
        </select>
      </p>
    </div>
  );
}
