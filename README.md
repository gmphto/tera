# TERA

Local-first instrument palette intelligence. This skeleton contains an empty
Python backend and one package-import smoke test. See [_docs/plan.md](_docs/plan.md)
for the product plan and [issue #1](https://github.com/gmphto/tera/issues/1) for this task.

## Setup and tests

Install Python 3.12 or later and uv. From a clean checkout, run these commands
in the repository root (the directory containing `pyproject.toml`):

```sh
uv sync --locked
uv run pytest
```

`uv sync` creates `.venv` and installs the development dependencies, including
pytest. No runtime dependencies are required. The project runs directly from
the checkout; pytest adds the repository root to its import path.

To run just the smoke test:

```sh
uv run pytest tests/test_smoke.py
```
