"""Project, palette and palette-item persistence for the library (issue #24).

Every database lives in `tmp_path`, every referenced sample row is built from
the synthetic contract fixtures (`kick-001`, `bass-001` and ids derived from
them with `dataclasses.replace`), and every content hash is the SHA-256 of a
synthetic label. No real library path, sample name or audio byte appears here,
and no test opens an audio file.

The mutation matrix is driven by `tests/fixtures/palette/palette-cases.json`,
whose expected revisions, `changed` flags, sample states, slot-role flags and
hash equal/different pairs are written by hand.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from dataclasses import fields, replace
from pathlib import Path

import pytest

from backend.analysis import batch
from backend.contracts import (
    ContractError,
    RecommendationBatch,
    SongContext,
)
from backend.intelligence import questions
from backend.library.errors import (
    DatabaseLocked,
    InvalidSample,
    LibraryError,
    PaletteIncomplete,
    RevisionConflict,
    RoleMismatch,
    UnknownPalette,
    UnknownProject,
    UnknownSample,
    UnknownSlot,
    WriteFailed,
)
from backend.library.repository import LibraryRepository
from backend.library.schema import open_database, utc_now
from backend.palette.model import (
    CONTEXT_STATES,
    MVP_SLOTS,
    PALETTE_HASH_VERSION,
    SLOT_ROLES,
    SONG_CONTEXT_ABSENT_REASON,
    PaletteContextState,
    palette_hash,
)


FIXTURES = Path(__file__).parent / "fixtures" / "contracts"
CASES_PATH = Path(__file__).parent / "fixtures" / "palette" / "palette-cases.json"
CASES = json.loads(CASES_PATH.read_text(encoding="utf-8"))
CASE_IDS = [case["name"] for case in CASES["cases"]]
DOCUMENT = Path(__file__).resolve().parent.parent / "_docs" / "palette-storage.md"

# The library the fixture cases refer to: the two contract-fixture samples, a
# second kick, a sub-bass derived from the bass, and one row in each non-present
# file state.
LIBRARY_ROWS = (
    ("kick-001", "kick", "present", "kick"),
    ("kick-002", "kick", "present", "kick"),
    ("bass-001", "bass", "present", "bass"),
    ("sub-bass-001", "sub-bass", "present", "bass"),
    ("bass-missing-001", "bass", "missing", "bass"),
    ("kick-unknown-001", "kick", "unknown", "kick"),
)


def content_hash(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def hybrid_samples():
    return RecommendationBatch.from_json(
        (FIXTURES / "hybrid.json").read_text(encoding="utf-8")).samples


def seed_library(repository: LibraryRepository) -> str:
    """Store the sample rows the fixture cases refer to; return the version."""

    version = repository.register_analysis_version(batch.analysis_descriptor())
    kick, bass = hybrid_samples()
    base = {"kick": kick, "bass": bass}
    for sample_id, role, file_status, source in LIBRARY_ROWS:
        # Each row needs its own synthetic path: the stored path_key is unique,
        # and a derived sample keeps the fixture's audio metadata otherwise. The
        # path is never opened.
        sample = replace(base[source], sample_id=sample_id, role=role,
                         analysis_version=version,
                         audio=replace(base[source].audio,
                                       local_path=f"C:/tera-fixtures/{sample_id}.wav"))
        repository.import_sample(sample, content_sha256=content_hash(sample_id),
                                 file_status=file_status)
    return version


@pytest.fixture
def library(tmp_path):
    connection = open_database(tmp_path / "library.sqlite3")
    repository = LibraryRepository(connection)
    seed_library(repository)
    try:
        yield repository
    finally:
        connection.close()


def new_palette(library: LibraryRepository):
    return library.create_project("Track A")


def song(name: str) -> SongContext:
    return SongContext.from_dict(CASES["contexts"][name])


def sample_id_of(step) -> str:
    if "sample_literal" in step:
        return step["sample_literal"]
    return CASES["library"][step["sample"]]


def mutate(library: LibraryRepository, palette_id: str, step):
    operation = step["operation"]
    if operation == "set_item":
        return library.set_palette_item(palette_id, step["slot"], sample_id_of(step),
                                        expected_revision=step["expected_revision"])
    if operation == "remove_item":
        return library.remove_palette_item(palette_id, step["slot"],
                                           expected_revision=step["expected_revision"])
    if operation == "set_context":
        return library.set_palette_context(palette_id, song(step["context"]),
                                           expected_revision=step["expected_revision"])
    raise AssertionError(f"Unknown fixture operation: {operation}")


def check_item(item, expected) -> None:
    assert item is not None
    for name, value in expected.items():
        assert getattr(item, name) == value, f"{name}: {getattr(item, name)!r} != {value!r}"


def check_mutation(mutation, expected) -> None:
    assert mutation.changed is expected["changed"]
    assert mutation.revision == expected["revision"]
    if "active_item" in expected:
        if expected["active_item"] is None:
            assert mutation.active_item is None
        else:
            check_item(mutation.active_item, expected["active_item"])
    if "previous_item" in expected:
        if expected["previous_item"] is None:
            assert mutation.previous_item is None
        else:
            check_item(mutation.previous_item, expected["previous_item"])


def active_rows(connection, palette_id):
    return [tuple(row) for row in connection.execute(
        "SELECT slot, sample_id, role FROM palette_items WHERE palette_id = ? "
        "AND removed_at IS NULL ORDER BY slot", (palette_id,))]


def palette_dump(connection, palette_id):
    return [tuple(row) for row in connection.execute(
        "SELECT * FROM palettes WHERE palette_id = ?", (palette_id,))] + [
        tuple(row) for row in connection.execute(
            "SELECT * FROM palette_items WHERE palette_id = ? ORDER BY item_id", (palette_id,))]


def tamper(value, **replacements):
    """A frozen dataclass copy with fields replaced, bypassing validation."""

    clone = object.__new__(type(value))
    for field in fields(value):
        object.__setattr__(clone, field.name,
                           replacements.get(field.name, getattr(value, field.name)))
    return clone


# ---------------------------------------------------------------------------
# the hand-written fixture matrix
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("case", CASES["cases"], ids=CASE_IDS)
def test_the_fixture_matrix(library, case):
    project = new_palette(library)
    palette_id = project.palette_id
    changed = 0
    for step in case["mutations"]:
        if "expect_error" in step:
            with pytest.raises(LibraryError) as raised:
                mutate(library, palette_id, step)
            assert raised.value.code == step["expect_error"]
            continue
        mutation = mutate(library, palette_id, step)
        check_mutation(mutation, step["expect"])
        changed += 1 if mutation.changed else 0
    record = library.load_palette(palette_id)
    expected = case["expect"]
    assert record.revision == expected["revision"] == changed
    assert [(item.slot, item.sample_id, item.role) for item in record.active_items] \
        == [tuple(item) for item in expected["active"]]
    assert [(item.slot, item.sample_id, item.removed_revision)
            for item in record.removed_items] == [tuple(item) for item in expected["removed"]]
    assert [record.context_state.tempo, record.context_state.key, record.context_state.genre] \
        == [expected["context_state"][name] for name in ("tempo", "key", "genre")]
    if "song" in expected:
        assert record.song.to_dict() == song(expected["song"]).to_dict()
    if expected["to_context"] == "palette_incomplete":
        with pytest.raises(PaletteIncomplete) as raised:
            record.to_context()
        assert raised.value.code == "palette_incomplete"
    else:
        context = record.to_context()
        assert context.palette_id == palette_id and context.revision == record.revision
        assert context.kick_id == expected["to_context"]["kick"]
        assert context.selected_bass_id == expected["to_context"]["bass"]
    for slot in MVP_SLOTS:
        rows = library.connection.execute(
            "SELECT COUNT(*) FROM palette_items WHERE palette_id = ? AND slot = ? "
            "AND removed_at IS NULL", (palette_id, slot)).fetchone()[0]
        assert rows <= 1, slot


# ---------------------------------------------------------------------------
# create, reopen and contract assembly
# ---------------------------------------------------------------------------


def test_create_project_reopens_with_the_same_palette(tmp_path):
    path = tmp_path / "library.sqlite3"
    connection = open_database(path)
    repository = LibraryRepository(connection)
    seed_library(repository)
    project = repository.create_project("Track A", palette_name="Main")
    assert project.name == "Track A"
    assert project.created_at == project.updated_at
    assert repository.load_palette(project.palette_id).revision == 0
    repository.set_palette_item(project.palette_id, "kick", "kick-001", expected_revision=0)
    repository.set_palette_item(project.palette_id, "bass", "bass-001", expected_revision=1)
    repository.set_palette_context(project.palette_id, song("known"), expected_revision=2)
    repository.remove_palette_item(project.palette_id, "kick", expected_revision=3)
    repository.set_palette_item(project.palette_id, "kick", "kick-002", expected_revision=4)
    before = repository.load_palette(project.palette_id)
    connection.close()

    connection = open_database(path)
    try:
        repository = LibraryRepository(connection)
        after = repository.load_palette(project.palette_id)
        assert after.palette_id == before.palette_id
        assert after.project_id == before.project_id
        assert after.revision == before.revision == 5
        assert after.active_items == before.active_items
        assert [(item.slot, item.sample_id, item.removed_revision)
                for item in after.removed_items] == [
                    (item.slot, item.sample_id, item.removed_revision)
                    for item in before.removed_items]
        assert after.removed_items[0].sample_id == "kick-001"
        assert after.context_state == before.context_state
        assert after.song.to_dict() == before.song.to_dict()
        assert palette_hash(after) == palette_hash(before)
        project_again = repository.get_project(project.project_id)
        assert project_again.palette_id == project.palette_id
        assert repository.list_palettes(project.project_id) == (after,)
    finally:
        connection.close()


def test_to_context_round_trips_through_the_contract(library):
    project = new_palette(library)
    palette_id = project.palette_id
    library.set_palette_item(palette_id, "kick", "kick-001", expected_revision=0)
    library.set_palette_item(palette_id, "bass", "sub-bass-001", expected_revision=1)
    library.set_palette_context(palette_id, song("known"), expected_revision=2)
    record = library.load_palette(palette_id)
    context = record.to_context()
    assert context.revision == 3
    assert context.kick_id == "kick-001" and context.selected_bass_id == "sub-bass-001"
    assert context.song.to_dict() == song("known").to_dict()
    assert type(context).from_json(context.to_json()) == context


def test_a_context_reports_the_absent_reason_where_the_columns_are_null(library):
    project = new_palette(library)
    fresh = library.load_palette(project.palette_id)
    assert fresh.song.tempo.unavailable_reason == SONG_CONTEXT_ABSENT_REASON
    assert fresh.song.key.unavailable_reason == SONG_CONTEXT_ABSENT_REASON
    assert fresh.song.genre is None
    assert fresh.song.genre_unavailable_reason == SONG_CONTEXT_ABSENT_REASON
    assert SONG_CONTEXT_ABSENT_REASON == questions.SONG_CONTEXT_ABSENT
    library.set_palette_item(project.palette_id, "kick", "kick-001", expected_revision=0)
    assert library.load_palette(project.palette_id).to_context().song == fresh.song


def test_a_recommendation_batch_around_the_context_validates(library):
    project = new_palette(library)
    palette_id = project.palette_id
    library.set_palette_item(palette_id, "kick", "kick-001", expected_revision=0)
    library.set_palette_item(palette_id, "bass", "sub-bass-001", expected_revision=1)
    context = library.load_palette(palette_id).to_context()
    kick = library.get_sample("kick-001").sample
    bass = library.get_sample("sub-bass-001").sample
    assembled = RecommendationBatch(run_id="palette-test-001", palette=context,
                                    samples=(kick, bass), results=(),
                                    ranking_version="palette-test-v1", mode="dsp-only",
                                    alternatives=())
    assert assembled.palette.kick_id == kick.sample_id
    assert assembled.to_json()


def test_unknown_ids_are_return_values_not_failures(library):
    assert library.load_palette("palette-does-not-exist") is None
    assert library.get_project("project-does-not-exist") is None
    assert library.delete_project("project-does-not-exist") is False
    with pytest.raises(UnknownProject) as raised:
        library.list_palettes("project-does-not-exist")
    assert raised.value.code == "unknown_project"


def test_projects_are_listed_in_creation_order(library):
    first = library.create_project("First")
    second = library.create_project("Second")
    projects = library.list_projects()
    assert {item.project_id for item in projects} == {first.project_id, second.project_id}
    # The documented order is created_at, then project_id; the timestamps can
    # tie inside one second, so the tie-break is part of the assertion.
    assert projects == tuple(sorted(projects, key=lambda item: (item.created_at,
                                                               item.project_id)))
    assert first.palette_id != second.palette_id
    for project in projects:
        assert library.get_project(project.project_id).palette_id == project.palette_id


def test_a_blank_project_name_is_refused_and_writes_nothing(library):
    with pytest.raises(WriteFailed) as raised:
        library.create_project("   ")
    assert raised.value.code == "write_failed"
    with pytest.raises(WriteFailed):
        library.create_project("Track", palette_name="")
    assert library.list_projects() == ()


def test_a_palette_without_a_kick_is_stored_and_reported(library):
    project = new_palette(library)
    library.set_palette_item(project.palette_id, "bass", "bass-001", expected_revision=0)
    record = library.load_palette(project.palette_id)
    assert record is not None and record.revision == 1
    assert [item.slot for item in record.active_items] == ["bass"]
    assert record.context_state == PaletteContextState("unset", "unset", "unset")
    assert len(palette_hash(record)) == 64
    with pytest.raises(PaletteIncomplete) as raised:
        record.to_context()
    assert raised.value.code == "palette_incomplete"


# ---------------------------------------------------------------------------
# context validation
# ---------------------------------------------------------------------------


def test_an_invalid_context_is_refused_with_the_contract_error_and_writes_nothing(library):
    project = new_palette(library)
    palette_id = project.palette_id
    known, unknown = song("known"), song("unknown")
    payloads = {
        "tempo_without_confidence": tamper(known, tempo=tamper(known.tempo, confidence=None)),
        "key_tonic_without_mode": tamper(known, key=tamper(known.key, mode=None)),
        "value_and_reason_together": tamper(
            known, tempo=tamper(known.tempo, unavailable_reason="also_a_reason")),
        "blank_reason": tamper(unknown, key=tamper(unknown.key, unavailable_reason="  ")),
        "not_a_song_context": None,
    }
    for name, payload in payloads.items():
        before = palette_dump(library.connection, palette_id)
        with pytest.raises(LibraryError) as raised:
            library.set_palette_context(palette_id, payload, expected_revision=0)
        assert raised.value.code == "invalid_context", name
        if name != "not_a_song_context":
            assert isinstance(raised.value.__cause__, ContractError), name
        assert palette_dump(library.connection, palette_id) == before, name
    assert library.load_palette(palette_id).revision == 0


def test_a_dict_context_is_accepted_and_validated(library):
    project = new_palette(library)
    payload = CASES["contexts"]["known"]
    mutation = library.set_palette_context(project.palette_id, dict(payload), expected_revision=0)
    assert mutation.changed and mutation.revision == 1
    record = library.load_palette(project.palette_id)
    assert record.context_state == PaletteContextState("known", "known", "known")
    assert record.song.to_dict() == song("known").to_dict()


# ---------------------------------------------------------------------------
# one active item per slot, and replacement atomicity
# ---------------------------------------------------------------------------


def test_the_unique_index_rejects_a_second_active_item(library):
    project = new_palette(library)
    palette_id = project.palette_id
    library.set_palette_item(palette_id, "kick", "kick-001", expected_revision=0)
    now = utc_now()
    with pytest.raises(sqlite3.IntegrityError):
        library.connection.execute(
            "INSERT INTO palette_items (item_id, palette_id, slot, sample_id, role, "
            "added_revision, added_at, removed_revision, removed_at) "
            "VALUES ('item-manual', ?, 'kick', 'kick-002', 'kick', 1, ?, NULL, NULL)",
            (palette_id, now))
    assert active_rows(library.connection, palette_id) == [("kick", "kick-001", "kick")]


def test_the_partial_unique_index_is_mapped_to_write_failed(library):
    project = new_palette(library)
    palette_id = project.palette_id
    before = palette_dump(library.connection, palette_id)
    library.connection.execute(
        "CREATE TRIGGER force_active_conflict AFTER INSERT ON palette_items "
        "BEGIN INSERT INTO palette_items (item_id, palette_id, slot, sample_id, role, "
        "added_revision, added_at, removed_revision, removed_at) "
        "SELECT 'item-trigger', palette_id, slot, 'kick-002', 'kick', added_revision, added_at, "
        "NULL, NULL FROM palette_items WHERE item_id = NEW.item_id; END")
    try:
        with pytest.raises(WriteFailed) as raised:
            library.set_palette_item(palette_id, "kick", "kick-001", expected_revision=0)
        assert raised.value.code == "write_failed"
        assert isinstance(raised.value.__cause__, sqlite3.IntegrityError)
        assert "palette_items.palette_id, palette_items.slot" in str(raised.value.__cause__)
        index = library.connection.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'ux_palette_items_active_slot'"
        ).fetchone()[0]
        assert "WHERE removed_at IS NULL" in index
    finally:
        library.connection.execute("DROP TRIGGER force_active_conflict")
    assert palette_dump(library.connection, palette_id) == before


def test_a_mid_replacement_failure_rolls_back(library):
    project = new_palette(library)
    palette_id = project.palette_id
    library.set_palette_item(palette_id, "kick", "kick-001", expected_revision=0)
    before = palette_dump(library.connection, palette_id)
    library.connection.execute(
        "CREATE TRIGGER refuse_palette_insert BEFORE INSERT ON palette_items "
        "BEGIN SELECT RAISE(ABORT, 'synthetic trigger failure'); END")
    try:
        with pytest.raises(WriteFailed) as raised:
            library.set_palette_item(palette_id, "kick", "kick-002", expected_revision=1)
        assert raised.value.code == "write_failed"
        assert raised.value.__cause__ is not None
    finally:
        library.connection.execute("DROP TRIGGER refuse_palette_insert")
    assert palette_dump(library.connection, palette_id) == before
    assert active_rows(library.connection, palette_id) == [("kick", "kick-001", "kick")]
    assert library.load_palette(palette_id).revision == 1


def test_ids_are_opaque_distinct_and_never_reused(library):
    project = new_palette(library)
    palette_id = project.palette_id
    first = library.set_palette_item(palette_id, "kick", "kick-001", expected_revision=0)
    first_id = first.active_item.item_id
    assert first.active_item.added_revision == 1
    removed = library.remove_palette_item(palette_id, "kick", expected_revision=1)
    assert removed.previous_item.item_id == first_id
    assert removed.previous_item.removed_revision == 2
    again = library.set_palette_item(palette_id, "kick", "kick-001", expected_revision=2)
    assert again.active_item.item_id != first_id
    assert again.active_item.added_revision == 3
    record = library.load_palette(palette_id)
    assert [item.item_id for item in record.active_items] == [again.active_item.item_id]
    assert [item.item_id for item in record.removed_items] == [first_id]
    assert record.removed_items[0].removed_revision == 2
    # Added revisions are monotonic per palette: every later item was added at a
    # strictly greater revision than the one it replaced.
    added = [item.added_revision for item in record.removed_items + record.active_items]
    assert added == sorted(added) and len(set(added)) == len(added)


def test_a_replacement_keeps_exactly_one_active_item_per_slot(library):
    project = new_palette(library)
    palette_id = project.palette_id
    previous = None
    for revision, sample in enumerate(("kick-001", "kick-002", "kick-001")):
        mutation = library.set_palette_item(palette_id, "kick", sample,
                                            expected_revision=revision)
        assert mutation.changed and mutation.revision == revision + 1
        assert active_rows(library.connection, palette_id) == [("kick", sample, "kick")]
        if previous is not None:
            assert mutation.previous_item.sample_id == previous
            assert mutation.previous_item.removed_revision == revision + 1
        previous = sample
    record = library.load_palette(palette_id)
    assert len(record.active_items) == 1 and len(record.removed_items) == 2


# ---------------------------------------------------------------------------
# deletion and shared samples
# ---------------------------------------------------------------------------


def test_deleting_a_project_cascades_and_keeps_every_library_row(library):
    connection = library.connection
    project = new_palette(library)
    palette_id = project.palette_id
    library.set_palette_item(palette_id, "kick", "kick-001", expected_revision=0)
    library.set_palette_item(palette_id, "bass", "bass-001", expected_revision=1)
    library.set_palette_context(palette_id, song("known"), expected_revision=2)
    before = {name: [tuple(row) for row in connection.execute(
        "SELECT * FROM " + name + " ORDER BY 1, 2")] for name in
        ("samples", "sample_features", "sample_keys", "sample_tags")}
    assert library.delete_project(project.project_id) is True
    assert library.delete_project(project.project_id) is False
    assert library.load_palette(palette_id) is None
    assert library.get_project(project.project_id) is None
    for name in ("projects", "palettes", "palette_items"):
        assert connection.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0] == 0
    after = {name: [tuple(row) for row in connection.execute(
        "SELECT * FROM " + name + " ORDER BY 1, 2")] for name in before}
    assert after == before


def test_two_projects_sharing_a_sample_stay_independent(library):
    connection = library.connection
    first = new_palette(library)
    second = new_palette(library)
    library.set_palette_item(first.palette_id, "bass", "bass-001", expected_revision=0)
    library.set_palette_item(second.palette_id, "bass", "bass-001", expected_revision=0)
    second_before = palette_dump(connection, second.palette_id)
    library.set_palette_item(first.palette_id, "bass", "sub-bass-001", expected_revision=1)
    assert palette_dump(connection, second.palette_id) == second_before
    library.remove_palette_item(second.palette_id, "bass", expected_revision=1)
    assert palette_dump(connection, second.palette_id) != second_before
    assert connection.execute("SELECT COUNT(*) FROM samples WHERE sample_id = 'bass-001'") \
        .fetchone()[0] == 1
    assert library.delete_project(first.project_id) is True
    record = library.load_palette(second.palette_id)
    assert record.project_id == second.project_id and record.revision == 2
    assert connection.execute("SELECT COUNT(*) FROM samples WHERE sample_id = 'bass-001'") \
        .fetchone()[0] == 1
    assert library.get_sample("bass-001") is not None


def test_a_mutation_after_the_palette_is_gone_raises_unknown_palette(library):
    project = new_palette(library)
    palette_id = project.palette_id
    library.delete_project(project.project_id)
    for call in (
            lambda: library.set_palette_item(palette_id, "kick", "kick-001",
                                             expected_revision=0),
            lambda: library.remove_palette_item(palette_id, "kick", expected_revision=0),
            lambda: library.set_palette_context(palette_id, song("known"), expected_revision=0)):
        with pytest.raises(UnknownPalette) as raised:
            call()
        assert raised.value.code == "unknown_palette"
    assert library.list_projects() == ()


# ---------------------------------------------------------------------------
# the revision rules and the hash
# ---------------------------------------------------------------------------


def test_the_revision_conflict_reads_both_revisions(library):
    project = new_palette(library)
    palette_id = project.palette_id
    library.set_palette_item(palette_id, "kick", "kick-001", expected_revision=0)
    with pytest.raises(RevisionConflict) as raised:
        library.set_palette_item(palette_id, "bass", "bass-001", expected_revision=7)
    assert raised.value.code == "revision_conflict"
    assert raised.value.expected_revision == 7
    assert raised.value.current_revision == 1
    assert library.load_palette(palette_id).revision == 1


def test_reads_and_no_ops_never_change_the_revision(library):
    project = new_palette(library)
    palette_id = project.palette_id
    library.set_palette_item(palette_id, "kick", "kick-001", expected_revision=0)
    library.set_palette_context(palette_id, song("known"), expected_revision=1)
    for _ in range(3):
        assert library.load_palette(palette_id).revision == 2
        assert library.list_palettes(project.project_id)[0].revision == 2
    assert library.set_palette_item(palette_id, "kick", "kick-001",
                                    expected_revision=2).changed is False
    assert library.set_palette_context(palette_id, song("known"),
                                       expected_revision=2).changed is False
    assert library.remove_palette_item(palette_id, "bass", expected_revision=2).changed is False
    assert library.load_palette(palette_id).revision == 2
    after = palette_dump(library.connection, palette_id)
    for _ in range(2):
        assert library.set_palette_item(palette_id, "kick", "kick-001",
                                        expected_revision=2).changed is False
        assert library.set_palette_context(palette_id, song("known"),
                                           expected_revision=2).changed is False
        library.load_palette(palette_id)
    assert palette_dump(library.connection, palette_id) == after


def build_scenario(library, name, project):
    """Build one named hash-matrix scenario in a fresh palette; return its record."""

    palette_id = project.palette_id
    step = {"revision": 0}

    def set_item(slot, sample):
        library.set_palette_item(palette_id, slot, sample,
                                 expected_revision=step["revision"])
        step["revision"] += 1

    def remove_item(slot):
        library.remove_palette_item(palette_id, slot, expected_revision=step["revision"])
        step["revision"] += 1

    def set_context(context):
        library.set_palette_context(palette_id, context, expected_revision=step["revision"])
        step["revision"] += 1

    if name == "kick_and_bass":
        set_item("kick", "kick-001")
        set_item("bass", "bass-001")
    elif name == "bass_then_kick":
        set_item("bass", "bass-001")
        set_item("kick", "kick-001")
    elif name == "kick_and_bass_removed_and_readded":
        set_item("kick", "kick-001")
        set_item("bass", "bass-001")
        remove_item("bass")
        set_item("bass", "bass-001")
    elif name == "other_kick":
        set_item("kick", "kick-002")
        set_item("bass", "bass-001")
    elif name == "sub_bass_for_bass":
        set_item("kick", "kick-001")
        set_item("bass", "sub-bass-001")
    elif name == "sub_bass_for_bass_rewritten":
        set_item("kick", "kick-001")
        set_item("bass", "sub-bass-001")
        remove_item("bass")
        set_item("bass", "sub-bass-001")
    elif name == "unset_context":
        pass
    elif name == "unknown_context":
        set_context(song("unknown"))
    elif name == "other_unknown_reason":
        original = song("unknown")
        set_context(replace(original, tempo=replace(original.tempo,
                                                    unavailable_reason="another_reason")))
    elif name == "known_context":
        set_context(song("known"))
    elif name == "other_genre":
        original = song("known")
        set_context(replace(original, genre="house"))
    else:
        raise AssertionError(f"Unknown scenario: {name}")
    return library.load_palette(palette_id)


def equivalence_classes(equal_pairs):
    """The groups of scenario names the fixture's equal pairs declare."""

    classes = []
    for left, right in equal_pairs:
        left_group = next((group for group in classes if left in group), None)
        right_group = next((group for group in classes if right in group), None)
        if left_group is None and right_group is None:
            classes.append({left, right})
        elif left_group is None:
            right_group.add(left)
        elif right_group is None:
            left_group.add(right)
        elif left_group is not right_group:
            left_group |= right_group
            classes.remove(right_group)
    return classes


