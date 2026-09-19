# Feasibility decision — Phase 1 authorization

Record for issue [#165](https://github.com/gmphto/tera/issues/165), decided 2026-09-19 at the user's direction. It supersedes the Phase 1 gate in `_docs/feasibility-decision-20.md` for the scope below. That file is unchanged: its blocker analysis remains the historical account of why the gate was closed and which evidence is still missing.

## Phase 1 authorization

Phase 1 authorized — DSP-only kick-to-bass

## Scope authorized

The standalone kick-to-bass workflow of `_docs/plan.md` section 4 and tasks 21–39 of `_docs/tasks.md`, built one issue at a time through `_docs/process.md`:

storage (#21), scanning (#22), jobs (#23), palettes (#24), normalized retrieval (#25), decision cache (#26), local service (#27), recommendation route (#28), outcome log (#29), desktop shell (#30), palette controls (#31), role selection (#32), recommendation cards (#33), audition (#34), explanations (#35), retention reporting (#36), latency measurement (#37) and packaging (#38), followed by the first-producer milestone validation (#39).

Nothing outside that scope is authorized by this record.

## Conditions

- **DSP-only.** No live TypeSafe Jev credentials exist (#68), so the product runs `mode: "dsp-only"`, `run.evidence.interface` stays 0, and every hybrid row remains `insufficient evidence`. No result may claim a hybrid or measured-model outcome.
- **Evidence gaps are carried forward, not waived.** #65 (pair sampler and split), #66 (producer panel), #67 (pool shortfall), #69 (rating budget), #76 (labeling session) and #77 (analysis manifests) stay open and remain the blockers to any quality or producer claim. This record authorizes building the workflow; it does not authorize claiming its quality.
- **Frozen inputs stay frozen.** `_docs/evaluation-protocol.md` at `tera-eval-protocol-v1`, its constants, the percentile rule and the verdict vocabulary are read-only; no threshold is lowered and no constant is redefined.
- **Public safety unchanged.** Audio and local library paths stay on the device; no real path, sample or pack name, fingerprint, credential, endpoint or audio byte enters the repository, the tests, the fixtures or the issue comments.
- **Re-evaluation.** Once the evidence gaps close — in particular after #76 and the #19 re-run — a later record under the new run key (#103) should replace this authorization.

## What this does not change

`_docs/feasibility-decision-20.md` keeps its decision, its scope line and its blocker rows, and the open issues it names remain the evidence the plan still wants. What changes is only the authorization line: the storage, scanner, service, API, client, latency and packaging work of #21–#38 may now be built, and those issue bodies are the operative specifications.
