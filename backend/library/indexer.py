"""Derived analysis-queue queries for the local sample library (issue #22).

`pending_analysis(connection)` is the whole job queue: the scanner writes no job
row, no attempt and no progress, so what still needs analysis is derived from
stored state instead of recorded next to it. The current analysis version is
`digest(analysis_descriptor())` from `backend.analysis.batch`, never a second
digest or version string.

One request is one *content identity*: a `samples.content_sha256` that no row
holds a current analysis for. A row whose stored availability is `missing` is
not requested, because #23 cannot read a file the last scan found gone; the row
returns to the queue by itself as soon as a scan finds the file again.

Only the standard library and `backend.analysis.batch` are imported here.
"""

from __future__ import annotations

from dataclasses import dataclass
import os

from backend.analysis.batch import analysis_descriptor, digest


REQUEST_FIELDS = ("sample_id", "fingerprint", "analysis_version", "path", "role")


@dataclass(frozen=True)
class AnalysisRequest:
    """One unit of pending analysis work, keyed by content identity.

    `sample_id` is the contract identity of the chosen row, `fingerprint` is the
    64-hex content hash, `analysis_version` is the version to store,
    `path` is the stored local path to read and `role` is the stored role.
    """

    sample_id: str
    fingerprint: str
    analysis_version: str
    path: str
    role: str


def current_analysis_version() -> str:
    """The digest of the analysis descriptor in force today."""

    return digest(analysis_descriptor())


def pending_analysis(connection) -> tuple:
    """One `AnalysisRequest` per content identity lacking a current analysis.

    Ordered deterministically by `(analysis_version, sample_id)`. When several
    rows share one content identity — which the version-1 schema's unique
    `content_sha256` still forbids — the row with the lexicographically smallest
    normalised path is chosen, so the request is stable across runs.

    The connection is one from `backend.library.schema.open_database`.
    """

    version = current_analysis_version()
    rows = connection.execute(
        "SELECT sample_id, content_sha256, role, original_path FROM samples "
        "WHERE file_status <> 'missing' "
        "AND NOT EXISTS (SELECT 1 FROM sample_features AS f "
        "                WHERE f.sample_id = samples.sample_id AND f.analysis_version = ?) "
        "AND NOT EXISTS (SELECT 1 FROM sample_keys AS k "
        "                WHERE k.sample_id = samples.sample_id AND k.analysis_version = ?)",
        (version, version)).fetchall()
    chosen = {}
    for sample_id, fingerprint, role, path in rows:
        key = os.path.normcase(os.path.abspath(path))
        current = chosen.get(fingerprint)
        if current is None or (key, sample_id) < (current[0], current[1]):
            chosen[fingerprint] = (key, sample_id, role, path)
    requests = [AnalysisRequest(sample_id, fingerprint, version, path, role)
                for fingerprint, (_key, sample_id, role, path) in chosen.items()]
    requests.sort(key=lambda request: (request.analysis_version, request.sample_id))
    return tuple(requests)