HASH_PROBE_ID = "palette-fixture-001"


def content_hash_of(record) -> str:
    """`palette_hash` with the palette id normalised.

    The hash covers the palette id, so two separately created palettes can only
    be compared for content after that one field is made equal. Every comparison
    below still runs the real `palette_hash` over a record the repository read.
    """

    return palette_hash(replace(record, palette_id=HASH_PROBE_ID))


def test_the_hash_matrix_from_the_fixture(library):
    records = {}

    def record_for(name):
        if name not in records:
            records[name] = build_scenario(library, name, new_palette(library))
        return records[name]

    for left, right in CASES["hash_matrix"]["equal"]:
        assert content_hash_of(record_for(left)) == content_hash_of(record_for(right)), \
            f"{left} and {right} must hash equal"
    for left, right in CASES["hash_matrix"]["different"]:
        assert content_hash_of(record_for(left)) != content_hash_of(record_for(right)), \
            f"{left} and {right} must hash differently"
    names = sorted({name for pair in CASES["hash_matrix"]["equal"] + CASES["hash_matrix"]["different"]
                    for name in pair})
    assert set(names) == set(records)
    classes = equivalence_classes(CASES["hash_matrix"]["equal"])
    for name in names:
        if not any(name in group for group in classes):
            classes.append({name})
    assert len({content_hash_of(record_for(name)) for name in names}) == len(classes)
    # The palette id itself is covered, so the same content under two ids differs.
    first, second = record_for("kick_and_bass"), record_for("other_kick")
    assert palette_hash(replace(first, palette_id="palette-fixture-a")) \
        != palette_hash(replace(first, palette_id="palette-fixture-b"))
    assert first.palette_id != second.palette_id


