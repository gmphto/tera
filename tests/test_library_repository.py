"""Repository identity, transactions and reads for the sample library (#21).

Every database lives in `tmp_path`. Paths in this file are either temporary
directories or the synthetic `C:/tera-fixtures/...` values already used by the
contract fixtures; no real library path appears here.
"""

import builtins
import hashlib
import json
import os
import sqlite3
import time
from dataclasses import fields, replace
from pathlib import Path

import pytest

from backend.analysis import batch
from backend.contracts import RecommendationBatch, Sample
from backend.library.errors import (
    AnalysisVersionMismatch,
    DatabaseLocked,
    DuplicateContent,
    IncompleteFeatures,
    InvalidContentIdentity,
    InvalidFileStatus,
    InvalidSample,
    InvalidTag,
    PathConflict,
    UnknownAnalysisVersion,
    UnknownSample,
    WriteFailed,
)
from backend.library.repository import (
    ImportResult,
    LibraryRepository,
    StoredSample,
    transaction,
)
from backend.library.schema import open_database, utc_now, verify


FIXTURES = Path(__file__).parent / "fixtures" / "contracts"
SILENT = "silent-sample.json"
HYBRID = "hybrid.json"
OTHER_CONTENT = "b" * 64


@pytest.fixture
def library(tmp_path):
    connection = open_database(tmp_path / "library.sqlite3")
    repository = LibraryRepository(connection)
    try:
        yield repository
    finally:
        connection.close()


def fixture_bytes(name=SILENT):
    return (FIXTURES / name).read_bytes()


def fixture_digest(name=SILENT):
    return hashlib.sha256(fixture_bytes(name)).hexdigest()


def silent_sample():
    return Sample.from_dict(json.loads((FIXTURES / SILENT).read_text(encoding="utf-8")))


def hybrid_samples():
    return RecommendationBatch.from_json(
        (FIXTURES / HYBRID).read_text(encoding="utf-8")).samples


def at_version(sample, version):
    return replace(sample, analysis_version=version)


def moved(sample, path, sample_id=None):
    result = replace(sample, audio=replace(sample.audio, local_path=str(path)))
    return result if sample_id is None else replace(result, sample_id=sample_id)


def register_version(repository):
    return repository.register_analysis_version(batch.analysis_descriptor())


def second_version(repository):
    return repository.register_analysis_version(
        {**batch.analysis_descriptor(), "batch": "test-v2"})


def tamper(value, **replacements):
    """A frozen dataclass copy with fields replaced, bypassing validation."""

    clone = object.__new__(type(value))
    for field in fields(value):
        object.__setattr__(clone, field.name,
                           replacements.get(field.name, getattr(value, field.name)))
    return clone


def count(connection, table):
    return connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]


def feature_rows(connection, sample_id, version):
    rows = connection.execute(
        "SELECT measurement, unit, value, unavailable_reason, confidence FROM sample_features "
        "WHERE sample_id = ? AND analysis_version = ? ORDER BY measurement",
        (sample_id, version)).fetchall()
    return tuple(tuple(row) for row in rows)


def key_row(connection, sample_id, version):
    row = connection.execute(
        "SELECT tonic, mode, confidence, unavailable_reason FROM sample_keys "
        "WHERE sample_id = ? AND analysis_version = ?", (sample_id, version)).fetchone()
    return None if row is None else tuple(row)


def added_at_of(connection, sample_id, tag):
    row = connection.execute("SELECT added_at FROM sample_tags WHERE sample_id = ? AND tag = ?",
                             (sample_id, tag)).fetchone()
    return None if row is None else row[0]


def insert_sample_row(connection, sample_id, content_sha256, path):
    connection.execute(
        "INSERT INTO samples (sample_id, schema_version, content_sha256, role, original_path, "
        "path_key, filename, pack_id, file_status, sample_rate_hz, channels, frame_count, "
        "duration_ms, imported_at, updated_at) "
        "VALUES (?, '1.0', ?, 'bass', ?, ?, 'sample.wav', NULL, 'unknown', 48000, 1, 24000, "
        "500.0, ?, ?)",
        (sample_id, content_sha256, path, os.path.normcase(os.path.abspath(path)),
         utc_now(), utc_now()))


