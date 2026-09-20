/**
 * The list area and its closed set of states (issue #31).
 *
 * The container is always labelled with the filters the *service* echoed, so a
 * mismatch between what was asked and what was applied is visible rather than
 * silent. An unavailable analysis or a missing file is a row state and never
 * replaces the list with an empty state.
 */

import { recoveryFor, type LibraryError } from "../api/errors";
import type { SamplePage, SampleQueryArgs } from "../api/types";
import { emptyStateFor } from "../state/selectors";
import { SampleRow } from "./SampleRow";

export interface LibraryResultsProps {
  page: SamplePage | undefined;
  args: SampleQueryArgs;
  serviceReady: boolean;
  isFetching: boolean;
  isFirstLoad: boolean;
  error: LibraryError | null;
  staleSelection: boolean;
  selectedSampleId: string | null;
  onRetry(): void;
  onClearFilters(): void;
  onSelect(sampleId: string): void;
  onImport(): void;
}

function ErrorNote({ error, onRetry }: { error: LibraryError; onRetry(): void }) {
  return (
    <>
      {error.kind === "api" ? (
        <p className="panel__error" data-testid="library-error" data-kind="api" data-code={error.code}>
          {recoveryFor(error)}
        </p>
      ) : (
        <p
          className="panel__error"
          data-testid="library-error"
          data-kind="transport"
          data-reason={error.reason}
        >
          {recoveryFor(error)}
        </p>
      )}
      {error.kind === "api" && error.code === "database_unavailable" ? (
        <p data-testid="library-degraded" data-code="database_unavailable">
          The library database is unavailable while the service is up. See the service panel above.
        </p>
      ) : null}
      <button type="button" data-testid="library-retry" onClick={onRetry}>
        Retry
      </button>
    </>
  );
}

export function LibraryResults({
  page,
  args,
  serviceReady,
  isFetching,
  isFirstLoad,
  error,
  staleSelection,
  selectedSampleId,
  onRetry,
  onClearFilters,
  onSelect,
  onImport,
}: LibraryResultsProps) {
  if (!serviceReady) {
    return (
      <section className="panel" data-testid="library-browser-body">
        <p data-testid="library-service-unavailable">
          The library is not requested while the service is starting, unavailable or failed. See the
          service panel above.
        </p>
      </section>
    );
  }
  if (error !== null) {
    return (
      <section className="panel" data-testid="library-browser-body">
        <ErrorNote error={error} onRetry={onRetry} />
      </section>
    );
  }
  if (isFirstLoad || page === undefined) {
    return (
      <section className="panel" data-testid="library-browser-body">
        <p data-testid="library-loading" aria-busy="true">
          Loading samples…
        </p>
      </section>
    );
  }
  const empty = emptyStateFor(page, args.roles, args.text ?? "");
  return (
    <section className="panel" data-testid="library-browser-body">
      <div
        data-testid="library-results"
        data-query-text={page.query.text ?? ""}
        data-query-roles={page.query.roles.join(",")}
        data-busy={isFetching ? "true" : "false"}
      >
        <p className="results__count" aria-live="polite">
          {page.page.count} shown{page.page.has_more ? ", more available" : ""}
        </p>
        {empty === "empty" ? (
          <div data-testid="library-empty">
            <p>The library holds no samples yet.</p>
            <button type="button" data-testid="library-empty-import" onClick={onImport}>
              Import a folder
            </button>
          </div>
        ) : null}
        {empty === "no-matches" ? (
          <div data-testid="library-no-matches" data-query-text={page.query.text ?? ""} data-query-roles={page.query.roles.join(",")}>
            <p>No samples match these filters.</p>
            <button type="button" data-testid="library-clear-filters" onClick={onClearFilters}>
              Clear filters
            </button>
          </div>
        ) : null}
        {staleSelection ? (
          <p data-testid="selection-stale">
            The selected sample is no longer in the library. The selection was cleared.
          </p>
        ) : null}
        {page.items.length === 0 ? null : (
          <ul className="results__rows">
            {page.items.map((item) => (
              <SampleRow
                key={item.sample_id}
                item={item}
                selected={item.sample_id === selectedSampleId}
                onSelect={onSelect}
              />
            ))}
          </ul>
        )}
        {isFetching ? <span className="results__busy">Refreshing…</span> : null}
      </div>
    </section>
  );
}