def test_the_hash_covers_content_only(library):
    project = new_palette(library)
    palette_id = project.palette_id
    library.set_palette_item(palette_id, "kick", "kick-001", expected_revision=0)
    library.set_palette_item(palette_id, "bass", "bass-001", expected_revision=1)
    before = library.load_palette(palette_id)
    first_hash = palette_hash(before)
    assert len(first_hash) == 64 and first_hash == first_hash.lower()
    assert PALETTE_HASH_VERSION not in first_hash
    library.remove_palette_item(palette_id, "bass", expected_revision=2)
    library.set_palette_item(palette_id, "bass", "bass-001", expected_revision=3)
    after = library.load_palette(palette_id)
    assert after.revision == 4 and before.revision == 2
    before_items = {item.slot: item for item in before.active_items}
    after_items = {item.slot: item for item in after.active_items}
    assert after_items["kick"].added_at == before_items["kick"].added_at
    assert after_items["kick"].item_id == before_items["kick"].item_id
    assert after_items["bass"].item_id != before_items["bass"].item_id
    assert after_items["bass"].added_revision > before_items["bass"].added_revision
    assert palette_hash(after) == first_hash
    library.set_palette_context(palette_id, song("known"), expected_revision=4)
    assert palette_hash(library.load_palette(palette_id)) != first_hash
    assert palette_hash(after) == first_hash