def store_two_versions(repository):
    first = register_version(repository)
    second = second_version(repository)
    original = at_version(silent_sample(), first)
    repository.import_sample(original, content_sha256=fixture_digest())
    changed = replace(original, analysis_version=second, features=replace(
        original.features, measurements=tuple(
            replace(item, value=0.25) if item.name == "rms" else item
            for item in original.features.measurements)))
    repository.import_sample(changed, content_sha256=fixture_digest())
    return first, second, original, changed


def set_created_at(repository, version, value):
    repository.connection.execute(
        "UPDATE analysis_versions SET created_at = ? WHERE analysis_version = ?", (value, version))


# -- round trip ---------------------------------------------------------------

def test_a_silent_sample_round_trips_through_the_database(library):
    version = register_version(library)
    original = at_version(silent_sample(), version)
    result = library.import_sample(original, content_sha256=fixture_digest())
    assert result == ImportResult(sample_id="silent-001", analysis_version=version, created=True,
                                  path_changed=False, file_status="unknown")
    stored = library.get_sample("silent-001", version)
    assert isinstance(stored, StoredSample)
    assert isinstance(stored.sample, Sample)
    assert stored.sample.to_dict() == original.to_dict()
    assert stored.analysis_version == version
    assert stored.pack_id is None and stored.tags == ()
    assert count(library.connection, "sample_features") == 19
    assert verify(library.connection) == ()


def test_both_hybrid_samples_round_trip(library):
    version = register_version(library)
    for index, sample in enumerate(hybrid_samples()):
        original = at_version(sample, version)
        content = hashlib.sha256(f"hybrid-fixture-{index}".encode("utf-8")).hexdigest()
        assert library.import_sample(original, content_sha256=content).created is True
        stored = library.get_sample(original.sample_id, version)
        assert stored.sample.to_dict() == original.to_dict()
        assert stored.sample.features.key.to_dict() == original.features.key.to_dict()
    assert library.list_analysis_versions("kick-001") == (version,)


# -- versions -----------------------------------------------------------------

def test_registering_a_version_is_idempotent(library):
    descriptor = batch.analysis_descriptor()
    first = library.register_analysis_version(descriptor)
    assert first == batch.digest(descriptor)
    assert library.register_analysis_version(descriptor) == first
    assert count(library.connection, "analysis_versions") == 1
    with pytest.raises(InvalidSample) as raised:
        library.register_analysis_version({"extractors": {1, 2}})
    assert raised.value.code == "invalid_sample"


def test_two_analysis_versions_coexist_for_one_sample(library):
    first = register_version(library)
    second = second_version(library)
    assert first != second
    original = at_version(silent_sample(), first)
    library.import_sample(original, content_sha256=fixture_digest())
    before_features = feature_rows(library.connection, "silent-001", first)
    before_key = key_row(library.connection, "silent-001", first)
    changed = replace(original, analysis_version=second, features=replace(
        original.features, measurements=tuple(
            replace(item, value=0.25) if item.name == "rms" else item
            for item in original.features.measurements)))
    result = library.import_sample(changed, content_sha256=fixture_digest())
    assert result.analysis_version == second
    assert feature_rows(library.connection, "silent-001", first) == before_features
    assert key_row(library.connection, "silent-001", first) == before_key
    assert library.get_sample("silent-001", first).sample.to_dict() == original.to_dict()
    assert library.get_sample("silent-001", second).sample.to_dict() == changed.to_dict()
    assert library.list_analysis_versions("silent-001") == tuple(sorted((first, second)))


def test_the_default_version_prefers_the_newest_created_at(library):
    first, second, _original, _changed = store_two_versions(library)
    set_created_at(library, first, "2024-03-01T00:00:00Z")
    set_created_at(library, second, "2024-02-01T00:00:00Z")
    assert library.get_sample("silent-001").analysis_version == first
    set_created_at(library, first, "2024-01-01T00:00:00Z")
    set_created_at(library, second, "2024-02-01T00:00:00Z")
    assert library.get_sample("silent-001").analysis_version == second


def test_the_default_version_breaks_ties_on_the_version_ascending(library):
    first, second, _original, _changed = store_two_versions(library)
    set_created_at(library, first, "2024-01-01T00:00:00Z")
    set_created_at(library, second, "2024-01-01T00:00:00Z")
    assert library.get_sample("silent-001").analysis_version == min(first, second)


