"""Outcome recovery: restarts, two connections and the crash window (issue #29).

The history is append-only, so a restart must read exactly what was committed,
a second connection must never see a half-written event, and a crash between the
palette write and the event write must leave a state a client retry can finish.
Every database is temporary and every id is synthetic.
"""

from __future__ import annotations

from contextlib import closing

import pytest

from backend.library.errors import RemovalNotReflected, UnknownSelection
from backend.library.outcomes import OutcomeSubmission
from backend.library.repository import LibraryRepository
from backend.library.schema import open_database
from tests.api_client import connection, insert_samples

MODE = "dsp-only"
RUN_ID = "run-00000000000000000000000000000001"


def submission(project, candidate, *, client_event_id, revision, event_type="auditioned",
               **overrides) -> OutcomeSubmission:
    supplied = dict(project_id=project.project_id, palette_id=project.palette_id,
                    candidate_id=candidate, run_id=RUN_ID, palette_revision=revision,
                    ranking_version="ranking-v1", mode=MODE,
                    candidate_analysis_version="analysis-v1")
    supplied.update(overrides)
    return OutcomeSubmission(client_event_id=client_event_id, event_type=event_type, **supplied)


def seed(tmp_path):
    """A migrated database with one active bass candidate in one palette."""

    database = tmp_path / "library.sqlite3"
    with closing(connection(database)):
        pass
    candidate = insert_samples(database, 1, directory=tmp_path, role="bass")[0]
    with closing(connection(database)) as opened:
        repository = LibraryRepository(opened)
        project = repository.create_project("Track A")
        repository.set_palette_item(project.palette_id, "bass", candidate, expected_revision=0)
    return database, project, candidate


def revision_of(repository, project) -> int:
    return repository.load_palette(project.palette_id).revision


def history(database):
    with closing(connection(database)) as opened:
        return LibraryRepository(opened).list_outcomes()


def test_a_restart_reads_the_same_history_in_the_same_order(tmp_path):
    database, project, candidate = seed(tmp_path)
    with closing(connection(database)) as opened:
        repository = LibraryRepository(opened)
        for index in range(3):
            repository.record_outcome(submission(
                project, candidate, client_event_id=f"event-{index}",
                revision=revision_of(repository, project)))
    before = history(database)
    assert [event.client_event_id for event in before] == ["event-0", "event-1", "event-2"]
    assert history(database) == before
    reopened = open_database(database)
    try:
        assert LibraryRepository(reopened).list_outcomes() == before
    finally:
        reopened.close()


def test_a_second_connection_sees_only_committed_events(tmp_path):
    database, project, candidate = seed(tmp_path)
    with closing(connection(database)) as writer, closing(connection(database)) as reader:
        written = LibraryRepository(writer).record_outcome(submission(
            project, candidate, client_event_id="event-committed",
            revision=revision_of(LibraryRepository(writer), project)))
        assert written.created is True
        assert [event.client_event_id for event in LibraryRepository(reader).list_outcomes()] == \
            ["event-committed"]


def test_a_crash_between_the_palette_write_and_the_event_write_is_recoverable(tmp_path):
    """The palette write commits first, so a client retry can still log the selection."""

    database, project, candidate = seed(tmp_path)
    with closing(connection(database)) as opened:
        repository = LibraryRepository(opened)
        # No `selected` event was written before the crash: the item is active and the
        # log holds no window, which is exactly what a retry has to be able to repair.
        assert repository.list_outcomes() == ()
        written = repository.record_outcome(submission(
            project, candidate, client_event_id="event-retry", event_type="selected",
            revision=revision_of(repository, project)))
        assert written.created is True and written.event.event_type == "selected"


def test_a_removal_closes_the_window_and_a_later_selection_opens_a_new_one(tmp_path):
    database, project, candidate = seed(tmp_path)
    with closing(connection(database)) as opened:
        repository = LibraryRepository(opened)
        first = repository.record_outcome(submission(
            project, candidate, client_event_id="event-selected-1", event_type="selected",
            revision=revision_of(repository, project)))
        with pytest.raises(RemovalNotReflected):
            repository.record_outcome(OutcomeSubmission(
                client_event_id="event-removed-early", event_type="removed",
                removes_event_id=first.event.event_id))
        # The candidate leaves the slot; only then can the removal close the window.
        repository.remove_palette_item(project.palette_id, "bass",
                                       expected_revision=revision_of(repository, project))
        removed = repository.record_outcome(OutcomeSubmission(
            client_event_id="event-removed-1", event_type="removed",
            removes_event_id=first.event.event_id))
        assert removed.created is True and removed.event.candidate_id == candidate
        assert removed.event.palette_id == project.palette_id
        # A later selection starts a new window rather than reopening the closed one.
        repository.set_palette_item(project.palette_id, "bass", candidate,
                                    expected_revision=revision_of(repository, project))
        second = repository.record_outcome(submission(
            project, candidate, client_event_id="event-selected-2", event_type="selected",
            revision=revision_of(repository, project)))
        assert second.created is True and second.event.event_id != first.event.event_id
        assert [event.event_type for event in repository.list_outcomes()] == \
            ["selected", "removed", "selected"]


def test_an_item_selected_before_the_log_has_no_window_to_remove(tmp_path):
    """A palette written before this log has no `selected` event, so a removal is unknown."""

    database, project, _candidate = seed(tmp_path)
    with closing(connection(database)) as opened:
        repository = LibraryRepository(opened)
        repository.remove_palette_item(project.palette_id, "bass",
                                       expected_revision=revision_of(repository, project))
        with pytest.raises(UnknownSelection):
            repository.record_outcome(OutcomeSubmission(
                client_event_id="event-removed-legacy", event_type="removed",
                removes_event_id=1))
        assert repository.list_outcomes() == ()
        assert repository.connection.in_transaction is False