# ---------------------------------------------------------------------------
# two connections
# ---------------------------------------------------------------------------


def test_no_palette_call_leaves_a_transaction_open(library):
    """Criterion (e): one call is one transaction, and none is held between calls."""

    project = new_palette(library)
    palette_id = project.palette_id
    assert library.connection.in_transaction is False
    library.load_palette(palette_id)
    library.list_palettes(project.project_id)
    assert library.connection.in_transaction is False
    library.create_project("Second")
    assert library.connection.in_transaction is False
    library.set_palette_item(palette_id, "kick", "kick-001", expected_revision=0)
    assert library.connection.in_transaction is False
    library.set_palette_context(palette_id, song("known"), expected_revision=1)
    assert library.connection.in_transaction is False
    with pytest.raises(RevisionConflict):
        library.remove_palette_item(palette_id, "kick", expected_revision=0)
    assert library.connection.in_transaction is False
    library.remove_palette_item(palette_id, "kick", expected_revision=2)
    assert library.connection.in_transaction is False
    library.delete_project(project.project_id)
    assert library.connection.in_transaction is False
    assert library.list_projects() is not None


def test_a_stale_writer_conflicts_then_retries(tmp_path):
    path = tmp_path / "library.sqlite3"
    first = open_database(path)
    second = open_database(path)
    try:
        repository = LibraryRepository(first)
        seed_library(repository)
        project = repository.create_project("Track A")
        palette_id = project.palette_id
        repository.set_palette_item(palette_id, "kick", "kick-001", expected_revision=0)
        other = LibraryRepository(second)
        assert other.load_palette(palette_id).revision == 1
        other.set_palette_item(palette_id, "bass", "bass-001", expected_revision=1)
        with pytest.raises(RevisionConflict) as raised:
            repository.set_palette_item(palette_id, "bass", "sub-bass-001",
                                        expected_revision=1)
        assert raised.value.current_revision == 2
        stored = other.load_palette(palette_id)
        assert [item.sample_id for item in stored.active_items] == ["bass-001", "kick-001"]
        repository.set_palette_item(palette_id, "bass", "sub-bass-001", expected_revision=2)
        assert repository.load_palette(palette_id).revision == 3
    finally:
        first.close()
        second.close()