def test_an_unregistered_version_and_an_unknown_id_are_answered(library):
    first = register_version(library)
    second = second_version(library)
    library.import_sample(at_version(silent_sample(), first), content_sha256=fixture_digest())
    assert library.get_sample("sample-999") is None
    assert library.find_sample_by_path("C:/tera-fixtures/nope.wav") is None
    assert library.find_by_content(OTHER_CONTENT) is None
    assert library.list_analysis_versions("sample-999") == ()
    with pytest.raises(UnknownAnalysisVersion) as raised:
        library.get_sample("silent-001", second)
    assert raised.value.code == "unknown_analysis_version"


def test_a_tampered_descriptor_is_reported(library):
    version = register_version(library)
    library.import_sample(at_version(silent_sample(), version), content_sha256=fixture_digest())
    library.connection.execute(
        "UPDATE analysis_versions SET descriptor = ? WHERE analysis_version = ?",
        ('{"batch":"tampered"}', version))
    with pytest.raises(AnalysisVersionMismatch) as raised:
        library.get_sample("silent-001", version)
    assert raised.value.code == "analysis_version_mismatch"
    with pytest.raises(AnalysisVersionMismatch):
        library.list_analysis_versions("silent-001")


# -- payload validation -------------------------------------------------------

def test_a_payload_that_fails_sample_validation_is_refused(library):
    version = register_version(library)
    original = at_version(silent_sample(), version)
    broken = tamper(original, sample_id="sample-002", role="banjo")
    with pytest.raises(InvalidSample) as raised:
        library.import_sample(broken, content_sha256=OTHER_CONTENT)
    assert raised.value.code == "invalid_sample"
    assert library.get_sample("sample-002") is None
    with pytest.raises(InvalidSample):
        library.import_sample({"sample_id": "sample-003"}, content_sha256=OTHER_CONTENT)
    with pytest.raises(InvalidSample):
        library.import_sample("not a sample", content_sha256=OTHER_CONTENT)
    assert count(library.connection, "samples") == 0


def test_a_measurement_unit_that_disagrees_with_measures_is_refused(library):
    version = register_version(library)
    original = at_version(silent_sample(), version)
    measurements = original.features.measurements
    wrong = tamper(measurements[0], unit="BPM")
    features = tamper(original.features, measurements=(wrong,) + measurements[1:])
    with pytest.raises(InvalidSample) as raised:
        library.import_sample(tamper(original, features=features),
                              content_sha256=fixture_digest())
    assert raised.value.code == "invalid_sample"
    assert library.get_sample("silent-001") is None


def test_invalid_content_identity_is_refused(library, tmp_path):
    version = register_version(library)
    sample = at_version(moved(silent_sample(), tmp_path / "a.wav"), version)
    with pytest.raises(TypeError):
        library.import_sample(sample)
    for bad in ("", "not-a-hash", "A" * 64, "a" * 63, 7, None):
        with pytest.raises(InvalidContentIdentity) as raised:
            library.import_sample(sample, content_sha256=bad)
        assert raised.value.code == "invalid_content_identity"
    addressed = at_version(
        moved(silent_sample(), tmp_path / "b.wav", sample_id="sha256:" + "0" * 64), version)
    with pytest.raises(InvalidContentIdentity):
        library.import_sample(addressed, content_sha256="c" * 64)
    assert library.get_sample(addressed.sample_id) is None
    matching = at_version(
        moved(silent_sample(), tmp_path / "c.wav", sample_id="sha256:" + "c" * 64), version)
    assert library.import_sample(matching, content_sha256="c" * 64).created is True


def test_an_unregistered_analysis_version_is_refused(library, tmp_path):
    unknown = "f" * 64
    sample = at_version(moved(silent_sample(), tmp_path / "a.wav"), unknown)
    with pytest.raises(UnknownAnalysisVersion) as raised:
        library.import_sample(sample, content_sha256=fixture_digest())
    assert raised.value.code == "unknown_analysis_version"
    assert library.get_sample("silent-001") is None
    assert count(library.connection, "samples") == 0


# -- duplicate content and paths ----------------------------------------------

