/**
 * One ranked candidate as a card (issue #33).
 *
 * Presentational: cards in, callbacks out, no store and no request. Every score
 * it shows is the payload's own value, rendered as two separately labeled
 * elements — compatibility and confidence are never combined into one number or
 * one bar. It renders no `similarity` at all, so a candidate with a known
 * similarity and its twin with `similarity: null` produce identical markup, and
 * retrieval similarity is never presented as a score or a reason to pick.
 */

import type { CardModel } from "./cards";
import { formatPercent, type CardState } from "./cards";
import { AuditionControls } from "../audition/AuditionControls";

export interface RecommendationCardProps {
  card: CardModel;
  /** This card's action state, from the slice; `idle` when nothing was asked. */
  state: CardState;
  errorCode: string | null;
  retryable: boolean;
  onSelect(candidateId: string): void;
  onReject(candidateId: string): void;
  onRetryAction(candidateId: string): void;
}

/** The copy of one action failure, always naming which half failed. */
function actionErrorCopy(state: CardState, code: string | null): string {
  if (state === "unrecorded") {
    return `Selected, but the event was not recorded (${code ?? "unknown"}).`;
  }
  return `This action was refused (${code ?? "unknown"}).`;
}

export function RecommendationCard({
  card,
  state,
  errorCode,
  retryable,
  onSelect,
  onReject,
  onRetryAction,
}: RecommendationCardProps) {
  // The saved marker is the batch's own selected bass, or this card's own
  // successful write; nothing else in the client may claim a selection.
  const selected = card.selectedBass || state === "saved";
  const pending = state === "pending";
  const failed = state === "error" || state === "unrecorded";
  return (
    <li
      className="rec-card"
      data-testid="recommendation-card"
      data-rank={card.rank}
      data-candidate-id={card.candidateId}
      data-compatibility={String(card.compatibility)}
      data-confidence={String(card.confidence)}
      data-uncertain={card.uncertain}
      data-selected={selected}
      data-jev={card.jevPresent ? "present" : "none"}
      data-state={state}
    >
      <p className="rec-card__label" data-testid="card-label">
        {card.label}
      </p>
      <p className="rec-card__rank" data-testid="card-rank">
        Rank {card.rank}
      </p>
      <p className="rec-card__score" data-testid="card-compatibility" data-value={card.compatibility}>
        Compatibility <span className="rec-card__percent">{formatPercent(card.compatibility)}%</span>
      </p>
      <p className="rec-card__score" data-testid="card-confidence" data-value={card.confidence}>
        Confidence <span className="rec-card__percent">{formatPercent(card.confidence)}%</span>
      </p>
      <p className="rec-card__jev" data-testid="card-jev" data-jev={card.jevPresent ? "present" : "none"}>
        {card.jevPresent ? "Jev judgments present" : "No Jev judgment — measured rules only"}
      </p>
      {card.uncertain ? (
        <p className="rec-card__uncertain" data-testid="card-uncertain" data-uncertain="true">
          Uncertain — the evidence behind this candidate is thin
        </p>
      ) : null}
      <AuditionControls candidateId={card.candidateId} />
      <div className="rec-card__actions">
        <button
          type="button"
          data-testid="card-select"
          data-state={state}
          disabled={pending}
          onClick={() => onSelect(card.candidateId)}
        >
          {state === "saved" ? "Selected" : "Select"}
        </button>
        <button
          type="button"
          data-testid="card-reject"
          disabled={pending || selected}
          onClick={() => onReject(card.candidateId)}
        >
          {state === "rejected" ? "Rejected" : "Reject"}
        </button>
      </div>
      {failed ? (
        <p
          className="rec-card__error"
          data-testid="card-action-error"
          data-error-code={errorCode ?? ""}
          data-retryable={retryable}
        >
          {actionErrorCopy(state, errorCode)}
          {retryable ? (
            <button
              type="button"
              data-testid="card-action-retry"
              onClick={() => onRetryAction(card.candidateId)}
            >
              Retry
            </button>
          ) : null}
        </p>
      ) : null}
    </li>
  );
}
