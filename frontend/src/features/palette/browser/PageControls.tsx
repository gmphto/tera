/**
 * Cursor paging (issue #31).
 *
 * Next pushes the cursor the service returned; Previous pops back to one that
 * was already used. No cursor is ever invented, and only `has_more` gates Next.
 */

import type { SamplePage } from "../api/types";
import { canGoNext, canGoPrevious, pageSummary } from "../state/selectors";

export interface PageControlsProps {
  pageIndex: number;
  page: SamplePage | undefined;
  onNext(): void;
  onPrevious(): void;
}

export function PageControls({ pageIndex, page, onNext, onPrevious }: PageControlsProps) {
  const nextCursor = page?.page.next_cursor ?? null;
  return (
    <nav
      className="pager"
      data-testid="library-pager"
      data-page-index={pageIndex}
      data-page-count={page?.page.count ?? 0}
      data-has-more={page?.page.has_more ? "true" : "false"}
      data-next-cursor={nextCursor === null ? "absent" : "present"}
      aria-label="Library pages"
    >
      <button
        type="button"
        data-testid="library-prev"
        disabled={!canGoPrevious(pageIndex)}
        onClick={onPrevious}
      >
        Previous
      </button>
      <span className="pager__summary">{page === undefined ? "No page yet" : pageSummary(page)}</span>
      <button type="button" data-testid="library-next" disabled={!canGoNext(page)} onClick={onNext}>
        Next
      </button>
    </nav>
  );
}
