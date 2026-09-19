"""Minimal local pair-rating session utility; see _docs/pair-rating-workflow.md.

One session plays an assigned kick+bass pair list under the frozen blind protocol in
_docs/evaluation-protocol.md, records the four labels with a separately recorded skip,
keeps every answer durable, resumes without losing or duplicating one and exports records
that satisfy the protocol's "Evidence #17 must record" contract. Every file it writes
lives under <cwd>/.local-evaluation/pair-rating/.
"""

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import uuid

from backend import audio
from backend.evaluation import playback
from backend.evaluation.manifest import canonical, fields, mapping_path, read_json, require


SCHEMA_VERSION = "1.0"
SUPPORTED_PROTOCOL_VERSION = "tera-eval-protocol-v1"
PROTOCOL_DOCUMENT = Path("_docs") / "evaluation-protocol.md"
REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
LABELS = ("poor", "acceptable", "good", "excellent")
LABEL_INPUTS = {"1": "poor", "2": "acceptable", "3": "good", "4": "excellent"}
SKIP_REASONS = ("failed-playback", "cannot-judge", "recognised", "other")
ORDER_SEED = "tera-eval-order-16-v1"
MAX_RECOGNISED_RATE = 0.20
SESSION_FIELDS = ("session_id", "evaluator_id", "protocol_version", "dataset_version",
                  "split_manifest_digest", "order_seed", "playback_gain_db",
                  "monitoring_description", "started_at", "finished_at")
RATING_FIELDS = ("pair_id", "kick_sample_id", "bass_sample_id", "presentation_index", "rating",
                 "skip", "skip_reason", "recognised", "playback_completed", "responded_at",
                 "correction_of")
SESSION_DOCUMENT_FIELDS = "schema_version " + " ".join(SESSION_FIELDS) + " state presentations"
PRESENTATION_FIELDS = "presentation_index pair_id kick_sample_id bass_sample_id"
EXPORT_FIELDS = "schema_version session ratings"
PAIR_LIST_FIELDS = "schema_version dataset_version split_manifest_digest sampler_seed assignment_seed pairs"
PAIR_FIELDS = "pair_id kick_sample_id bass_sample_id"
ROLES = ("kick", "bass")
STATES = ("in_progress", "complete")
SESSION_FILE = "session.json"
JOURNAL_FILE = "answers.jsonl"
EXPORT_FILE = "export.json"
INSTRUCTIONS_FILE = "instructions.txt"
PLAYBACK_DIRECTORY = "playback"
LOCK_FILE = ".lock"
PRIVATE_DIRECTORIES = (".local-evaluation", "pair-rating")
SESSIONS_DIRECTORY = "sessions"
SESSION_ID_PATTERN = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
EVALUATOR_ID_PATTERN = re.compile(r"[a-z0-9][a-z0-9-]{0,31}")
PAIR_ID_PATTERN = re.compile(r"[A-Za-z0-9:_.-]{1,128}")
DIGEST_PATTERN = re.compile(r"sha256:[0-9a-f]{64}")
TIMESTAMP_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})")

class RatingError(ValueError):
    """A refusal code plus a path-free message; the command exits 2."""

    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class Protocol:
    version: str
    order_seed: str
    max_recognised_rate: float
    instructions: str


@dataclass
class Context:
    directory: Path
    document: dict
    dataset: dict
    records: list
    backend: str
    player_command: str | None
    instructions: str
    limit: float


def timestamp():
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def private_root():
    return Path.cwd().joinpath(*PRIVATE_DIRECTORIES)


def private_path(value, root):
    """Resolve a user path and refuse anything outside the private root."""
    resolved = Path(value).resolve()
    if not _inside(resolved, Path(root).resolve()):
        raise RatingError("outside_private_root",
                          "Every file this utility writes stays under <cwd>/.local-evaluation/pair-rating/.")
    return resolved


def _inside(path, root):
    name, base = os.path.normcase(str(path)), os.path.normcase(str(root))
    return name == base or name.startswith(base + os.sep)


def _atomic_replace(source, destination):
    os.replace(source, destination)


def _atomic_text(path, text):
    temporary = None
    try:
        descriptor, temporary = tempfile.mkstemp(dir=str(Path(path).parent),
                                                 prefix=Path(path).name + ".", suffix=".tmp")
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        _atomic_replace(temporary, str(path))
        temporary = None
    except OSError as error:
        raise RatingError("storage_failure",
                          "A private file could not be written; the previous content is unchanged.") from error
    finally:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)


def write_journal(path, records):
    _atomic_text(path, "".join(canonical(record) + "\n" for record in records))


def read_document(path, code):
    try:
        document = read_json(path)
    except (OSError, ValueError) as error:
        raise RatingError(code, "A private document is missing or unreadable.") from error
    try:
        require(isinstance(document, dict), "A private document must hold one JSON object.")
    except ValueError as error:
        raise RatingError(code, "A private document must hold one JSON object.") from error
    return document


