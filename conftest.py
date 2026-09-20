"""Repository-level pytest policy.

Verification here is focused: run the tests a change affects with
`uv run python -m tools.focused`, and reserve the whole suite for integration
and release checkpoints behind an explicit `--all`. A bare `uv run pytest` is
refused so the suite is never the accidental default. See `AGENTS.md`.
"""

import pytest


def pytest_addoption(parser):
    parser.addoption("--all", action="store_true", default=False,
                     help="run the entire suite (integration/release checkpoint)")


def pytest_configure(config):
    if config.getoption("--all"):
        return
    from tools.focused import refusal_reason

    reason = refusal_reason(
        [str(argument) for argument in config.invocation_params.args],
        keyword=config.option.keyword or "",
        markexpr=config.option.markexpr or "",
        collectonly=bool(config.option.collectonly),
        testpaths=tuple(config.getini("testpaths")),
    )
    if reason:
        raise pytest.UsageError(reason)
