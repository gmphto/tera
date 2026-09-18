# Issue grooming — 2026-09-18

Project code: TERA.

All 60 existing GitHub issues were reviewed. All 59 open issues are groomed: 58 issue bodies were rewritten and published; #4 already met the PM template and retains its detailed WAV-reader requirements. Closed setup issue #1 remains unchanged.

The rewritten issues preserve their original goals and use the four sections required by `_docs/task-template.md`: Goal, Acceptance criteria, Out of scope, and Constraints. Each specifies observable acceptance criteria, edge cases, linked exclusions, implementation prerequisites, module boundaries, dependency approval, and local-audio constraints.

Scope gaps assigned to existing issues:

- #32 owns the palette API operations required by its controls.
- #43 owns multi-role palette/API/UI support as well as full-palette aggregation; #41 and #42 depend on it.
- #27 exposes job cancellation/retry needed by #31.
- #54 sets measurable bridge acceptance limits before #57 validates them.

Product gates:

- #16 defines evaluation thresholds before results are examined.
- #20 records the feasibility decision before Phase 1 product work.
- #39 records the standalone milestone decision before later phases.
- Missing live integrations, human ratings, or host access cannot be counted as successful evidence.

Verification completed:

- All 58 saved issue bodies exactly match the prepared rewrites.
- Issue #4 is unchanged; all 59 open issues have the four required sections and acceptance checklists.
- Titles, states, assignees, labels, and milestones are unchanged.
- Implementation prerequisites form an acyclic graph.
- No implementation code or dependencies were added. Code tests were unnecessary for this issue/documentation-only task.

Groomed does not mean ready or implemented. The first tasks without open prerequisites are [#2: contracts](https://github.com/gmphto/tera/issues/2) and [#3: DSP feasibility](https://github.com/gmphto/tera/issues/3). Follow the existing one-task-at-a-time workflow; adding dependencies still requires user approval.

[Open backlog](https://github.com/gmphto/tera/issues)

The user confirmed the project code as TERA. Requested completion title: `TERA - groomed all open issues - 2026-09-18` (Europe/London). No chat-title tool is exposed in this session, so the title could not be changed.