def test_two_identical_submissions_produce_one_winner(tmp_path):
    path = tmp_path / "library.sqlite3"
    first = open_database(path)
    second = open_database(path)
    try:
        repository = LibraryRepository(first)
        seed_library(repository)
        project = repository.create_project("Track A")
        palette_id = project.palette_id
        winner = repository.set_palette_item(palette_id, "kick", "kick-001", expected_revision=0)
        assert winner.changed is True and winner.revision == 1
        loser = LibraryRepository(second)
        with pytest.raises(RevisionConflict) as raised:
            loser.set_palette_item(palette_id, "kick", "kick-001", expected_revision=0)
        assert raised.value.code == "revision_conflict"
        stored = loser.load_palette(palette_id)
        assert stored.revision == 1
        assert len([item for item in stored.active_items if item.slot == "kick"]) == 1
        assert stored.active_items[0].item_id == winner.active_item.item_id
        retry = loser.set_palette_item(palette_id, "kick", "kick-001", expected_revision=1)
        assert retry.changed is False and retry.revision == 1
        assert len(stored.active_items) == 1
    finally:
        first.close()
        second.close()


def test_a_reader_sees_pre_commit_and_then_committed_state(tmp_path):
    path = tmp_path / "library.sqlite3"
    first = open_database(path)
    second = open_database(path)
    try:
        repository = LibraryRepository(first)
        seed_library(repository)
        project = repository.create_project("Track A")
        palette_id = project.palette_id
        reader = LibraryRepository(second)
        now = utc_now()
        first.execute("BEGIN IMMEDIATE")
        try:
            first.execute(
                "INSERT INTO palette_items (item_id, palette_id, slot, sample_id, role, "
                "added_revision, added_at, removed_revision, removed_at) "
                "VALUES ('item-uncommitted', ?, 'kick', 'kick-001', 'kick', 1, ?, NULL, NULL)",
                (palette_id, now))
            first.execute("UPDATE palettes SET revision = 1, updated_at = ? WHERE palette_id = ?",
                          (now, palette_id))
            pre_commit = reader.load_palette(palette_id)
            assert pre_commit.revision == 0
            assert pre_commit.active_items == ()
            first.execute("COMMIT")
        except BaseException:
            first.execute("ROLLBACK")
            raise
        committed = reader.load_palette(palette_id)
        assert committed.revision == 1
        assert [item.sample_id for item in committed.active_items] == ["kick-001"]
    finally:
        first.close()
        second.close()


