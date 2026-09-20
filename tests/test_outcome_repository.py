"""The outcome repository: append-only writes, idempotence and coded failures (issue #29).

This module owns the storage-layer evidence the socket tests cannot show: that a
refusal leaves no row, that a retry is idempotent, that a page is read in
insertion order with an exclusive cursor, that a lock maps to `database_locked`,
and that the SQLite failure mapping is the documented one. It uses #21's
repository directly and #22's synthetic rows; no real library path is involved.
"""

from __future__ import annotations

from contextlib import closing
import sqlite3

import pytest

from backend.library.errors import (DatabaseLocked, IdempotencyConflict, InvalidOutcome,
                                    OutcomeConflict, RemovalNotReflected,
                                    SelectionNotInPalette, UnknownSelection, WriteFailed)
from backend.library.outcomes import OutcomeSubmission
from backend.library.repository import LibraryRepository, coded_error
from tests.api_client import connection, insert_samples

MODE = "dsp-only"
RUN_ID = "run-00000000000000000000000000000001"


@pytest.fixture
def library(tmp_path):
    """A migrated database, one active bass candidate and its project."""

    database = tmp_path / "library.sqlite3"
    with closing(connection(database)):
        pass
    candidate = insert_samples(database, 1, directory=tmp_path, role="bass")[0]
    with closing(connection(database)) as opened:
        repository = LibraryRepository(opened)
        project = repository.create_project("Track A")
        repository.set_palette_item(project.palette_id, "bass", candidate, expected_revision=0)
        yield database, repository, project, candidate


def submission(project, candidate, *, client_event_id="event-1", event_type="auditioned",
               **overrides) -> OutcomeSubmission:
    supplied = dict(project_id=project.project_id, palette_id=project.palette_id,
                    candidate_id=candidate, run_id=RUN_ID, palette_revision=1,
                    ranking_version="ranking-v1", mode=MODE,
                    candidate_analysis_version="analysis-v1")
    supplied.update(overrides)
    return OutcomeSubmission(client_event_id=client_event_id, event_type=event_type, **supplied)


def rows(connection_) -> int:
    return connection_.execute(
        "SELECT COUNT(*) FROM recommendation_outcomes").fetchone()[0]


def test_an_audition_is_appended_with_every_stored_field(library):
    _, repository, project, candidate = library
    written = repository.record_outcome(submission(project, candidate))
    assert written.created is True
    event = written.event
    assert (event.event_id, event.event_type, event.project_id, event.palette_id,
            event.candidate_id, event.palette_revision, event.run_id, event.mode,
            event.candidate_analysis_version) == \
        (1, "auditioned", project.project_id, project.palette_id, candidate, 1, RUN_ID, MODE,
         "analysis-v1")
    assert event.recorded_at and event.removes_event_id is None
    assert rows(repository.connection) == 1


def test_the_same_client_event_id_with_the_same_content_is_idempotent(library):
    _, repository, project, candidate = library
    first = repository.record_outcome(submission(project, candidate))
    again = repository.record_outcome(submission(project, candidate))
    assert again.created is False and again.event == first.event
    assert rows(repository.connection) == 1


def test_a_known_client_event_id_with_new_content_is_refused(library):
    _, repository, project, candidate = library
    repository.record_outcome(submission(project, candidate))
    with pytest.raises(IdempotencyConflict) as caught:
        repository.record_outcome(submission(project, candidate, candidate_id="candidate-other"))
    assert caught.value.event_id == 1
    assert rows(repository.connection) == 1
    assert repository.connection.in_transaction is False


def test_a_selection_that_is_not_active_is_refused_and_writes_nothing(library):
    _, repository, project, _candidate = library
    with pytest.raises(SelectionNotInPalette):
        repository.record_outcome(
            submission(project, "candidate-other", event_type="selected"))
    assert rows(repository.connection) == 0
    assert repository.connection.in_transaction is False


def test_a_removal_naming_no_selection_is_refused_and_writes_nothing(library):
    _, repository, project, _candidate = library
    with pytest.raises(UnknownSelection):
        repository.record_outcome(OutcomeSubmission(
            client_event_id="event-removal", event_type="removed", removes_event_id=999))
    assert rows(repository.connection) == 0
    assert repository.connection.in_transaction is False


def test_a_removal_is_refused_while_the_candidate_is_still_active(library):
    _, repository, project, candidate = library
    selection = repository.record_outcome(submission(project, candidate, event_type="selected"))
    with pytest.raises(RemovalNotReflected):
        repository.record_outcome(OutcomeSubmission(
            client_event_id="event-removal", event_type="removed",
            removes_event_id=selection.event.event_id))
    assert rows(repository.connection) == 1


def test_a_rejection_of_a_live_selection_is_refused(library):
    _, repository, project, candidate = library
    repository.record_outcome(submission(project, candidate, client_event_id="event-selected",
                                         event_type="selected"))
    with pytest.raises(OutcomeConflict):
        repository.record_outcome(submission(project, candidate, client_event_id="event-rejected",
                                             event_type="rejected"))
    assert rows(repository.connection) == 1


def test_the_history_is_read_in_insertion_order_with_an_exclusive_cursor(library):
    _, repository, project, candidate = library
    first = repository.record_outcome(submission(project, candidate))
    second = repository.record_outcome(submission(project, candidate, client_event_id="event-2"))
    third = repository.record_outcome(submission(project, candidate, client_event_id="event-3"))
    ordered = repository.list_outcomes()
    assert [event.event_id for event in ordered] == [1, 2, 3]
    assert [event.event_id for event in
            repository.list_outcomes(after_event_id=first.event.event_id)] == [2, 3]
    assert [event.event_id for event in
            repository.list_outcomes(limit=1, after_event_id=second.event.event_id)] == [3]
    assert repository.list_outcomes(event_types=("selected",)) == ()
    assert third.created is True


def test_a_page_limit_or_filter_outside_the_contract_is_refused(library):
    _, repository, _project, _candidate = library
    with pytest.raises(InvalidOutcome):
        repository.list_outcomes(limit=0)
    with pytest.raises(InvalidOutcome):
        repository.list_outcomes(project_id="")
    with pytest.raises(InvalidOutcome):
        repository.list_outcomes(event_types=("ignored",))


def test_a_second_writer_times_out_as_database_locked(tmp_path):
    database = tmp_path / "library.sqlite3"
    with closing(connection(database)):
        pass
    candidate = insert_samples(database, 1, directory=tmp_path, role="bass")[0]
    with closing(connection(database)) as holder:
        repository = LibraryRepository(holder)
        project = repository.create_project("Track A")
        repository.set_palette_item(project.palette_id, "bass", candidate, expected_revision=0)
        with closing(connection(database)) as writer:
            holder.execute("BEGIN IMMEDIATE")
            holder.execute("UPDATE projects SET updated_at = updated_at")
            with pytest.raises(DatabaseLocked):
                LibraryRepository(writer).record_outcome(submission(project, candidate))
            holder.execute("ROLLBACK")
        assert rows(holder) == 0


def test_the_storage_failure_mapping_is_the_documented_one():
    assert isinstance(coded_error(sqlite3.OperationalError("database is locked")), DatabaseLocked)
    assert isinstance(coded_error(sqlite3.IntegrityError("UNIQUE constraint failed")), WriteFailed)
    assert isinstance(coded_error(sqlite3.OperationalError("disk I/O error")), WriteFailed)