def test_duplicate_content_on_one_id_moves_the_path(library, tmp_path):
    version = register_version(library)
    first_path = tmp_path / "first" / "sample-001.wav"
    second_path = tmp_path / "second" / "sample-001.wav"
    content = fixture_digest()
    original = at_version(moved(silent_sample(), first_path), version)
    assert library.import_sample(original, content_sha256=content) == ImportResult(
        "silent-001", version, True, False, "unknown")
    before = library.get_sample("silent-001", version)
    assert library.import_sample(moved(original, second_path), content_sha256=content) == ImportResult(
        "silent-001", version, False, True, "unknown")
    after = library.get_sample("silent-001", version)
    assert after.sample.audio.local_path == str(second_path)
    assert after.imported_at == before.imported_at
    assert after.updated_at >= before.updated_at
    assert count(library.connection, "samples") == 1
    assert library.find_sample_by_path(first_path) is None
    assert library.find_sample_by_path(second_path).sample.sample_id == "silent-001"
    assert library.find_by_content(content).sample.sample_id == "silent-001"


def test_duplicate_content_under_another_id_is_refused(library, tmp_path):
    version = register_version(library)
    content = fixture_digest()
    library.import_sample(at_version(moved(silent_sample(), tmp_path / "a.wav"), version),
                          content_sha256=content)
    other = at_version(moved(silent_sample(), tmp_path / "b.wav", sample_id="sample-002"), version)
    with pytest.raises(DuplicateContent) as raised:
        library.import_sample(other, content_sha256=content)
    assert raised.value.code == "duplicate_content"
    assert "silent-001" in str(raised.value)
    assert library.get_sample("sample-002") is None
    assert library.find_sample_by_path(tmp_path / "b.wav") is None


def test_a_path_conflict_is_refused(library, tmp_path):
    version = register_version(library)
    path = tmp_path / "shared" / "sample.wav"
    library.import_sample(at_version(moved(silent_sample(), path), version),
                          content_sha256=fixture_digest())
    other = at_version(moved(silent_sample(), path, sample_id="sample-002"), version)
    with pytest.raises(PathConflict) as raised:
        library.import_sample(other, content_sha256=OTHER_CONTENT)
    assert raised.value.code == "path_conflict"
    assert library.get_sample("sample-002") is None


@pytest.mark.skipif(os.name != "nt", reason="Windows path normalisation")
def test_paths_differing_in_case_or_separators_share_a_path_key(library, tmp_path):
    version = register_version(library)
    upper = tmp_path / "Sample.WAV"
    variant = str(tmp_path).replace("\\", "/") + "/sample.wav"
    assert (os.path.normcase(os.path.abspath(variant))
            == os.path.normcase(os.path.abspath(str(upper))))
    library.import_sample(at_version(moved(silent_sample(), upper), version),
                          content_sha256=fixture_digest())
    other = at_version(moved(silent_sample(), variant, sample_id="sample-002"), version)
    with pytest.raises(PathConflict):
        library.import_sample(other, content_sha256=OTHER_CONTENT)
    assert library.get_sample("sample-002") is None


# -- completeness, rollback and transactions ----------------------------------

def test_a_missing_measurement_or_key_row_is_refused(library):
    version = register_version(library)
    original = at_version(silent_sample(), version)
    library.import_sample(original, content_sha256=fixture_digest())
    connection = library.connection
    connection.execute(
        "DELETE FROM sample_features WHERE sample_id = ? AND analysis_version = ? "
        "AND measurement = 'rms'", ("silent-001", version))
    with pytest.raises(IncompleteFeatures) as raised:
        library.get_sample("silent-001", version)
    assert raised.value.code == "incomplete_features"
    library.import_sample(original, content_sha256=fixture_digest())
    connection.execute("DELETE FROM sample_keys WHERE sample_id = ? AND analysis_version = ?",
                       ("silent-001", version))
    with pytest.raises(IncompleteFeatures):
        library.get_sample("silent-001", version)
    with pytest.raises(IncompleteFeatures):
        library.list_samples()