def test_a_second_writer_times_out_as_database_locked(tmp_path):
    path = tmp_path / "library.sqlite3"
    first = open_database(path)
    second = open_database(path)
    try:
        repository = LibraryRepository(first)
        seed_library(repository)
        project = repository.create_project("Track A")
        palette_id = project.palette_id
        repository.set_palette_item(palette_id, "kick", "kick-001", expected_revision=0)
        blocked = LibraryRepository(second)
        started = time.monotonic()
        first.execute("BEGIN IMMEDIATE")
        try:
            first.execute("UPDATE palettes SET updated_at = ? WHERE palette_id = ?",
                          (utc_now(), palette_id))
            with pytest.raises(DatabaseLocked) as raised:
                blocked.set_palette_item(palette_id, "kick", "kick-002", expected_revision=1)
            assert raised.value.code == "database_locked"
            first.execute("COMMIT")
        except BaseException:
            first.execute("ROLLBACK")
            raise
        assert time.monotonic() - started >= 1.0
        assert [item.sample_id for item in repository.load_palette(palette_id).active_items] \
            == ["kick-001"]
        retry = blocked.set_palette_item(palette_id, "kick", "kick-002", expected_revision=1)
        assert retry.changed is True and retry.revision == 2
    finally:
        first.close()
        second.close()


