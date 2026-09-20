"""The focused selector and the fail-closed whole-suite guard.

Every tree here is synthetic: the mapping is exercised on a temporary
repository shape, so no real test file has to change when the tool changes.
"""

import subprocess
import textwrap
from pathlib import Path

from tools import focused


def _tree(root: Path, files: dict[str, str]) -> None:
    for relative, text in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(text), encoding="utf-8")


def _names(paths, root: Path) -> list[str]:
    return [path.relative_to(root).as_posix() for path in paths]


def test_a_package_import_selects_the_importing_test(tmp_path):
    _tree(tmp_path, {
        "backend/library/scanner.py": "SCANNER = 1\n",
        "tests/test_library_scanner.py": "from backend.library import scanner\n",
        "tests/test_other.py": "from backend.library import queue\n",
    })
    assert _names(focused.affected_tests(["backend/library/scanner.py"], tmp_path), tmp_path) == \
        ["tests/test_library_scanner.py"]


def test_the_file_name_convention_matches_a_test_that_imports_nothing(tmp_path):
    _tree(tmp_path, {
        "backend/library/queue.py": "QUEUE = 1\n",
        "tests/test_library_queue.py": "import json\n",
    })
    assert _names(focused.affected_tests(["backend/library/queue.py"], tmp_path), tmp_path) == \
        ["tests/test_library_queue.py"]


def test_a_member_import_selects_the_module_it_comes_from(tmp_path):
    _tree(tmp_path, {
        "backend/analysis/batch.py": "ROLES = ()\n",
        "tests/test_batch.py": "from backend.analysis.batch import ROLES\n",
    })
    assert _names(focused.affected_tests(["backend/analysis/batch.py"], tmp_path), tmp_path) == \
        ["tests/test_batch.py"]


def test_an_unrelated_change_selects_nothing(tmp_path):
    _tree(tmp_path, {
        "backend/palette/ranking.py": "RANKING = 1\n",
        "tests/test_library_queue.py": "from backend.library import queue\n",
    })
    assert focused.affected_tests(["backend/palette/ranking.py"], tmp_path) == []


def test_a_helper_is_reached_through_the_module_that_imports_it(tmp_path):
    _tree(tmp_path, {
        "backend/library/scanner.py": "from backend.library import reconcile\n",
        "backend/library/reconcile.py": "RECONCILE = 1\n",
        "tests/test_library_scanner.py": "from backend.library import scanner\n",
    })
    assert _names(focused.affected_tests(["backend/library/reconcile.py"], tmp_path), tmp_path) == \
        ["tests/test_library_scanner.py"]


def test_a_documentation_change_selects_nothing(tmp_path):
    _tree(tmp_path, {
        "backend/library/queue.py": "QUEUE = 1\n",
        "tests/test_library_queue.py": "import json\n",
    })
    assert focused.affected_tests(["_docs/library-jobs.md"], tmp_path) == []


def test_a_new_file_inside_an_untracked_directory_is_a_change(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    _tree(tmp_path, {"tools/focused.py": "TOOL = 1\n"})
    assert focused.changed_files(tmp_path) == ["tools/focused.py"]


def test_a_bare_whole_suite_run_is_refused():
    assert focused.refusal_reason(()) is not None
    assert focused.refusal_reason(("-q",)) is not None
    assert focused.refusal_reason(("tests",)) is not None
    assert focused.refusal_reason(("tests/",)) is not None
    assert focused.refusal_reason((".",)) is not None


def test_a_focused_or_explicit_run_is_allowed():
    assert focused.refusal_reason(("tests/test_api_contracts.py",)) is None
    assert focused.refusal_reason(("-k", "outcome")) is None
    assert focused.refusal_reason(("-m", "slow")) is None
    assert focused.refusal_reason((), keyword="outcome") is None
    assert focused.refusal_reason((), markexpr="slow") is None
    assert focused.refusal_reason((), collectonly=True) is None
    assert focused.refusal_reason(("--all",)) is None
