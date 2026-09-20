/**
 * The ranked recommendation panel (issue #33).
 *
 * It renders exactly one state, chosen from the slice, and nothing from a run
 * that is not the active one. The panel holds no recommendation state of its
 * own: the request identity, the payloads and every card action live in
 * `recommendationsSlice`, and this file maps them onto the state components.
 *
 * The one thing it keeps locally is a clock, because "this run is slow" is about
 * elapsed time rather than about the payload: it starts when a request starts
 * and stops when one ends.
 */

import { useEffect, useRef, useState } from "react";

import { useAppDispatch, useAppSelector } from "../../../app/hooks";
import type { RootState } from "../../../app/store";
import {
  alternativeItems,
  cardModels,
  deriveRunNotice,
  emptyKind,
  exclusionRows,
  isDegraded,
  unscoredCount,
} from "./cards";
import { RecommendationCard } from "./RecommendationCard";
import {
  AlternativesBlock,
  CancelledState,
  DegradedBadge,
  EmptyState,
  ErrorState,
  ExclusionsSummary,
  IdleState,
  LoadingState,
  StaleState,
  UnavailableState,
  UnscoredNote,
} from "./RecommendationStates";
import {
  recommendationActions,
  recommendationRequest,
  recommendationsIdle,
  selectCardAction,
  selectRecommendationBatch,
  selectRecommendationError,
  selectRecommendationRun,
  selectRecommendationStale,
  selectRecommendationStatus,
} from "../state/recommendationsSlice";

/** How often the elapsed clock is read while a request is in flight. */
export const SLOW_NOTICE_TICK_MS = 1000;

export function RecommendationPanel() {
  const dispatch = useAppDispatch();
  const palette = useAppSelector((state: RootState) => state.palette);
  const status = useAppSelector(selectRecommendationStatus);
  const batch = useAppSelector(selectRecommendationBatch);
  const run = useAppSelector(selectRecommendationRun);
  const stale = useAppSelector(selectRecommendationStale);
  const error = useAppSelector(selectRecommendationError);
  const cardStates = useAppSelector((state: RootState) => state.recommendations);
  const servicePhase = useAppSelector((state: RootState) => state.service.snapshot?.phase ?? "unknown");

  const kickId = palette.items.kick?.sample_id ?? null;
  const paletteId = palette.paletteId;
  const revision = palette.revision;

  // One run per request identity. A change of kick, palette or revision aborts
  // the run in flight before the new one starts, so two runs never mix.
  useEffect(() => {
    if (paletteId === null || revision < 0 || kickId === null) {
      dispatch(recommendationsIdle());
      return;
    }
    void dispatch(
      recommendationActions.run(recommendationRequest({ paletteId, revision, kickId })),
    );
  }, [dispatch, paletteId, revision, kickId]);

  const startedAt = useRef<number | null>(null);
  const [, setTick] = useState(0);
  useEffect(() => {
    if (status !== "loading") {
      startedAt.current = null;
      return;
    }
    startedAt.current = Date.now();
    const timer = setInterval(() => setTick((value) => value + 1), SLOW_NOTICE_TICK_MS);
    return () => clearInterval(timer);
  }, [status, paletteId, revision, kickId]);
  const slow = startedAt.current !== null && deriveRunNotice(Date.now() - startedAt.current);

  const onRetry = () => {
    void dispatch(recommendationActions.retry());
  };
  const onCancel = () => {
    void dispatch(recommendationActions.cancel());
  };
  const focusCard = (candidateId: string) => {
    const card = document.querySelector(`[data-testid="recommendation-card"][data-candidate-id="${candidateId}"] button`);
    if (card instanceof HTMLElement) {
      card.focus();
    }
  };

  if (status === "idle") {
    return (
      <section className="rec-panel" data-testid="recommendations-panel" data-state="idle">
        <h2 className="rec-panel__title">Ranked bass candidates</h2>
        <IdleState />
      </section>
    );
  }

  if (status === "loading") {
    return (
      <section className="rec-panel" data-testid="recommendations-panel" data-state="loading">
        <h2 className="rec-panel__title">Ranked bass candidates</h2>
        <LoadingState slow={slow} onCancel={onCancel} />
      </section>
    );
  }

  if (status === "cancelled") {
    return (
      <section className="rec-panel" data-testid="recommendations-panel" data-state="cancelled">
        <h2 className="rec-panel__title">Ranked bass candidates</h2>
        <CancelledState onRetry={onRetry} />
      </section>
    );
  }

  if (status === "stale") {
    return (
      <section className="rec-panel" data-testid="recommendations-panel" data-state="stale">
        <h2 className="rec-panel__title">Ranked bass candidates</h2>
        <StaleState
          requestedRevision={stale?.requestedRevision ?? -1}
          currentRevision={stale?.currentRevision ?? null}
          onRetry={onRetry}
        />
      </section>
    );
  }

  if (status === "unavailable") {
    return (
      <section className="rec-panel" data-testid="recommendations-panel" data-state="unavailable">
        <h2 className="rec-panel__title">Ranked bass candidates</h2>
        <UnavailableState phase={servicePhase} onRetry={onRetry} />
      </section>
    );
  }

  if (status === "error" || batch === null || run === null) {
    return (
      <section className="rec-panel" data-testid="recommendations-panel" data-state="error">
        <h2 className="rec-panel__title">Ranked bass candidates</h2>
        <ErrorState
          source={error?.source ?? "service"}
          code={error?.code ?? "internal_error"}
          retryable={error?.retryable ?? false}
          onRetry={onRetry}
        />
      </section>
    );
  }

  const kind = emptyKind(run);
  const degraded = isDegraded(batch, run);
  const unscored = unscoredCount(run);
  const cards = cardModels(batch);
  const alternatives = alternativeItems(batch);
  const exclusions = exclusionRows(run);

  return (
    <section className="rec-panel" data-testid="recommendations-panel" data-state="ready">
      <h2 className="rec-panel__title">Ranked bass candidates</h2>
      {degraded ? (
        <DegradedBadge mode={batch.mode} jevStatus={run.ranking.jev_status} />
      ) : null}
      {kind === null ? (
        <>
          {run.counts.excluded > 0 ? <ExclusionsSummary count={run.counts.excluded} /> : null}
          <ol className="rec-list" data-testid="recommendations-list">
            {cards.map((card) => {
              const action = selectCardAction({ recommendations: cardStates }, card.candidateId);
              return (
                <RecommendationCard
                  key={card.candidateId}
                  card={card}
                  state={action?.state ?? "idle"}
                  errorCode={action?.errorCode ?? null}
                  retryable={action?.retryable ?? false}
                  onSelect={(candidateId) => {
                    void dispatch(recommendationActions.select(candidateId));
                  }}
                  onReject={(candidateId) => {
                    void dispatch(recommendationActions.reject(candidateId));
                  }}
                  onRetryAction={(candidateId) => {
                    void dispatch(recommendationActions.retryCard(candidateId));
                  }}
                />
              );
            })}
          </ol>
          {unscored > 0 ? <UnscoredNote count={unscored} /> : null}
          <AlternativesBlock items={alternatives} onFocus={focusCard} />
        </>
      ) : (
        <EmptyState
          kind={kind}
          limitReason={run.retrieval.limit_reason}
          exclusions={exclusions}
          unscored={unscored}
        />
      )}
    </section>
  );
}
