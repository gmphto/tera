"""Focused test selection: run the tests a change actually affects.

Verification in this repository is focused by default; the whole suite is an
explicit, deliberate act (`AGENTS.md`, "Commands"):

    uv run python -m tools.focused                    # derive the change from git
    uv run python -m tools.focused --issue 29         # files from commits naming #29
    uv run python -m tools.focused tests/test_api_contracts.py
    uv run python -m tools.focused backend/library/queue.py
    uv run python -m tools.focused --dry-run          # print the selection only

Selection is import-based, so it survives renames. A test module is selected
when it imports the changed module (directly or through its package) or when
its file name follows the changed module (`backend/library/scanner.py` ->
`tests/test_library_scanner.py`). A changed module that no test covers directly
brings in the tests of the modules that import it, so a helper is reached
through its user. A change to a hub module (`schema.py`, `errors.py`,
`service.py`) legitimately selects every test that imports it; the printed
selection is what makes that visible. The whole suite stays available as
`uv run pytest --all`.
"""

from __future__ import annotations

import argparse
import ast
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOTS = ("backend", "tools")
TESTS_DIR = "tests"

REFUSAL = (
    "Refusing to run the whole suite: verification here is focused by default.\n"
    "  uv run python -m tools.focused             tests affected by the current change\n"
    "  uv run python -m tools.focused --issue 29  tests affected by an issue's commits\n"
    "  uv run pytest <path> [...]                 an explicit focused run\n"
    "Pass --all for the whole suite at an integration or release checkpoint."
)


def normalize(value) -> str:
    """One path spelling: posix separators and no trailing slash."""
    return str(value).replace("\\", "/").rstrip("/")


def module_name(relative_path) -> str | None:
    """`backend/library/scanner.py` -> `backend.library.scanner`; None if not source."""
    normalized = normalize(relative_path)
    if not normalized.endswith(".py"):
        return None
    parts = normalized[:-3].split("/")
    if parts[0] not in SOURCE_ROOTS:
        return None
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts) or None


def imported_names(path: Path) -> set[str]:
    """Every dotted name a module imports, including package-qualified members."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, UnicodeDecodeError):
        return set()
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
            names.update(f"{node.module}.{alias.name}"
                         for alias in node.names if alias.name != "*")
    return names


def source_files(root: Path) -> list[Path]:
    """Every module under the source roots, ordered for stable output."""
    files: list[Path] = []
    for source_root in SOURCE_ROOTS:
        files.extend(sorted((root / source_root).rglob("*.py")))
    return files


def importers(root: Path) -> dict[str, set[str]]:
    """module -> the modules that import it, across the source roots."""
    edges: dict[str, set[str]] = {}
    for path in source_files(root):
        importer = module_name(path.relative_to(root))
        if importer is None:
            continue
        for imported in imported_names(path):
            edges.setdefault(imported, set()).add(importer)
    return edges


def name_candidates(module: str) -> set[str]:
    """Conventional test file names for a module: `test_<stem>.py`, `test_<parent>_<stem>.py`."""
    parts = module.split(".")
    names = {f"test_{parts[-1]}.py"}
    if len(parts) > 1:
        names.add(f"test_{parts[-2]}_{parts[-1]}.py")
    return names


def affected_tests(sources, root: Path = REPO_ROOT) -> list[Path]:
    """The test files a change to `sources` affects, in a stable order."""
    modules = sorted({name for name in (module_name(source) for source in sources) if name})
    if not modules:
        return []
    tests = sorted((root / TESTS_DIR).glob("test_*.py"))
    imports = {test: imported_names(test) for test in tests}
    selected: set[Path] = set()
    uncovered: list[str] = []
    for module in modules:
        hits = {
            test for test in tests
            if test.name in name_candidates(module)
            or any(name == module or name.startswith(module + ".") for name in imports[test])
        }
        if hits:
            selected |= hits
        else:
            uncovered.append(module)
    if uncovered:
        edges = importers(root)
        for module in uncovered:
            for dependent in edges.get(module, ()):
                selected |= {test for test in tests if test.name in name_candidates(dependent)}
    return sorted(selected)


def _git(root: Path, *arguments: str) -> list[str]:
    result = subprocess.run(["git", *arguments], cwd=root, capture_output=True, text=True)
    if result.returncode != 0:
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def changed_files(root: Path = REPO_ROOT, issue: int | None = None) -> list[str]:
    """Repository-relative paths: an issue's commits, or the current working tree."""
    if issue is not None:
        return _git(root, "log", "--name-only", "--pretty=format:", f"--grep=#{issue}\\b")
    paths = set(_git(root, "diff", "HEAD", "--name-only"))
    for line in _git(root, "status", "--porcelain", "-uall"):
        path = line[3:].strip()
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        if path:
            paths.add(path.strip('"'))
    return sorted(paths)


def refusal_reason(argv, *, keyword="", markexpr="", collectonly=False,
                   testpaths=(TESTS_DIR,)) -> str | None:
    """Why this pytest invocation must not run the whole suite, or None when it is focused."""
    arguments = [str(argument) for argument in argv]
    if "--all" in arguments or collectonly or keyword or markexpr:
        return None
    whole_suite = {TESTS_DIR, "."} | {normalize(path) for path in testpaths}
    positional = [argument for argument in arguments if not argument.startswith("-")]
    if not positional or any(normalize(argument) in whole_suite for argument in positional):
        return REFUSAL
    return None


def _partition(paths: list[str]) -> tuple[list[str], list[str]]:
    """Split given paths into test files and source files."""
    given = [normalize(path) for path in paths]
    direct = [path for path in given if path.split("/")[0] == TESTS_DIR]
    sources = [path for path in given if path.split("/")[0] != TESTS_DIR]
    return direct, sources


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="tools.focused", description="Run the tests the current change affects.")
    parser.add_argument("paths", nargs="*",
                        help="changed source or test paths; default is the git working tree")
    parser.add_argument("--issue", type=int,
                        help="take the changed files from the commits naming this issue")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the selection without running it")
    args = parser.parse_args(argv)

    root = REPO_ROOT
    if args.paths:
        direct, sources = _partition(args.paths)
    else:
        direct = []
        sources = changed_files(root, args.issue)

    selected = sorted({*(root / path for path in direct), *affected_tests(sources, root)})
    if not selected:
        print(f"No affected tests: nothing under {'/, '.join(SOURCE_ROOTS)}/ changed"
              + (f" in the commits naming #{args.issue}" if args.issue else "") + ".")
        return 0

    print(f"Focused run over {len(selected)} test file(s):")
    for path in selected:
        print(f"  {path.relative_to(root).as_posix()}")
    if args.dry_run:
        return 0
    command = [sys.executable, "-m", "pytest", "-q",
               *[path.relative_to(root).as_posix() for path in selected]]
    return subprocess.run(command, cwd=root).returncode


if __name__ == "__main__":
    raise SystemExit(main())
