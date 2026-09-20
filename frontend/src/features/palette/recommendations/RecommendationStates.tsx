/**
 * Every visible state of the recommendation panel (issue #33).
 *
 * One component per closed state, so the panel picks one and renders it rather
 * than branching inline, and so a render test can supply the exact payload each
 * state is defined by. The badges and notes that may sit *beside* a state —
 * degraded, the exclusion tally, the unscored note, the alternatives — are
 * separate components for the same reason: they never replace the state.
 */

import { RECOMMENDATION_BUDGET_SECONDS, cardLabel } from "./cards";
import type { AlternativeItem, EmptyKind, ExclusionRow } from "./cards";

/** `#62` owns what happens to a candidate that could not be scored. */
export const UNSCORED_ISSUE_URL = "https://github.com/gmphto/tera/issues/62";

export function IdleState() {
  return (
    <section className="rec-state" data-testid="recommendations-idle" data-reason="no_kick">
      <p className="rec-state__copy">Select a kick to see ranked bass candidates.</p>
    </section>
  );
}

export function LoadingState({ slow, onCancel }: { slow: boolean; onCancel(): void }) {
  return (
    <section
      className="rec-state"
      data-testid="recommendations-loading"
      data-slow={slow}
      role="status"
      aria-live="polite"
    >
      <p className="rec-state__copy">
        Ranking bass candidates
        {slow
          ? ` — this run can take up to ${RECOMMENDATION_BUDGET_SECONDS} seconds.`
          : "…"}
      </p>
      <div className="rec-state__actions">
        <button type="button" data-testid="recommendations-cancel" onClick={onCancel}>
          Cancel
        </button>
      </div>
    </section>
  );
}

export function CancelledState({ onRetry }: { onRetry(): void }) {
  return (
    <section className="rec-state" data-testid="recommendations-cancelled">
      <p className="rec-state__copy">The run was cancelled. Nothing was published.</p>
      <div className="rec-state__actions">
        <button type="button" data-testid="recommendations-retry" onClick={onRetry}>
          Retry
        </button>
      </div>
    </section>
  );
}

export function StaleState({
  requestedRevision,
  currentRevision,
  onRetry,
}: {
  requestedRevision: number;
  currentRevision: number | null;
  onRetry(): void;
}) {
  return (
    <section
      className="rec-state"
      data-testid="recommendations-stale"
      data-run-revision={requestedRevision}
      data-current-revision={currentRevision ?? ""}
      role="alert"
    >
      <p className="rec-state__copy">
        The palette changed while this run was in flight: it ranked revision {requestedRevision}
        {currentRevision === null ? "" : `, and the palette is now at ${currentRevision}`}. Nothing
        from the old run is shown.
      </p>
      <div className="rec-state__actions">
        <button type="button" data-testid="recommendations-retry" onClick={onRetry}>
          Retry
        </button>
      </div>
    </section>
  );
}

export function UnavailableState({ phase, onRetry }: { phase: string; onRetry(): void }) {
  return (
    <section
      className="rec-state"
      data-testid="recommendations-unavailable"
      data-service-phase={phase}
    >
      <p className="rec-state__copy">
        The local service is not reachable ({phase}). Its status panel is above.
      </p>
      <div className="rec-state__actions">
        <button type="button" data-testid="recommendations-retry" onClick={onRetry}>
          Retry
        </button>
      </div>
    </section>
  );
}

export function ErrorState({
  source,
  code,
  retryable,
  onRetry,
}: {
  source: "service" | "client";
  code: string;
  retryable: boolean;
  onRetry(): void;
}) {
  return (
    <section
      className="rec-state rec-state--error"
      data-testid="recommendations-error"
      data-error-source={source}
      data-error-code={code}
      data-retryable={retryable}
      role="alert"
    >
      <p className="rec-state__copy">
        {source === "client"
          ? `This response could not be read (${code}). Nothing is shown from it.`
          : `The service refused this run (${code}).`}
      </p>
      {retryable ? (
        <div className="rec-state__actions">
          <button type="button" data-testid="recommendations-retry" onClick={onRetry}>
            Retry
          </button>
        </div>
      ) : null}
    </section>
  );
}