def test_every_key_row_has_its_nineteen_measurements(library):
    version = register_version(library)
    library.import_sample(at_version(silent_sample(), version), content_sha256=fixture_digest())
    connection = library.connection
    incomplete = connection.execute(
        "SELECT k.sample_id, k.analysis_version FROM sample_keys AS k "
        "LEFT JOIN sample_features AS f ON f.sample_id = k.sample_id "
        "AND f.analysis_version = k.analysis_version "
        "GROUP BY k.sample_id, k.analysis_version "
        "HAVING count(f.measurement) <> 19").fetchall()
    assert incomplete == []
    assert count(connection, "sample_keys") == 1


def test_import_rolls_back_on_a_mid_write_failure(library, tmp_path):
    version = register_version(library)
    connection = library.connection
    connection.execute(
        "CREATE TRIGGER refuse_tags BEFORE INSERT ON sample_tags "
        "BEGIN SELECT RAISE(ABORT, 'no tags'); END")
    sample = at_version(moved(silent_sample(), tmp_path / "a.wav"), version)
    with pytest.raises(WriteFailed) as raised:
        library.import_sample(sample, content_sha256=fixture_digest(), tags=("boom",))
    assert raised.value.code == "write_failed"
    assert isinstance(raised.value.__cause__, sqlite3.Error)
    for table in ("samples", "sample_features", "sample_keys", "sample_tags"):
        assert count(connection, table) == 0
    assert verify(connection) == ()


def test_the_transaction_context_manager_rolls_back(library, tmp_path):
    register_version(library)
    connection = library.connection
    with pytest.raises(RuntimeError):
        with transaction(connection):
            insert_sample_row(connection, "sample-001", fixture_digest(), str(tmp_path / "a.wav"))
            assert count(connection, "samples") == 1
            raise RuntimeError("boom")
    assert count(connection, "samples") == 0
    assert count(connection, "sample_features") == 0
    assert library.get_sample("sample-001") is None
    assert verify(connection) == ()


# -- tags ---------------------------------------------------------------------

def test_tags_normalise_and_stay_unique(library):
    version = register_version(library)
    library.import_sample(at_version(silent_sample(), version), content_sha256=fixture_digest())
    assert library.add_tag("silent-001", "  Kick  ") is True
    added_at = added_at_of(library.connection, "silent-001", "kick")
    assert added_at is not None
    assert library.add_tag("silent-001", "kick") is False
    assert library.add_tag("silent-001", "KICK") is False
    assert added_at_of(library.connection, "silent-001", "kick") == added_at
    assert library.list_tags("silent-001") == ("kick",)
    assert library.remove_tag("silent-001", " KICK ") is True
    assert library.remove_tag("silent-001", "kick") is False
    assert library.list_tags("silent-001") == ()


@pytest.mark.parametrize("bad", ["", "   ", "x" * 65, 7, None, ["kick"]])
def test_invalid_tags_are_refused(library, bad):
    version = register_version(library)
    library.import_sample(at_version(silent_sample(), version), content_sha256=fixture_digest())
    with pytest.raises(InvalidTag) as raised:
        library.add_tag("silent-001", bad)
    assert raised.value.code == "invalid_tag"
    assert library.list_tags("silent-001") == ()


def test_tag_and_pack_operations_refuse_an_unknown_sample(library):
    register_version(library)
    calls = (lambda: library.add_tag("sample-999", "kick"),
             lambda: library.remove_tag("sample-999", "kick"),
             lambda: library.list_tags("sample-999"),
             lambda: library.set_pack("sample-999", None))
    for call in calls:
        with pytest.raises(UnknownSample) as raised:
            call()
        assert raised.value.code == "unknown_sample"


# -- packs --------------------------------------------------------------------