# ---------------------------------------------------------------------------
# errors, privacy and the document
# ---------------------------------------------------------------------------


def test_every_palette_failure_is_a_coded_library_error(library):
    project = new_palette(library)
    palette_id = project.palette_id
    cases = (
        (UnknownProject, "unknown_project",
         lambda: library.list_palettes("project-missing")),
        (UnknownPalette, "unknown_palette",
         lambda: library.set_palette_item("palette-missing", "kick", "kick-001",
                                          expected_revision=0)),
        (UnknownSlot, "unknown_slot",
         lambda: library.set_palette_item(palette_id, "snare", "kick-001", expected_revision=0)),
        (InvalidSample, "invalid_sample",
         lambda: library.set_palette_item(palette_id, "kick", "", expected_revision=0)),
        (UnknownSample, "unknown_sample",
         lambda: library.set_palette_item(palette_id, "kick", "kick-999",
                                          expected_revision=0)),
        (RoleMismatch, "role_mismatch",
         lambda: library.set_palette_item(palette_id, "kick", "bass-001", expected_revision=0)),
        (RevisionConflict, "revision_conflict",
         lambda: library.remove_palette_item(palette_id, "kick", expected_revision=3)),
    )
    for error, code, call in cases:
        with pytest.raises(error) as raised:
            call()
        assert raised.value.code == code
        assert isinstance(raised.value, LibraryError)