def read_journal(path):
    """Return (records, violations); a torn or unparseable line is a violation."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as error:
        raise RatingError("journal_unreadable", "The answers journal cannot be read.") from error
    records, violations = [], []
    if text and not text.endswith("\n"):
        violations.append(violation("invalid_field_set", None, "the journal does not end with a newline"))
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            violations.append(violation("invalid_field_set", None, f"journal line {number} is blank"))
            continue
        try:
            records.append(json.loads(line))
        except ValueError:
            violations.append(violation("invalid_field_set", None, f"journal line {number} is not JSON"))
    return records, violations


def violation(code, index, detail):
    return {"code": code, "presentation_index": index, "detail": detail}


def preflight(code, context):
    return {"code": code, "context": context}

# --- the protocol document -------------------------------------------------

MARK = "\N{GRAVE ACCENT}"


def section(text, heading):
    lines, collecting = [], False
    for line in text.splitlines():
        if line.strip().startswith("#"):
            if collecting:
                break
            collecting = line.strip() == heading
            continue
        if collecting:
            lines.append(line)
    return "\n".join(lines)


def protocol_constants(text):
    """Read the frozen-constants table; marked values stay text, plain values become numbers."""
    constants = {}
    for line in section(text, "### Frozen constants").splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) < 2:
            continue
        name = cells[0].strip(MARK)
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", name):
            continue
        raw = cells[1]
        if raw.startswith(MARK) and raw.endswith(MARK) and len(raw) > 2:
            constants[name] = raw.strip(MARK)
            continue
        try:
            constants[name] = float(raw)
        except ValueError:
            continue
    return constants


def instruction_text(text):
    """Return the evaluator instruction blockquote of the protocol document."""
    lines = []
    for line in section(text, "### Evaluator instruction text").splitlines():
        if line.startswith(">"):
            lines.append(line[1:].lstrip(" "))
        elif lines:
            break
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines).strip()


def load_protocol(path):
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as error:
        raise RatingError("protocol_version_mismatch",
                          "The protocol document is missing or unreadable at the resolved location.") from error
    constants = protocol_constants(text)
    version = constants.get("PROTOCOL_VERSION")
    if version != SUPPORTED_PROTOCOL_VERSION:
        raise RatingError("protocol_version_mismatch",
                          "The protocol document does not declare the supported version.")
    order_seed = constants.get("ORDER_SEED")
    limit = constants.get("MAX_RECOGNISED_RATE")
    if type(order_seed) is not str or not order_seed or type(limit) is not float:
        raise RatingError("protocol_constants_missing",
                          "The protocol document is missing ORDER_SEED or MAX_RECOGNISED_RATE.")
    if order_seed != ORDER_SEED:
        raise RatingError("protocol_constants_missing",
                          "The protocol document does not declare the pinned ORDER_SEED.")
    instructions = instruction_text(text)
    if not instructions:
        raise RatingError("protocol_constants_missing",
                          "The protocol document carries no evaluator instruction text.")
    return Protocol(version, order_seed, limit, instructions)


def document_path(arguments):
    chosen = getattr(arguments, "protocol_doc", None)
    return Path(chosen) if chosen else REPOSITORY_ROOT / PROTOCOL_DOCUMENT

# --- inputs ----------------------------------------------------------------

def check_dataset_document(document):
    if type(document) is not dict:
        return [preflight("schema_mismatch", "dataset")]
    problems = []
    version = document.get("dataset_version")
    if type(version) is not str or not version.strip():
        problems.append(preflight("schema_mismatch", "dataset.dataset_version"))
    sources = document.get("sources")
    if type(sources) is not dict or not sources:
        problems.append(preflight("schema_mismatch", "dataset.sources"))
    else:
        for source_id, source in sources.items():
            if type(source_id) is not str or not source_id or type(source) is not dict \
                    or type(source.get("root")) is not str or not source["root"]:
                problems.append(preflight("schema_mismatch", "dataset.sources"))
                break
    selected = document.get("selected")
    if type(selected) is not list or not selected:
        problems.append(preflight("schema_mismatch", "dataset.selected"))
    else:
        for record in selected:
            if type(record) is not dict or type(record.get("sample_id")) is not str \
                    or not record["sample_id"].strip() or record.get("role") not in ROLES \
                    or type(record.get("mapping")) is not dict \
                    or type(record["mapping"].get("source")) is not str \
                    or type(record["mapping"].get("path")) is not str \
                    or (type(sources) is dict and record["mapping"].get("source") not in sources):
                problems.append(preflight("schema_mismatch", "dataset.selected"))
                break
    return problems


def load_dataset(path):
    document = read_document(path, "schema_mismatch")
    problems = check_dataset_document(document)
    if problems:
        report(problems)
        return None
    return document


def check_pair_list_document(document):
    if type(document) is not dict or set(document) != set(PAIR_LIST_FIELDS.split()):
        return [preflight("schema_mismatch", "pair_list")]
    problems = []
    if document["schema_version"] != SCHEMA_VERSION:
        problems.append(preflight("schema_mismatch", "pair_list.schema_version"))
    if type(document["dataset_version"]) is not str or not document["dataset_version"].strip():
        problems.append(preflight("schema_mismatch", "pair_list.dataset_version"))
    if type(document["split_manifest_digest"]) is not str \
            or not DIGEST_PATTERN.fullmatch(document["split_manifest_digest"]):
        problems.append(preflight("schema_mismatch", "pair_list.split_manifest_digest"))
    for name in ("sampler_seed", "assignment_seed"):
        if type(document[name]) is not str or not document[name].strip():
            problems.append(preflight("schema_mismatch", "pair_list." + name))
    if type(document["pairs"]) is not list or not document["pairs"]:
        problems.append(preflight("schema_mismatch", "pair_list.pairs"))
        return problems
    for pair in document["pairs"]:
        if type(pair) is not dict or set(pair) != set(PAIR_FIELDS.split()):
            problems.append(preflight("schema_mismatch", "pair_list.pairs"))
            return problems
        context = pair["pair_id"] if type(pair["pair_id"]) is str and pair["pair_id"] else "pair_list.pairs"
        if type(pair["pair_id"]) is not str or not PAIR_ID_PATTERN.fullmatch(pair["pair_id"]):
            problems.append(preflight("schema_mismatch", context))
        for name in ("kick_sample_id", "bass_sample_id"):
            if type(pair[name]) is not str or not pair[name].strip():
                problems.append(preflight("schema_mismatch", context))
    return problems


def load_pair_list(path):
    document = read_document(path, "schema_mismatch")
    problems = check_pair_list_document(document)
    if problems:
        report(problems)
        return None
    return document


def resolve_audio(dataset):
    return {record["sample_id"]: mapping_path(dataset["sources"], record["mapping"])
            for record in dataset["selected"] if type(record) is dict}


def check_assignments(dataset, pair_list):
    """Every assigned pair: unique id, resolved roles, readable audio, one shared sample rate."""
    problems = []
    selected = {}
    for record in dataset["selected"]:
        if type(record) is dict and type(record.get("sample_id")) is str:
            selected.setdefault(record["sample_id"], record)
    paths = resolve_audio(dataset)
    seen = set()
    for pair in pair_list["pairs"]:
        pair_id = pair["pair_id"]
        if pair_id in seen:
            problems.append(preflight("duplicate_pair_id", pair_id))
            continue
        seen.add(pair_id)
        elements, unresolved = {}, False
        for name, role in (("kick_sample_id", "kick"), ("bass_sample_id", "bass")):
            record = selected.get(pair[name])
            if record is None or record.get("role") != role or pair[name] not in paths:
                unresolved = True
                break
            elements[name] = paths[pair[name]]
        if unresolved:
            problems.append(preflight("sample_unresolved", pair_id))
            continue
        rates = {}
        for name in ("kick_sample_id", "bass_sample_id"):
            try:
                rates[name] = _sample_rate(elements[name])
            except (OSError, ValueError):
                problems.append(preflight("audio_unreadable", pair_id))
                break
        else:
            if len(set(rates.values())) != 1:
                problems.append(preflight("sample_rate_mismatch", pair_id))
    return problems


def _sample_rate(path):
    if path.is_dir() or not path.is_file():
        raise OSError("not a regular file")
    attributes = getattr(path.stat(), "st_file_attributes", 0)
    if attributes & (0x1000 | 0x40000 | 0x400000):
        raise OSError("cloud placeholder")
    return audio.load_wav(path).sample_rate_hz


def report(problems):
    for problem in problems:
        print(f"{problem['code']}: {problem['context']}", file=sys.stderr)


def check_backend(arguments, monitoring):
    backend = arguments.playback_backend or "command"
    problem = playback.monitoring_problem(backend, monitoring)
    if problem is not None:
        raise RatingError("invalid_monitoring_description", problem)
    if backend == "command":
        problem = playback.player_command_problem(arguments.player_command)
        if problem is not None:
            raise RatingError("invalid_player_command", problem)
    return backend

# --- session records -------------------------------------------------------

def presentation_order(pairs, order_seed, session_id):
    """Order the sorted pair ids by a seeded digest, then repair shared kicks.

    Whenever two consecutive presentations share a kick, the pair at the later position trades
    places with the first later, then the first earlier, presentation whose kick differs, and the
    swap is kept only when it strictly reduces the number of shared-kick adjacencies. When no such
    swap exists the two stay adjacent. The rule is deterministic: same inputs, same order.
    """
    ordered = sorted(pairs, key=lambda pair: pair["pair_id"])
    seeds = {pair["pair_id"]: hashlib.sha256(
        (order_seed + "\n" + session_id + "\n" + pair["pair_id"]).encode("utf-8")).hexdigest()
        for pair in ordered}
    shuffled = sorted(ordered, key=lambda pair: (seeds[pair["pair_id"]], pair["pair_id"]))
    for index in range(1, len(shuffled)):
        if not shared_kick(shuffled[index - 1], shuffled[index]):
            continue
        for position in (index, index - 1):
            others = list(range(position + 1, len(shuffled))) + list(range(position - 1, -1, -1))
            for other in others:
                if _improves(shuffled, position, other):
                    shuffled[position], shuffled[other] = shuffled[other], shuffled[position]
                    break
            else:
                continue
            break
    return shuffled


def shared_kick(first, second):
    return first["kick_sample_id"] == second["kick_sample_id"]


def _adjacent_collisions(order, index):
    total = 0
    if index and shared_kick(order[index - 1], order[index]):
        total += 1
    if index + 1 < len(order) and shared_kick(order[index], order[index + 1]):
        total += 1
    return total


def _improves(order, first, second):
    """True when swapping two positions strictly reduces the shared-kick adjacency count."""
    before = _adjacent_collisions(order, first) + _adjacent_collisions(order, second)
    order[first], order[second] = order[second], order[first]
    after = _adjacent_collisions(order, first) + _adjacent_collisions(order, second)
    order[first], order[second] = order[second], order[first]
    return after < before


def presentation_documents(order):
    return [{"presentation_index": index, "pair_id": pair["pair_id"],
             "kick_sample_id": pair["kick_sample_id"], "bass_sample_id": pair["bass_sample_id"]}
            for index, pair in enumerate(order, start=1)]


def effective_answers(records):
    effective = {}
    for record in records:
        if type(record) is dict and type(record.get("presentation_index")) is int:
            effective[record["presentation_index"]] = record
    return effective


def next_unanswered(effective, total):
    for index in range(1, total + 1):
        if index not in effective:
            return index
    return None


def counts(document, records):
    presentations = document["presentations"]
    effective = effective_answers(records)
    return {
        "session_id": document["session_id"],
        "evaluator_id": document["evaluator_id"],
        "state": document["state"],
        "presentations": len(presentations),
        "answered": len(effective),
        "skipped": sum(record["skip"] for record in effective.values()),
        "recognised": sum(record["recognised"] for record in effective.values()),
        "failed_playback": sum(not record["playback_completed"] for record in effective.values()),
        "next_presentation_index": next_unanswered(effective, len(presentations)),
    }


def export_document(document, records):
    return {"schema_version": SCHEMA_VERSION,
            "session": {name: document[name] for name in SESSION_FIELDS},
            "ratings": records}

# --- validation ------------------------------------------------------------

def session_violations(document):
    if type(document) is not dict:
        return [violation("invalid_field_set", None, "the session record is not an object")]
    try:
        fields(document, SESSION_DOCUMENT_FIELDS)
    except ValueError:
        missing = sorted(set(SESSION_DOCUMENT_FIELDS.split()) - set(document))
        extra = sorted(set(document) - set(SESSION_DOCUMENT_FIELDS.split()))
        return [violation("invalid_field_set", None,
                          f"session record: missing {missing} unexpected {extra}")]
    problems = []
    checks = (
        ("schema_version", lambda value: value == SCHEMA_VERSION),
        ("session_id", lambda value: type(value) is str and SESSION_ID_PATTERN.fullmatch(value) is not None),
        ("evaluator_id", lambda value: type(value) is str and EVALUATOR_ID_PATTERN.fullmatch(value) is not None),
        ("protocol_version", lambda value: value == SUPPORTED_PROTOCOL_VERSION),
        ("dataset_version", lambda value: type(value) is str and bool(value.strip())),
        ("split_manifest_digest",
         lambda value: type(value) is str and DIGEST_PATTERN.fullmatch(value) is not None),
        ("order_seed", lambda value: value == ORDER_SEED),
        ("playback_gain_db",
         lambda value: type(value) is float and value == playback.PLAYBACK_GAIN_DB),
        ("monitoring_description",
         lambda value: type(value) is str and bool(value.strip())
         and len(value) <= playback.MONITORING_MAXIMUM),
        ("started_at", lambda value: type(value) is str and TIMESTAMP_PATTERN.fullmatch(value) is not None),
        ("finished_at", lambda value: value is None or (type(value) is str
                                                        and TIMESTAMP_PATTERN.fullmatch(value) is not None)),
        ("state", lambda value: value in STATES),
    )
    for name, check in checks:
        if not check(document[name]):
            problems.append(violation("invalid_value", None, "session record field " + name))
    presentations = document["presentations"]
    if type(presentations) is not list or not presentations:
        problems.append(violation("invalid_value", None, "session record field presentations"))
        return problems
    pair_ids = set()
    for position, presentation in enumerate(presentations, start=1):
        if type(presentation) is not dict or set(presentation) != set(PRESENTATION_FIELDS.split()):
            problems.append(violation("invalid_field_set", position,
                                      "presentation: unexpected or missing fields"))
            continue
        if presentation["presentation_index"] != position:
            problems.append(violation("invalid_value", position,
                                      "presentation_index is not its 1-based position"))
        for name in ("pair_id", "kick_sample_id", "bass_sample_id"):
            if type(presentation[name]) is not str or not presentation[name].strip():
                problems.append(violation("invalid_value", position, "presentation field " + name))
        if type(presentation["pair_id"]) is str and not PAIR_ID_PATTERN.fullmatch(presentation["pair_id"]):
            problems.append(violation("invalid_value", position, "presentation field pair_id"))
        if presentation["pair_id"] in pair_ids:
            problems.append(violation("invalid_value", position, "presentation pair_id is not unique"))
        pair_ids.add(presentation["pair_id"])
    return problems


def record_violations(document, records):
    problems = []
    presentations = document.get("presentations") if type(document) is dict else None
    total = len(presentations) if type(presentations) is list else 0
    seen, non_corrections = set(), set()
    for number, record in enumerate(records, start=1):
        if type(record) is not dict:
            problems.append(violation("invalid_field_set", None, f"journal line {number} is not an object"))
            continue
        try:
            fields(record, " ".join(RATING_FIELDS))
        except ValueError:
            missing = sorted(set(RATING_FIELDS) - set(record))
            extra = sorted(set(record) - set(RATING_FIELDS))
            problems.append(violation("invalid_field_set", None,
                                      f"rating record {number}: missing {missing} unexpected {extra}"))
            continue
        index = record["presentation_index"]
        if type(index) is not int or isinstance(index, bool) or not 1 <= index <= total:
            problems.append(violation("invalid_value", None, f"rating record {number} presentation_index"))
            continue
        checks = (
            ("pair_id", lambda value: type(value) is str and PAIR_ID_PATTERN.fullmatch(value) is not None),
            ("kick_sample_id", lambda value: type(value) is str and bool(value.strip())),
            ("bass_sample_id", lambda value: type(value) is str and bool(value.strip())),
            ("rating", lambda value: value is None or value in LABELS),
            ("skip", lambda value: type(value) is bool),
            ("skip_reason", lambda value: value is None or value in SKIP_REASONS),
            ("recognised", lambda value: type(value) is bool),
            ("playback_completed", lambda value: type(value) is bool),
            ("responded_at",
             lambda value: type(value) is str and TIMESTAMP_PATTERN.fullmatch(value) is not None),
            ("correction_of", lambda value: value is None
             or (type(value) is int and not isinstance(value, bool))),
        )
        for name, check in checks:
            if not check(record[name]):
                problems.append(violation("invalid_value", index, "rating record field " + name))
        if record["rating"] is not None and (record["skip"] is not False
                                             or record["playback_completed"] is not True):
            problems.append(violation("rating_invariant", index,
                                      "a rating needs skip false and a completed playback"))
        if record["skip"] is True:
            if record["rating"] is not None or record["skip_reason"] is None:
                problems.append(violation("rating_invariant", index,
                                          "a skip needs a null rating and a reason"))
        elif record["skip"] is False and record["skip_reason"] is not None:
            problems.append(violation("rating_invariant", index, "a non-skip needs a null skip_reason"))
        if record["skip_reason"] == "recognised" and record["recognised"] is not True:
            problems.append(violation("rating_invariant", index,
                                      "a recognised skip needs recognised true"))
        if type(presentations) is list and 1 <= index <= total \
                and type(presentations[index - 1]) is dict:
            presentation = presentations[index - 1]
            for name in ("pair_id", "kick_sample_id", "bass_sample_id"):
                if record[name] != presentation.get(name):
                    problems.append(violation("presentation_mismatch", index,
                                              "rating record field " + name
                                              + " disagrees with its presentation"))
        if record["correction_of"] is not None:
            if record["correction_of"] != index:
                problems.append(violation("invalid_correction", index,
                                          "correction_of must repeat presentation_index"))
            if index not in seen:
                problems.append(violation("unknown_correction_target", index,
                                          "correction_of has no earlier record at that index"))
        elif index in non_corrections:
            problems.append(violation("duplicate_presentation", index,
                                      "one index has more than one record that is not a correction"))
        non_corrections.add(index)
        seen.add(index)
    if type(document) is dict and document.get("finished_at") is not None:
        effective = effective_answers(records)
        if next_unanswered(effective, total) is not None:
            problems.append(violation("session_invariant", None,
                                      "finished_at is set while a presentation has no effective answer"))
        if document.get("state") != "complete":
            problems.append(violation("session_invariant", None,
                                      "finished_at is set while the session is not complete"))
    return problems


def export_violations(directory, document, records):
    path = Path(directory) / EXPORT_FILE
    if not path.is_file():
        return [violation("export_missing", None, "the session has no export.json; run export first")]
    try:
        exported = read_json(path)
    except (OSError, ValueError):
        return [violation("invalid_value", None, "export.json is not readable JSON")]
    if type(exported) is not dict or set(exported) != set(EXPORT_FIELDS.split()):
        return [violation("invalid_field_set", None, "export.json: unexpected or missing fields")]
    problems = []
    if exported["schema_version"] != SCHEMA_VERSION:
        problems.append(violation("invalid_value", None, "export.json schema_version"))
    if not session_violations(document):
        expected = {name: document[name] for name in SESSION_FIELDS}
        if exported["session"] != expected:
            problems.append(violation("export_mismatch", None,
                                      "export.json session block disagrees with the session record"))
    if exported["ratings"] != records:
        problems.append(violation("export_mismatch", None,
                                  "export.json ratings disagree with the journal order"))
    return problems

# --- the interactive surface ----------------------------------------------

def parse_command(line):
    words = line.strip().lower().split()
    if not words:
        return "unknown_command", ""
    first = words[0]
    if len(words) == 1 and first in ("r", "k", "q", "?"):
        return first, ""
    if len(words) == 1 and (first in LABEL_INPUTS or first in LABELS):
        return "label", LABEL_INPUTS.get(first, first)
    if first == "s":
        return "skip", words[1] if len(words) == 2 else ""
    if first == "c":
        return "correct", words[1] if len(words) == 2 else ""
    if len(words) == 1:
        return "label", first
    return "unknown_command", ""


def build_record(index, pair, rating, skip, skip_reason, recognised, played, correcting):
    return {"pair_id": pair["pair_id"], "kick_sample_id": pair["kick_sample_id"],
            "bass_sample_id": pair["bass_sample_id"], "presentation_index": index,
            "rating": rating, "skip": skip, "skip_reason": skip_reason,
            "recognised": recognised, "playback_completed": played,
            "responded_at": timestamp(), "correction_of": index if correcting else None}


def progress(records, total):
    return f"answered {len(effective_answers(records))} of {total}"


def attempt(context, index, pair):
    """Render and play one presentation; any failure means the playback did not complete."""
    destination = playback.render_path(context.directory, index)
    paths = resolve_audio(context.dataset)
    try:
        playback.render_pair(paths[pair["kick_sample_id"]], paths[pair["bass_sample_id"]], destination)
    except (playback.PlaybackError, OSError, ValueError, KeyError):
        return False
    return playback.play(context.backend, destination, context.player_command)


def discard_render(context, index):
    playback.render_path(context.directory, index).unlink(missing_ok=True)


def store_answer(context, index, stdout):
    write_journal(context.directory / JOURNAL_FILE, context.records)
    discard_render(context, index)
    print(progress(context.records, len(context.document["presentations"])), file=stdout, flush=True)


def publish_completion(context, stdout, already_complete=False):
    if not already_complete:
        context.document["state"] = "complete"
        context.document["finished_at"] = timestamp()
        _atomic_text(context.directory / SESSION_FILE, canonical(context.document) + "\n")
    summary = counts(context.document, context.records)
    total = summary["presentations"]
    rate = summary["recognised"] / total if total else 0.0
    print(canonical({**summary, "recognised_rate": rate,
                     "recognised_rate_exceeded": rate > context.limit}), file=stdout, flush=True)
    return 1 if summary["failed_playback"] else 0


def run_prompt(context, stream, stdout, index, pair, completed, flagged, correcting, effective):
    """Return ("advance", None), ("move", target) or ("pause", None); writes an accepted answer."""
    while True:
        line = stream.readline()
        if line == "":
            print("paused", file=stdout, flush=True)
            return "pause", None
        command, argument = parse_command(line)
        if command == "q":
            print("paused", file=stdout, flush=True)
            return "pause", None
        if command == "?":
            print(context.instructions, file=stdout, flush=True)
            continue
        if command == "k":
            flagged = not flagged
            continue
        if command == "r":
            completed = attempt(context, index, pair)
            if not completed:
                print("playback_incomplete", file=stdout, flush=True)
            continue
        if command == "label":
            if argument not in LABELS:
                print("invalid_label", file=stdout, flush=True)
                continue
            if not completed:
                print("playback_incomplete", file=stdout, flush=True)
                continue
            context.records.append(build_record(index, pair, argument, False, None,
                                                flagged, True, correcting))
            store_answer(context, index, stdout)
            return "advance", None
        if command == "skip":
            if argument not in SKIP_REASONS:
                print("invalid_skip_reason", file=stdout, flush=True)
                continue
            if argument == "failed-playback" and completed:
                print("skip_reason_not_applicable", file=stdout, flush=True)
                continue
            context.records.append(build_record(index, pair, None, True, argument,
                                                flagged or argument == "recognised", completed,
                                                correcting))
            store_answer(context, index, stdout)
            return "advance", None
        if command == "correct":
            target = int(argument) if argument.isdigit() else None
            if target is None or not 1 <= target <= len(context.document["presentations"]) \
                    or target not in effective:
                print("unknown_presentation", file=stdout, flush=True)
                continue
            return "move", target
        print("unknown_command", file=stdout, flush=True)


def run_interactive(context, stream, stdout):
    document = context.document
    total = len(document["presentations"])
    effective = effective_answers(context.records)
    if len(effective) == total:
        return publish_completion(context, stdout,
                                  already_complete=document["state"] == "complete")
    if not effective:
        print(context.instructions, file=stdout, flush=True)
    index, correcting, flagged = next_unanswered(effective, total), False, False
    while True:
        pair = document["presentations"][index - 1]
        completed = attempt(context, index, pair)
        if not completed:
            context.records.append(build_record(index, pair, None, True, "failed-playback",
                                                flagged, False, correcting))
            store_answer(context, index, stdout)
        else:
            decision, target = run_prompt(context, stream, stdout, index, pair, completed,
                                          flagged, correcting, effective)
            if decision == "pause":
                return 3
            if decision == "move":
                index, correcting, flagged = target, True, False
                continue
        effective = effective_answers(context.records)
        if len(effective) == total:
            return publish_completion(context, stdout)
        index, correcting, flagged = next_unanswered(effective, total), False, False

# --- session lifecycle -----------------------------------------------------

@contextmanager
def session_lock(directory):
    lock = Path(directory) / LOCK_FILE
    try:
        descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as error:
        raise RatingError("session_locked",
                          "Another writer holds this session; confirm no writer is active before "
                          "removing a stale lock.") from error
    try:
        with os.fdopen(descriptor, "w", encoding="ascii") as stream:
            stream.write(str(os.getpid()) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        yield
    finally:
        lock.unlink(missing_ok=True)


def session_directories(root, session_id=None):
    sessions = Path(root) / SESSIONS_DIRECTORY
    if not sessions.is_dir():
        return []
    return [entry for entry in sorted(sessions.iterdir())
            if entry.is_dir() and entry.name != session_id]


def answered_elsewhere(root, session_id, evaluator_id):
    """pair_id -> earlier session_id for every pair this evaluator already answered elsewhere."""
    answered = {}
    for directory in session_directories(root, session_id):
        try:
            document = read_json(directory / SESSION_FILE)
        except (OSError, ValueError):
            continue
        if type(document) is not dict or document.get("evaluator_id") != evaluator_id:
            continue
        try:
            records, problems = read_journal(directory / JOURNAL_FILE)
        except RatingError:
            continue
        if problems:
            continue
        for record in records:
            if type(record) is dict and type(record.get("pair_id")) is str:
                answered.setdefault(record["pair_id"], directory.name)
    return answered


def check_pairs_unrated(root, session_id, evaluator_id, pair_ids):
    answered = answered_elsewhere(root, session_id, evaluator_id)
    for pair_id in sorted(pair_ids):
        if pair_id in answered:
            raise RatingError("evaluator_pair_already_rated",
                              f"The pair {pair_id} was already answered in session {answered[pair_id]}.")


def load_session(directory):
    if not (Path(directory) / SESSION_FILE).is_file():
        raise RatingError("session_unreadable", "The session directory does not hold a session file.")
    document = read_document(Path(directory) / SESSION_FILE, "session_unreadable")
    problems = session_violations(document)
    if problems:
        raise RatingError(problems[0]["code"], "The stored session record is invalid; run validate.")
    records, problems = read_journal(Path(directory) / JOURNAL_FILE)
    if problems:
        raise RatingError("journal_invalid", "The stored journal is invalid; run validate.")
    problems = record_violations(document, records)
    if problems:
        raise RatingError(problems[0]["code"], "The stored rating records are invalid; run validate.")
    return document, records


def start(arguments, stream, stdout):
    session_id = arguments.session_id if arguments.session_id is not None else uuid.uuid4().hex
    if not isinstance(session_id, str) or not SESSION_ID_PATTERN.fullmatch(session_id):
        raise RatingError("invalid_session_id", "The session id must match ^[a-z0-9][a-z0-9_-]{0,63}$.")
    if not isinstance(arguments.evaluator_id, str) \
            or not EVALUATOR_ID_PATTERN.fullmatch(arguments.evaluator_id):
        raise RatingError("invalid_evaluator_id", "The evaluator id must match ^[a-z0-9][a-z0-9-]{0,31}$.")
    backend = check_backend(arguments, arguments.monitoring)
    protocol = load_protocol(document_path(arguments))
    dataset = load_dataset(arguments.dataset)
    if dataset is None:
        return 2
    pair_list = load_pair_list(arguments.pairs)
    if pair_list is None:
        return 2
    if pair_list["dataset_version"] != dataset["dataset_version"]:
        report([preflight("dataset_version_mismatch", "pair_list")])
        return 2
    problems = check_assignments(dataset, pair_list)
    if problems:
        report(problems)
        return 2
    root = private_root()
    directory = root / SESSIONS_DIRECTORY / session_id
    if (directory / SESSION_FILE).exists():
        raise RatingError("session_exists", "A session with this id already exists under the private root.")
    check_pairs_unrated(root, session_id, arguments.evaluator_id,
                        {pair["pair_id"] for pair in pair_list["pairs"]})
    order = presentation_order(pair_list["pairs"], protocol.order_seed, session_id)
    document = {"schema_version": SCHEMA_VERSION, "session_id": session_id,
                "evaluator_id": arguments.evaluator_id, "protocol_version": protocol.version,
                "dataset_version": dataset["dataset_version"],
                "split_manifest_digest": pair_list["split_manifest_digest"],
                "order_seed": protocol.order_seed, "playback_gain_db": playback.PLAYBACK_GAIN_DB,
                "monitoring_description": arguments.monitoring, "started_at": timestamp(),
                "finished_at": None, "state": "in_progress",
                "presentations": presentation_documents(order)}
    try:
        (directory / PLAYBACK_DIRECTORY).mkdir(parents=True)
    except OSError as error:
        raise RatingError("storage_failure", "The session directory could not be created.") from error
    with session_lock(directory):
        _atomic_text(directory / INSTRUCTIONS_FILE, protocol.instructions + "\n")
        _atomic_text(directory / SESSION_FILE, canonical(document) + "\n")
        write_journal(directory / JOURNAL_FILE, [])
        context = Context(directory, document, dataset, [], backend, arguments.player_command,
                          protocol.instructions, protocol.max_recognised_rate)
        return run_interactive(context, stream, stdout)


def resume(arguments, stream, stdout):
    root = private_root()
    directory = private_path(arguments.session, root)
    document, records = load_session(directory)
    protocol = load_protocol(document_path(arguments))
    if document["protocol_version"] != protocol.version:
        raise RatingError("protocol_version_mismatch",
                          "The session was recorded under another protocol version.")
    backend = check_backend(arguments, document["monitoring_description"])
    if document["monitoring_description"].startswith(playback.FAKE_MONITORING_PREFIX) \
            and backend != "fake":
        raise RatingError("invalid_monitoring_description",
                          "A dry-run session can only be resumed with the fake playback backend.")
    dataset = load_dataset(arguments.dataset)
    if dataset is None:
        return 2
    if dataset["dataset_version"] != document["dataset_version"]:
        raise RatingError("dataset_version_mismatch",
                          "The dataset does not match the recorded dataset version.")
    check_pairs_unrated(root, document["session_id"], document["evaluator_id"],
                        {presentation["pair_id"] for presentation in document["presentations"]})
    with session_lock(directory):
        context = Context(directory, document, dataset, records, backend, arguments.player_command,
                          protocol.instructions, protocol.max_recognised_rate)
        return run_interactive(context, stream, stdout)


def status(arguments, stdout):
    root = private_root()
    document, records = load_session(private_path(arguments.session, root))
    print(canonical(counts(document, records)), file=stdout, flush=True)
    return 0


def export(arguments, stdout):
    root = private_root()
    directory = private_path(arguments.session, root)
    document, records = load_session(directory)
    destination = private_path(arguments.output, root) if arguments.output else directory / EXPORT_FILE
    _atomic_text(destination, canonical(export_document(document, records)) + "\n")
    print(canonical({"schema_version": SCHEMA_VERSION, "session_id": document["session_id"],
                     "ratings": len(records)}), file=stdout, flush=True)
    return 0


def validate(arguments, stdout):
    root = private_root()
    directory = private_path(arguments.session, root)
    if not (directory / SESSION_FILE).is_file():
        raise RatingError("session_unreadable", "The session directory does not hold a session file.")
    document = read_document(directory / SESSION_FILE, "session_unreadable")
    records, problems = read_journal(directory / JOURNAL_FILE)
    session_problems = session_violations(document)
    problems = session_problems + problems
    if not session_problems:
        problems += record_violations(document, records)
        problems += export_violations(directory, document, records)
    print(canonical({"valid": not problems, "violations": problems}), file=stdout, flush=True)
    return 0 if not problems else 1

# --- command line ----------------------------------------------------------

def build_parser():
    parser = argparse.ArgumentParser(prog="python -m backend.evaluation.rating", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    start_parser = commands.add_parser("start", help="start one assigned pair-rating session")
    start_parser.add_argument("--dataset", required=True)
    start_parser.add_argument("--pairs", required=True)
    start_parser.add_argument("--evaluator-id", required=True)
    start_parser.add_argument("--monitoring", required=True)
    start_parser.add_argument("--session-id")
    add_playback_arguments(start_parser)
    resume_parser = commands.add_parser("resume", help="continue the first unanswered presentation")
    resume_parser.add_argument("--session", required=True)
    resume_parser.add_argument("--dataset", required=True)
    add_playback_arguments(resume_parser)
    status_parser = commands.add_parser("status", help="print counts only")
    status_parser.add_argument("--session", required=True)
    export_parser = commands.add_parser("export", help="write the private export document")
    export_parser.add_argument("--session", required=True)
    export_parser.add_argument("--output")
    validate_parser = commands.add_parser("validate", help="check one session record set")
    validate_parser.add_argument("--session", required=True)
    return parser


def add_playback_arguments(parser):
    parser.add_argument("--playback-backend", choices=playback.BACKENDS)
    parser.add_argument("--player-command")
    parser.add_argument("--protocol-doc")


def main(argv=None, stdin=None):
    arguments = build_parser().parse_args(argv)
    stream = sys.stdin if stdin is None else stdin
    try:
        if arguments.command == "start":
            return start(arguments, stream, sys.stdout)
        if arguments.command == "resume":
            return resume(arguments, stream, sys.stdout)
        if arguments.command == "status":
            return status(arguments, sys.stdout)
        if arguments.command == "export":
            return export(arguments, sys.stdout)
        return validate(arguments, sys.stdout)
    except RatingError as error:
        print(f"{error.code}: {error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
    except (OSError, ValueError, KeyError, TypeError):
        print("refused: local failure; nothing was accepted.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