def test_packs_link_and_survive_pack_deletion(library):
    version = register_version(library)
    library.import_sample(at_version(silent_sample(), version), content_sha256=fixture_digest())
    connection = library.connection
    library.upsert_pack("pack-001", "Test Pack One", "Vendor One")
    created_at = connection.execute(
        "SELECT created_at FROM sample_packs WHERE pack_id = 'pack-001'").fetchone()[0]
    library.upsert_pack("pack-001", "Test Pack One", "Vendor Two")
    library.upsert_pack("pack-001", "Test Pack One", "Vendor Two")
    rows = connection.execute("SELECT pack_id, name, vendor FROM sample_packs").fetchall()
    assert [tuple(row) for row in rows] == [("pack-001", "Test Pack One", "Vendor Two")]
    assert connection.execute(
        "SELECT created_at FROM sample_packs WHERE pack_id = 'pack-001'").fetchone()[0] == created_at
    assert library.set_pack("silent-001", "pack-001") is None
    assert library.get_sample("silent-001").pack_id == "pack-001"
    with pytest.raises(WriteFailed):
        library.set_pack("silent-001", "pack-999")
    connection.execute("DELETE FROM sample_packs WHERE pack_id = 'pack-001'")
    stored = library.get_sample("silent-001")
    assert stored.pack_id is None
    assert stored.sample.sample_id == "silent-001"
    with pytest.raises(WriteFailed):
        library.set_pack("silent-001", "pack-001")
    library.upsert_pack("pack-001", "Test Pack One")
    library.set_pack("silent-001", "pack-001")
    assert library.get_sample("silent-001").pack_id == "pack-001"
    library.set_pack("silent-001", None)
    assert library.get_sample("silent-001").pack_id is None


# -- missing files and deletion ------------------------------------------------

def test_a_missing_file_keeps_its_rows_and_status(library, tmp_path):
    version = register_version(library)
    missing = tmp_path / "gone" / "sample-001.wav"
    original = at_version(moved(silent_sample(), missing), version)
    assert library.import_sample(original, content_sha256=fixture_digest()).file_status == "unknown"
    assert not missing.exists()
    before = library.get_sample("silent-001")
    library.mark_file_status("silent-001", "missing")
    after = library.get_sample("silent-001")
    assert after.file_status == "missing"
    assert after.sample.to_dict() == before.sample.to_dict()
    assert library.list_samples() == (after,)
    assert count(library.connection, "sample_features") == 19
    assert library.list_samples(role="kick") == ()
    assert library.list_samples(role="bass") == (after,)
    with pytest.raises(InvalidFileStatus) as raised:
        library.mark_file_status("silent-001", "gone")
    assert raised.value.code == "invalid_file_status"
    with pytest.raises(UnknownSample):
        library.mark_file_status("sample-999", "missing")


def test_list_samples_filters_by_role_without_touching_the_filesystem(library, tmp_path):
    version = register_version(library)
    for index, sample in enumerate(hybrid_samples()):
        content = hashlib.sha256(f"hybrid-fixture-{index}".encode("utf-8")).hexdigest()
        library.import_sample(at_version(moved(sample, tmp_path / f"{index}.wav"), version),
                              content_sha256=content)
    assert [item.sample.sample_id for item in library.list_samples()] == ["bass-001", "kick-001"]
    assert [item.sample.role for item in library.list_samples(role="kick")] == ["kick"]
    assert library.list_samples(role="sub-bass") == ()


def test_foreign_keys_are_enforced_for_features(library, tmp_path):
    register_version(library)
    connection = library.connection
    insert_sample_row(connection, "sample-001", fixture_digest(), str(tmp_path / "a.wav"))
    assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO sample_features (sample_id, analysis_version, measurement, unit, value, "
            "unavailable_reason, confidence) "
            "VALUES ('sample-001', ?, 'rms', 'linear', 0.5, NULL, NULL)", ("f" * 64,))
    assert count(connection, "sample_features") == 0
    assert count(connection, "samples") == 1


def test_no_other_column_records_a_library_path(library, tmp_path):
    version = register_version(library)
    path = tmp_path / "nested" / "sample-001.wav"
    library.import_sample(at_version(moved(silent_sample(), path), version),
                          content_sha256=fixture_digest(), tags=("kick",))
    library.upsert_pack("pack-001", "Test Pack One")
    library.set_pack("silent-001", "pack-001")
    allowed = {("samples", "original_path"), ("samples", "path_key"), ("samples", "filename")}
    found = set()
    for table in ("analysis_versions", "sample_packs", "samples", "sample_features",
                  "sample_keys", "sample_tags"):
        for column in library.connection.execute(f"PRAGMA table_info({table})").fetchall():
            if (column[2] or "").upper() != "TEXT":
                continue
            rows = library.connection.execute(f'SELECT "{column[1]}" FROM {table}').fetchall()
            if any(isinstance(row[0], str) and "sample-001.wav" in row[0] for row in rows):
                found.add((table, column[1]))
    assert found == allowed


