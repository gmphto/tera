# TERA

## Conventions
- Validation library: none configured.
- API query, parameter, and request parsing lives at: `backend/api/` (shared validation in `backend/api/schemas.py`).
- Do not hand-roll parsers elsewhere.

## Before finishing
- Test: `uv run pytest <affected-test-paths>`; `uv run pytest` at integration/release checkpoints or when impact cannot be safely bounded.
- Lint: none configured.
- Typecheck: none configured.

# Commands

Run from the repository root containing `pyproject.toml`.

- `uv sync` — install dependencies.
- `uv run pytest <affected-test-paths>` — default verification.
- `uv run pytest` — full suite at integration/release checkpoints,
  or when a change's impact cannot be safely bounded.

# Product rules

- Ask before adding any dependency, including frontend or desktop dependencies.
- Follow the product scope and architecture in `_docs/plan.md`.
- Track implementation work in https://github.com/gmphto/tera/issues.
- Keep audio files and local library paths on the user's device.
- DSP owns measured audio facts, Jev provides typed compatibility judgments,
  and application code owns the final ranking.

# Workflow authority

The workflow and testing rules below take precedence over conflicting rules
in `_docs/process.md`, `_docs/team/`, and `_docs/task-template.md`.
Those documents are optional references, not mandatory stages.

# Implementation workflow

- One owner handles one issue at a time. The main session may implement,
  test, review, and close issues.
- Do not automatically launch PM, Engineer, and QA agents.
- Groom only unclear scope or conflicting requirements. Do not rewrite
  an already actionable issue.
- Preserve existing implementation and valid verification evidence.
  Do not restart work to adopt this process.
- Close an issue only when its acceptance criteria and required checks pass.
  Explicitly report checks that could not run.
- Commit coherent changes and provide one concise completion note.

# Context and scope

- Read the current issue and relevant source, tests, and architecture sections.
  Do not load the entire documentation tree or every dependency module.
- Load skills and role guides only when needed.
- For new tasks, aim for 3–7 observable acceptance criteria and under 800 words.
  These are planning targets, not permission to discard requirements.
- Split oversized tasks into useful slices with linked issues.
  Do not bulk-regroom the existing backlog.
- Reference canonical contracts instead of copying them into each issue.
- Keep handoffs brief: issue, revision, changed files, verification,
  unresolved risks, and next action. Do not copy transcripts or full test logs.

# Lean testing

- Test observable behavior and distinct failure modes, not every function.
- Test each rule at its owning layer. API tests verify API contracts;
  they do not repeat the ranking test matrix.
- Keep a small number of integration tests to verify layers connect.
- Use representative cases and meaningful boundaries. Avoid exhaustive
  combinations unless interactions exercise distinct behavior.
- Do not add tests merely to increase test count or coverage percentage.
- Extend existing tests where possible. Add regression tests when they
  provide meaningful coverage of a defect.
- Keep end-to-end tests focused on core workflows and critical recovery paths.
- Expensive memory, timing, concurrency, and large-library tests need
  an explicit requirement or a concrete risk.
- During implementation and fix rounds, run affected tests and relevant
  contract tests.
- Documentation-only changes need documentation checks, not Python tests.
- Run the full suite before release, when separately changed components
  are integrated, or after changes with broad or uncertain impact.
  Do not run it automatically for every issue or reviewer.
- Reuse applicable test evidence. Record the command, result, revision,
  working-tree changes, and relevant environment.
- After fixes, rerun affected checks. Repeat broader checks only when
  the changes invalidate the previous evidence.
- Consolidate existing tests incrementally when duplicate coverage is
  demonstrated. Preserve distinct failure coverage; never delete tests
  solely because the suite is large or slow.

# Review

- Owner review is sufficient for documentation and low-risk local changes.
- Obtain focused independent review for ranking/DSP correctness,
  persistence/migrations, concurrency, resource ownership, privacy/security,
  and external error contracts.
- Review the diff, relevant requirements, and existing evidence.
  Independently probe risky behavior and unsupported claims.
- Do not build a second comprehensive test suite during QA.
- Every acceptance criterion needs credible evidence, but not necessarily
  a separate automated test.
- Crashes, broken contracts, unmet criteria, and missing required evidence
  block closure regardless of severity labels.
- Optional improvements may become linked follow-ups.
- Minor documentation fixes do not require a separate engineer/QA cycle.
- After a fix, review that finding and affected contracts.
  Expand review only when new risk justifies it.

# Reporting and tool failures

- Report delivered behavior, verification, remaining limitations,
  and linked follow-ups concisely. Avoid duplicate reports.
- Do not repeat failed tool calls without a changed hypothesis.
  Report environmental blockers promptly; never claim blocked work succeeded.
- After two completed issues, compare available token usage, review time,
  full-suite runs, and fix rounds before adding more process.