def test_a_role_mismatch_names_the_slot_roles_and_sample(library):
    project = new_palette(library)
    with pytest.raises(RoleMismatch) as raised:
        library.set_palette_item(project.palette_id, "bass", "kick-001", expected_revision=0)
    message = str(raised.value)
    # The message names the slot, its accepted roles, the sample and its stored role.
    assert "slot bass" in message
    assert "roles bass, sub-bass" in message
    assert "kick-001" in message and "role kick" in message


def test_the_palette_tables_hold_no_blob_path_or_secret(library):
    connection = library.connection
    project = new_palette(library)
    palette_id = project.palette_id
    library.set_palette_item(palette_id, "kick", "kick-001", expected_revision=0)
    library.set_palette_context(palette_id, song("known"), expected_revision=1)
    forbidden = ("path", "blob", "jev", "payload", "token", "credential", "endpoint",
                 "outcome", "recommendation")
    for table in ("projects", "palettes", "palette_items"):
        for column in connection.execute(f"PRAGMA table_info({table})"):
            assert (column[2] or "").upper() != "BLOB", (table, column[1])
            assert not any(word in column[1].lower() for word in forbidden), (table, column[1])
    for table in ("projects", "palettes", "palette_items"):
        for row in connection.execute(f"SELECT * FROM {table}"):
            for value in row:
                text = "" if value is None else str(value)
                assert "\\" not in text and ".wav" not in text.lower()
                assert "C:" not in text and "://" not in text


def test_the_palette_document_names_every_table_column_index_code_constant_and_slot(library):
    text = DOCUMENT.read_text(encoding="utf-8")
    for table in ("projects", "palettes", "palette_items"):
        assert f"### {table}" in text, table
        for row in library.connection.execute(f"PRAGMA table_info({table})"):
            assert f"`{row[1]}`" in text, f"{table}.{row[1]}"
    for index in ("ux_palette_items_active_slot", "idx_palette_items_sample"):
        assert index in text, index
    for constant in ("MVP_SLOTS", "SLOT_ROLES", "PALETTE_HASH_VERSION", "SONG_CONTEXT_ABSENT_REASON",
                     "CONTEXT_STATES", "palette-hash-v1", "song_context_absent"):
        assert constant in text, constant
    for state in CONTEXT_STATES:
        assert f"`{state}`" in text, state
    for slot in MVP_SLOTS:
        assert f"`{slot}`" in text, slot
    for role in ("kick", "bass", "sub-bass"):
        assert f"`{role}`" in text, role
    for code in ("unknown_project", "unknown_palette", "unknown_slot", "role_mismatch",
                 "invalid_context", "revision_conflict", "palette_incomplete", "unknown_sample",
                 "invalid_sample", "write_failed", "database_locked", "migration_failed",
                 "not_a_database", "database_corrupt", "schema_version_newer"):
        assert code in text, code
    for method in ("create_project", "get_project", "list_projects", "delete_project",
                   "load_palette", "list_palettes", "set_palette_item", "remove_palette_item",
                   "set_palette_context", "palette_hash", "to_context"):
        assert method in text, method
    for repository_name in ("ProjectRecord", "PaletteRecord", "PaletteItemRecord",
                            "PaletteContextState", "PaletteMutation"):
        assert repository_name in text, repository_name