def test_delete_sample_cascades_and_keeps_versions_and_packs(library):
    version = register_version(library)
    library.import_sample(at_version(silent_sample(), version), content_sha256=fixture_digest())
    library.upsert_pack("pack-001", "Test Pack One")
    library.set_pack("silent-001", "pack-001")
    library.add_tag("silent-001", "kick")
    connection = library.connection
    assert library.delete_sample("silent-001") is True
    assert library.delete_sample("silent-001") is False
    for table in ("samples", "sample_features", "sample_keys", "sample_tags"):
        assert count(connection, table) == 0
    assert count(connection, "analysis_versions") == 1
    assert count(connection, "sample_packs") == 1
    assert verify(connection) == ()


def test_import_never_opens_or_stats_the_audio_file(library, tmp_path, monkeypatch):
    version = register_version(library)
    audio = tmp_path / "sample-001.wav"
    wanted = os.path.normcase(os.path.abspath(str(audio)))
    touched = []

    def is_audio(path):
        try:
            return os.path.normcase(os.path.abspath(os.fspath(path))) == wanted
        except TypeError:
            return False

    def refuse(*args, **kwargs):
        touched.append(args)
        raise AssertionError("import_sample touched the audio file")

    real_path_open, real_open, real_stat = Path.open, builtins.open, os.stat

    def guarded_path_open(self, *args, **kwargs):
        return refuse() if is_audio(self) else real_path_open(self, *args, **kwargs)

    def guarded_open(file, *args, **kwargs):
        return refuse() if is_audio(file) else real_open(file, *args, **kwargs)

    def guarded_stat(path, *args, **kwargs):
        return refuse() if is_audio(path) else real_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded_path_open)
    monkeypatch.setattr(builtins, "open", guarded_open)
    monkeypatch.setattr(os, "stat", guarded_stat)
    original = at_version(moved(silent_sample(), audio), version)
    assert library.import_sample(original, content_sha256=fixture_digest()).created is True
    assert touched == []
    assert library.get_sample("silent-001").sample.to_dict() == original.to_dict()


# -- concurrency ---------------------------------------------------------------

def test_a_reader_sees_committed_state_only(tmp_path):
    path = tmp_path / "library.sqlite3"
    writer = open_database(path)
    reader = open_database(path)
    try:
        writing = LibraryRepository(writer)
        reading = LibraryRepository(reader)
        version = register_version(writing)
        original = at_version(moved(silent_sample(), tmp_path / "a.wav"), version)
        writing.import_sample(original, content_sha256=fixture_digest())
        before = reading.get_sample("silent-001")
        with transaction(writer):
            writer.execute("UPDATE samples SET updated_at = ? WHERE sample_id = ?",
                           ("2024-01-01T00:00:00Z", "silent-001"))
            assert reading.get_sample("silent-001").updated_at == before.updated_at
        after = reading.get_sample("silent-001")
        assert after.updated_at == "2024-01-01T00:00:00Z"
        assert after.sample.to_dict() == before.sample.to_dict()
    finally:
        writer.close()
        reader.close()


def test_a_second_writer_times_out_and_succeeds_after_the_commit(tmp_path):
    path = tmp_path / "library.sqlite3"
    writer = open_database(path)
    reader = open_database(path)
    try:
        writing = LibraryRepository(writer)
        reading = LibraryRepository(reader)
        version = register_version(writing)
        original = at_version(moved(silent_sample(), tmp_path / "a.wav"), version)
        writing.import_sample(original, content_sha256=fixture_digest())
        reader.execute("PRAGMA busy_timeout = 200")
        with transaction(writer):
            writer.execute("UPDATE samples SET updated_at = ? WHERE sample_id = ?",
                           ("2024-01-01T00:00:00Z", "silent-001"))
            started = time.monotonic()
            with pytest.raises(DatabaseLocked) as raised:
                reading.mark_file_status("silent-001", "missing")
            waited = time.monotonic() - started
        assert raised.value.code == "database_locked"
        assert waited >= 0.1
        assert reading.get_sample("silent-001").file_status == "unknown"
        reading.mark_file_status("silent-001", "missing")
        assert reading.get_sample("silent-001").file_status == "missing"
    finally:
        writer.close()
        reader.close()
