# Commands

The project contains an empty Python backend and an import smoke test. Run these commands from the repository root, which contains `pyproject.toml`.

- `uv sync` - install dependencies.
- `uv run pytest` - run the whole Python test suite.
- `uv run pytest tests/test_smoke.py` - run the initial smoke test file; substitute another test path as needed.

# Rules

- Python dependencies are declared in `pyproject.toml`. Ask the user before adding any dependency, including frontend or desktop dependencies.
- Follow the product scope and architecture in `_docs/plan.md`. Track implementation work in the GitHub issues at https://github.com/gmphto/tera/issues.
- Keep audio files and local library paths on the user's device. DSP owns measured audio facts, Jev provides typed compatibility judgments, and application code owns the final ranking.

- `_docs/process.md` - how work is organized