/** One exclusion code's tally; its label is the client's table or the code. */
export function ExclusionTally({ rows }: { rows: ExclusionRow[] }) {
  return (
    <ul className="rec-exclusions" data-testid="recommendations-exclusions">
      {rows.map((row) => (
        <li
          key={row.code}
          data-testid="exclusion-row"
          data-exclusion-code={row.code}
          data-exclusion-count={row.count}
        >
          {row.label} — {row.count}
        </li>
      ))}
    </ul>
  );
}

export function ExclusionsSummary({ count }: { count: number }) {
  return (
    <p className="rec-exclusions__summary" data-testid="exclusions-summary" data-excluded-count={count}>
      {count} candidate{count === 1 ? "" : "s"} excluded before ranking.
    </p>
  );
}

export function EmptyState({
  kind,
  limitReason,
  exclusions,
  unscored,
}: {
  kind: EmptyKind;
  limitReason: string | null;
  exclusions: ExclusionRow[];
  unscored: number;
}) {
  return (
    <section
      className="rec-state"
      data-testid="recommendations-empty"
      data-empty-kind={kind}
      {...(kind === "no_candidates" && limitReason !== null
        ? { "data-limit-reason": limitReason }
        : {})}
    >
      <p className="rec-state__copy">
        {kind === "no_candidates"
          ? "No bass candidate is eligible for this palette yet."
          : kind === "all_excluded"
            ? "Every candidate was excluded before ranking."
            : "No candidate could be scored, so there is nothing to rank."}
      </p>
      {kind === "all_excluded" ? <ExclusionTally rows={exclusions} /> : null}
      {unscored > 0 ? <UnscoredNote count={unscored} /> : null}
    </section>
  );
}

/** The count of candidates #28 returned no record for, and never a card. */
export function UnscoredNote({ count }: { count: number }) {
  return (
    <p className="rec-note" data-testid="unscored-note" data-unscored-count={count}>
      {count} candidate{count === 1 ? "" : "s"} could not be scored and {count === 1 ? "is" : "are"}{" "}
      not shown.{" "}
      <a href={UNSCORED_ISSUE_URL} rel="noreferrer" target="_blank">
        Track it
      </a>
    </p>
  );
}

/** What the run lacked; it never claims a Jev judgment the run did not make. */
export function DegradedBadge({ mode, jevStatus }: { mode: string; jevStatus: string }) {
  const copy =
    jevStatus === "jev_partial"
      ? "Some Jev dimensions are missing for this run, so it ranks on measured rules and the judgments it has."
      : "Measured rules only — no Jev judgment was available for this run.";
  return (
    <p
      className="rec-badge"
      data-testid="recommendations-degraded"
      data-mode={mode}
      data-jev-status={jevStatus}
    >
      {copy}
    </p>
  );
}

/** The API's own alternatives, each with a control that moves focus to its card. */
export function AlternativesBlock({
  items,
  onFocus,
}: {
  items: AlternativeItem[];
  onFocus(candidateId: string): void;
}) {
  if (items.length === 0) {
    return null;
  }
  return (
    <section className="rec-alternatives" data-testid="recommendations-alternatives">
      <h3 className="rec-alternatives__title">Alternatives</h3>
      <ul className="rec-alternatives__list">
        {items.map((item) => (
          <li
            key={item.candidateId}
            data-testid="alternative-item"
            data-candidate-id={item.candidateId}
            data-rank={item.rank}
            data-compatibility={String(item.compatibility)}
            data-confidence={String(item.confidence)}
          >
            <span>
              Rank {item.rank} — {cardLabel(item.candidateId)}
            </span>
            <button
              type="button"
              data-testid="alternative-focus"
              data-candidate-id={item.candidateId}
              onClick={() => onFocus(item.candidateId)}
            >
              Show card
            </button>
          </li>
        ))}
      </ul>
    </section>
  );
}
