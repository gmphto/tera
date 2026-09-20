# Workflow Review

Remediation (2026-09-20, issue #168): V1–V6 are addressed at specification
level by [the canonical process](process.md), version 1.0, and its root
authority link. That document supersedes the proposed tables below, including
grouped rows and effect-before-state ordering. It records phase entry before
effects and makes all transitions explicit. No runtime dispatcher, atomic
lock or transaction is claimed. V7 remains a minor follow-up. Original hashes
and findings below describe the pre-remediation snapshot.

Remediation verification: documentation catalog checks validate unique IDs,
state/event/guard/action references, reachability, terminal exits and mutually
exclusive resume guards. `git diff --check` checks whitespace. Owner review
covers approval gates, impact-based checks, required independent review,
duplicate/uncertain effects and invalidated completion evidence. No product
code or dependencies changed; Python tests and runtime-conformance claims
are outside this documentation-only change.

## Sources

| ID | Path | Version | Scope | Precedence |
|---|---|---|---|---|
| G | `C:/Users/giftm.YUJI/.codex/AGENTS.md` | SHA-256 `45B3F294FD7666AAAEFEAB23C8DB6779B2C879E32188E95A236852708DE2BD01` | Global defaults: naming and data-driven dispatch | Applies unless overridden by R |
| R | `D:/dev/work/tera/AGENTS.md` | SHA-256 `00649244131847A64D2C24881446C3A11C14DACF3C57FDDC95D4155E4C419BBB`; latest file commit `9b8a8973cc70e251ee293622f1feee297d96688e` | TERA implementation, verification, review, privacy, reporting | Overrides G on conflicts; explicitly overrides workflow/testing rules in the referenced process, team, and task-template documents |
| Q | User audit request | Current conversation, 2026-09-20 | Review criteria and requested output | Governs this audit; not an existing project workflow definition |

Both files were read from disk. Repository status was clean at inspection. Neither file declares a semantic version; hashes identify the inspected bytes. Completion date uses Europe/London.

**Conflicts:** No direct G–R conflicts found. R's independent review requirement is compatible with its prohibition on automatically launching a fixed agent team: independent review is conditional. R's prose workflow is not itself evidence of a violation of G's conditional code-dispatch rule. G permits two-branch dispatch below its stated threshold; Q requests a stricter proposed table model. This difference is recorded rather than silently replacing G's threshold. R declares precedence over optional documents, but their individual conflicts are outside this two-source audit.

## Assembled Workflow

The following is a normalized interpretation of prose, not an existing executable specification.

| Component | Effective requirements | Source |
|---|---|---|
| States | Intake, scope clarification when needed, implementation, verification, review, correction, completion; blocked work is reported | R: Implementation workflow, Context and scope, Lean testing, Review, Reporting |
| Events | Issue selected; scope resolved; changes ready; checks pass/fail/unavailable; findings accepted/resolved; closure criteria satisfied | R: same sections; event names inferred |
| Transitions | Read issue and relevant contracts before implementation; clarify only unclear scope; implement → verify → review; fix failures and rerun affected checks; close only after criteria and required checks pass | R |
| Guards | One owner/issue; dependency approval; credible evidence for every criterion; review appropriate to risk; no unresolved crashes, contract failures, missing evidence, or unmet criteria | R |
| Actions | Preserve valid work; split oversized tasks with linked issues; implement; verify; review; commit coherent changes; close eligible issue; concise completion report; rename task | R; G: Codex chat naming |
| Data | Issue, acceptance criteria, canonical contracts, revision, changed files, command/result, working-tree changes, relevant environment, risks, follow-ups, next action, project code/date | R; G |
| Interpreter | Human or assistant owner applies prose; no formal dispatcher identified | R: main session may implement, test, review, close |
| Policies | Root precedence; ask before dependencies; local audio/path privacy; DSP measures, Jev judges compatibility, application ranks; centralized request parsing; bounded testing; selective independent review; no duplicate mechanisms | R; G |
| Audit | Verification evidence and concise handoffs/reports; rename on completion; compare available usage/review time/full-suite runs/fix rounds after two completed issues | R; G |

Testing is impact-based: documentation checks for documentation-only work; affected behavior and contract checks during implementation/fixes; full suite before release, integration of separately changed components, or broad/uncertain impact. Reuse valid evidence and broaden checks only when justified. No lint or typecheck is configured. Planning targets are 3–7 observable criteria and under 800 words, without discarding requirements. Existing actionable issues must not be regroomed merely to adopt a process.

## Drift

No optional runtime WORKFLOW was supplied. Missing, extra, or divergent runtime states, events, transitions, guards, actions, data, interpreter, policies, and audit behavior therefore cannot be established. This does not prevent an audit of the supplied instruction specification. Source-code dispatcher inspection and operational log inspection were not performed.

## Verdict

**Adequate as a prose collaboration policy; incomplete as an executable declarative workflow specification.** The existence or exclusivity of a single interpreter cannot be confirmed. No concrete source-code dispatch violation is established.

| Check | Result for current sources |
|---|---|
| Duplicate transition IDs | Not assessable: no transition IDs/table |
| Missing state/event pairs | Not assessable: neither domain is explicitly enumerated |
| Unreachable or dead-end states | Not assessable without a graph; terminal completion is implicit |
| Cycles without exit | Correction cycles are implied; recovery, cancellation, and retry limits are unspecified |
| Conflicting guards/non-determinism | Guard precedence and simultaneous-event handling are unspecified |
| Guard purity | Criteria require evidence, but evaluation boundaries, external calls, mutable reads, and time dependence are unspecified |
| Action safety | Idempotency, retries, timeouts, compensation, rollback, ordering, and atomicity are largely unspecified |
| Data contract | Evidence fields are listed; event/output schemas, state payload, workflow version, and concurrency control are absent |
| Transition audit | Evidence/reporting exists; actor, timestamp, reason, correlation, and authorization are not uniformly required per transition |
| Hard-coded dispatch | No executable dispatcher supplied; no observed per-item conditional dispatch to cite |
| Implicit transitions | Fix/recheck, blocked-work recovery, closure/reporting/naming are prose-only |

## Violations

These are gaps against Q's formal audit target, not proof that repository code violates G or R. Severity concerns adopting an executable workflow from these sources.

| ID | Severity | Location | Evidence | Required change |
|---|---|---|---|---|
| V1 | Major | R: Implementation workflow, Lean testing, Review | Ordering and closure are prose; no finite state/event sets or transition rows | Declare named domains and transition rows, including rejection, correction, blocking, recovery, and completion |
| V2 | Major | G: Data over control flow; R: Implementation workflow | G requires one interpreter when triggered, but no workflow interpreter is identified | Name one transition authority; route every state mutation through it; verify exclusivity against implementation before claiming it |
| V3 | Major | R: Review and Before finishing | Evidence and approval gates lack immutable evaluation inputs and conflict handling | Evaluate pure predicates over a versioned snapshot; move evidence collection and authorization lookup into actions |
| V4 | Major | R: Commit/close/report; Reporting and tool failures | Changed-hypothesis retry rule exists; durable retry identity, timeouts, partial-success recovery, and compensation do not | Define action receipts, bounded retry rules, deadlines, reconciliation, ordering, and recovery for external effects |
| V5 | Major | R: Context and scope, Lean testing | Some evidence fields exist; state/event schema, versioning, ownership enforcement, and atomic update rules do not | Define named records, validation, owner lease, expected state version, and atomic state/audit persistence |
| V6 | Major | R: Reporting; G: Codex chat naming | Completion reports and names do not establish a per-transition audit trail | Record source/target, event, actor, time, reason, correlation, authorization evidence, action receipts, and revision per accepted/rejected attempt |
| V7 | Minor | R: after-two-issues process comparison | Trigger exists without a counter scope or persisted checkpoint | Define the counter scope, reset/checkpoint rule, and unavailable-metric treatment |

## Declarative Refactor

| Table | ID | Name | Description | Required fields | Constraints | Source |
|---|---|---|---|---|---|---|
| States | S0 | Intake | Selected work awaiting scope assessment | issue, owner | One active issue per owner | R |
| States | S1 | Clarification | Scope unresolved | questions, criteria | Preserve existing valid work | R |
| States | S2 | Implementation | Authorized work in progress | scope, baseline, approvals | Dependency additions require approval | R |
| States | S3 | Verification | Required evidence collected | change revision, verification plan | Impact determines check breadth | R |
| States | S4 | Review | Findings and acceptance assessed | evidence, risk, reviewer | Independent reviewer for named risk categories | R |
| States | S5 | Completion | Ordered completion effects pending | accepted revision, action receipts | Revalidate evidence before issue closure | R; proposed recovery |
| States | S6 | Done | Completion effects confirmed | closure receipt, report, task title | Terminal | R, G |
| States | S7 | Blocked | Cannot safely progress | prior state, blocker, resume target | Resume target must have a row | R; proposed |
| States | S8 | Cancelled | Explicitly cancelled work | actor, reason | Terminal; no automatic issue closure | Proposed behavior addition |

| Table | ID | Name | Description | Required fields | Constraints | Source |
|---|---|---|---|---|---|---|
| Events | E1 | Scope assessed | Scope assessment complete | outcome, criteria | outcome in {clear, unclear} | R; proposed schema |
| Events | E2 | Scope resolved | Clarification complete | approved scope | Explicit unresolved requirements retained | R |
| Events | E3 | Changes ready | Revision ready for checks | revision, change set | Named immutable revision | R |
| Events | E4 | Checks assessed | Required checks evaluated | outcome, evidence | outcome in {pass, fail}; unavailable uses E8 | R; proposed schema |
| Events | E5 | Review assessed | Review complete | outcome, findings, reviewer | outcome in {accepted, changes required} | R |
| Events | E6 | Effects confirmed | Completion receipts reconciled | receipts | All required effects confirmed | Proposed |
| Events | E7 | Resume requested | Blocker resolved | target, evidence | Authorized target from persisted prior state | Proposed |
| Events | E8 | Blocked | Required progress unavailable | reason, prior state | Preserve receipts and evidence | R; proposed schema |
| Events | E9 | Cancel requested | Explicit cancellation | actor, reason | Authorized request | Proposed behavior addition |

| Table | ID | Name | Description | Required fields | Constraints | Source |
|---|---|---|---|---|---|---|
| Guards | G1 | Clear scope | Scope outcome is clear | snapshot.scope outcome | Pure; mutually exclusive with G2 | R |
| Guards | G2 | Unclear scope | Scope outcome is unclear | snapshot.scope outcome | Pure; mutually exclusive with G1 | R |
| Guards | G3 | Work authorized | Scope and required approvals present | scope, approvals, owner | No authorization service calls during evaluation | R; proposed boundary |
| Guards | G4 | Checks pass | Required current evidence passes | check plan, evidence, revision | No stale or unavailable required evidence | R |
| Guards | G5 | Checks fail | Assessment records failure | check assessment | Mutually exclusive with G4 | R |
| Guards | G6 | Review accepted | All criteria supported; no blocking findings; appropriate reviewer | criteria, evidence, findings, risk, reviewer | Reviewer independent when risk requires it | R |
| Guards | G7 | Changes required | Review requests changes | review outcome | Mutually exclusive with G6 | R |
| Guards | G8 | Effects complete | Required completion receipts present | receipts, workflow version | Includes title and any due process comparison | R, G; proposed |
| Guards | G9 | Resume valid | Blocker resolved and target matches saved prior state | resolution, prior state, target | Snapshot-only; no arbitrary target | Proposed |
| Guards | G10 | Authorized event | Event permitted for actor | authorization snapshot | Pure; applied uniformly to every row | Proposed |

| Table | ID | Name | Description | Required fields | Constraints | Source |
|---|---|---|---|---|---|---|
| Actions | A1 | Prepare scope | Read relevant issue/contracts; clarify or split as needed | issue, canonical references | No bulk regrooming | R |
| Actions | A2 | Perform work | Apply approved scope | baseline, scope, approvals | Local audio/paths; preserve valid work; approved dependencies only | R |
| Actions | A3 | Verify | Collect impact-appropriate checks | revision, change set, plan | Reuse only applicable evidence | R |
| Actions | A4 | Review | Review requirements, diff, evidence, focused risks | risk, revision, evidence | Reviewer selected by policy; no duplicate comprehensive suite | R |
| Actions | A5 | Complete | Named dependency order: commit → closure → report → title → due process comparison | revision, evidence, receipts, project code, local date, completion counter | Reconcile existing receipts before repeat; closure guard rechecked against committed content | R, G; proposed ordering |
| Actions | A6 | Record blocker | Persist reason, prior state, receipts; report limitation | blocker, prior state | No false success | R; proposed persistence |
| Actions | A7 | Resume | Reconcile outstanding action before continuing | receipts, resolution, target | Never repeat irreversible effects blindly | Proposed |
| Actions | A8 | Cancel | Stop pending work and record request | actor, reason, receipts | Preserve changes and evidence; no automatic rollback of user work | Proposed |

| Table | ID | Name | Description | Required fields | Constraints | Source |
|---|---|---|---|---|---|---|
| Transitions | T01 | Assess clear | S0 + E1 → S2; G1; A1,A2 | source, event, guard, actions, target | Common contract C1 | R; normalized |
| Transitions | T02 | Assess unclear | S0 + E1 → S1; G2; A1 | same named fields | C1 | R; normalized |
| Transitions | T03 | Resolve | S1 + E2 → S2; G3; A2 | same named fields | C1 | R; normalized |
| Transitions | T04 | Verify changes | S2 + E3 → S3; G3; A3 | same named fields | C1 | R; normalized |
| Transitions | T05 | Review passing work | S3 + E4 → S4; G4; A4 | same named fields | C1 | R; normalized |
| Transitions | T06 | Fix failed checks | S3 + E4 → S2; G5; A2 | same named fields | C1 | R; normalized |
| Transitions | T07 | Complete accepted work | S4 + E5 → S5; G6; A5 | same named fields | C1 | R; normalized |
| Transitions | T08 | Fix findings | S4 + E5 → S2; G7; A2 | same named fields | C1 | R; normalized |
| Transitions | T09 | Finish | S5 + E6 → S6; G8; no effects | same named fields | C1 | Proposed |
| Transitions | T10–T15 | Block | Sources {S0,S1,S2,S3,S4,S5}, each + E8 → S7; G10; A6 | one unique row ID per named source | C1; expand before validation | Proposed |
| Transitions | T16–T21 | Resume | S7 + E7 → each named target {S0,S1,S2,S3,S4,S5}; G9; A7 | one unique row ID per named target | C1; target-specific equality predicate; expand before validation | Proposed |
| Transitions | T22–T28 | Cancel | Sources {S0,S1,S2,S3,S4,S5,S7}, each + E9 → S8; G10; A8 | one unique row ID per named source | C1; expand before validation | Proposed |

| Table | ID | Name | Description | Required fields | Constraints | Source |
|---|---|---|---|---|---|---|
| Data | D1 | Event input | Named event envelope | ID, event, issue, actor, reason, correlation, expected version, payload | Validate known event and payload schema; reject unknown fields/types according to explicit schema | Proposed |
| Data | D2 | State payload | Durable workflow record | issue, owner, state, version, workflow version, revision, scope, evidence, findings, approvals, receipts, blocker | Owner lease; optimistic version check; one active issue per owner | R; proposed enforcement |
| Data | D3 | Result output | Transition result | status, transition ID, prior/next state, version, receipts, rejection reason | Explicit statuses {accepted, rejected, pending}; no implicit state mutation | Proposed |
| Data | D4 | Action plan | Named effect dependencies | action ID, dependencies, idempotency key, deadline, retry limit, compensation reference | Acyclic dependency graph; bounded retries; reconcile uncertain external outcomes | Proposed |
| Data | D5 | Metrics checkpoint | Session completion comparison | session ID, completed count, last checkpoint, available metrics | Every two additional completed issues; unavailable metrics recorded as unavailable | R; proposed counter scope |
| Data | D6 | Catalog | Versioned workflow tables | schema version, states, events, transitions, guards, actions, policies, audit definitions | Referential integrity; distinct IDs; migration required for incompatible in-flight changes | Proposed |

| Table | ID | Name | Description | Required fields | Constraints | Source |
|---|---|---|---|---|---|---|
| Policies | C1 | Common transition contract | Validate input, ownership, G10, version; select exactly one enabled row | D1,D2,D6 | Zero or multiple enabled rows reject with audit; never use default transition | G; proposed mechanics |
| Policies | P1 | Verification selection | Documentation checks; affected/contract checks; full suite at stated checkpoints | change classification, impact, checkpoint | No unnecessary repeated broad checks; no configured lint/typecheck requirement | R |
| Policies | P2 | Review selection | Owner review for low risk; independent review for enumerated risks | risk categories, reviewer relation | Ranking/DSP, persistence/migrations, concurrency, resource ownership, privacy/security, external error contracts require independence | R |
| Policies | P3 | Product boundaries | Local audio/path privacy; approved dependencies; canonical architecture and parsing | data destination, approvals, contract references | Measured facts, compatibility judgments, and ranking retain stated owners | R |
| Policies | P4 | Declarative dispatch | One transition authority and existing catalog | catalog, interpreter identity | Unknown input rejects; new cases change data; G's implementation threshold remains recorded | G; Q |
| Policies | P5 | Effect recovery | Durable intent/receipts; ordered execution; timeout and reconciliation | D4, receipts | No global atomicity claim across external systems; state/audit commit atomic locally; partial effects retained for recovery | Proposed |
| Policies | P6 | Retry and compensation | Changed hypothesis for failed tool retries; bounded attempts | hypothesis, attempt count, deadline, compensation policy | No automatic compensation of published commits, issue closure, or user changes; require authorized recovery action | R; proposed |
| Policies | P7 | Completion naming | TERA - actual completed result - local date | project code, result, Europe/London date | Update when main result changes | G; R project identity |

| Table | ID | Name | Description | Required fields | Constraints | Source |
|---|---|---|---|---|---|---|
| Audit | AU1 | Transition attempt | Record accepted/rejected/pending attempt | attempt ID, event ID, transition ID when selected, actor, timestamp, reason, correlation, authorization reference, before/after state and version | Append-only; atomic with local state update; no audio/local path disclosure | Q; proposed |
| Audit | AU2 | Effect receipt | Record action result and reconciliation | action ID, idempotency key, attempt, start/end time, outcome, receipt, failure reason | Durable before acknowledging success; redact private data | Proposed |
| Audit | AU3 | Verification evidence | Preserve credible current evidence | command, result, revision, working-tree changes, environment, criteria links | Invalidation rules bind evidence to changed behavior | R |
| Audit | AU4 | Completion record | Concise result and required process checkpoint | delivered behavior, verification, limitations, follow-ups, title, metrics checkpoint | No duplicate reports; unavailable checks explicit | R, G |

## Interpreter Contract

One proposed authority, **Workflow Interpreter**, owns state transitions. Action executors return receipts/events and cannot assign workflow state. The current sources do not establish that this authority exists or is exclusive.

```text
validate and load the versioned catalog
repeat for each received event:
    read event
    validate its named input schema
    capture one immutable state, evidence, and authorization snapshot
    find transition rows matching the snapshot state and event
    evaluate their guards against that snapshot
    require exactly one enabled transition and valid owner/version
    persist transition intent and named action plan
    execute the plan through the common effect executor; retain receipts
    require the transition's required effect receipts
    atomically update state/version and append the transition audit record
    acknowledge the event using its durable result
```

Uniform rejection records a reason without changing state. Effect failure preserves pending intent; an explicit Blocked event uses the defined transition rows. Recovery reconciles receipts before resuming the pending plan. Duplicate events return their stored result. Stale versions reject; they do not replay effects. Guard evaluation performs no external calls, mutable reads, or clock reads. Timestamps are captured as audit data, not hidden guard inputs. Action ordering uses named dependencies rather than positional meaning.

The proposed graph has reachable terminal states Done and Cancelled, explicit correction exits, and recoverable blocking. Unlisted pairs are rejected, not inferred. Reachability is structural, not a guarantee of eventual completion. Before execution, expand grouped transition declarations into distinct rows and validate uniqueness, references, reachability, terminal states, guard exclusivity, and dependency cycles. Proving sole dispatch additionally requires implementation inspection for all state writes and discriminator dispatch; that evidence is absent.

## Test Matrix

This is a proposed matrix, not executed verification. Every row also requires valid authorization, ownership, and version; their failure leaves state unchanged, runs no effects, and records rejection.

| State | Event | Guard true: next state / actions | Guard false: next state / actions |
|---|---|---|---|
| Intake | Scope assessed: clear | G1: Implementation / Prepare scope, Perform work | Intake / none; reject when no alternate row enabled |
| Intake | Scope assessed: unclear | G2: Clarification / Prepare scope | Intake / none; reject when no alternate row enabled |
| Clarification | Scope resolved | G3: Implementation / Perform work | Clarification / none; reject |
| Implementation | Changes ready | G3: Verification / Verify | Implementation / none; reject |
| Verification | Checks assessed: pass | G4: Review / Review | Verification / none; reject when no alternate row enabled |
| Verification | Checks assessed: fail | G5: Implementation / Perform work | Verification / none; reject when no alternate row enabled |
| Review | Review assessed: accepted | G6: Completion / Complete | Review / none; reject when no alternate row enabled |
| Review | Review assessed: changes required | G7: Implementation / Perform work | Review / none; reject when no alternate row enabled |
| Completion | Effects confirmed | G8: Done / none | Completion / none; reject |
| Each of Intake, Clarification, Implementation, Verification, Review, Completion | Blocked | G10: Blocked / Record blocker | Original state / none; reject |
| Blocked | Resume requested for each declared target | G9: recorded target / Resume | Blocked / none; reject |
| Each nonterminal state | Cancel requested | G10: Cancelled / Cancel | Original state / none; reject |
| Done or Cancelled | Any event | No row: unchanged / none; reject | Same |
| Any state | Unknown event or absent state/event pair | No row: unchanged / none; reject | Same |
| Any state | Event with multiple enabled rows | Reject ambiguity / none | Zero enabled rows also reject |
| Any state | Duplicate acknowledged event | Stored result / no repeated effects | Different payload with same ID rejects |
| Any state | Stale expected version | Unchanged / none; reject | Current version follows its defined row |
| Review | Accepted review with stale evidence, missing criterion, or missing required independent review | G6 cannot pass: unchanged / none | Review / none; reject |
| Completion | Partial closure/report/title effects | Retain receipts and pending intent; explicit Blocked event → Blocked / Record blocker | Never mark Done without G8 |

Document-only audit: no product code changed, no runtime tests executed, and no claim of runtime conformance made. Proposed cancellation, durable orchestration, audit persistence, recovery, and counter semantics are explicit behavior additions; they are not existing requirements disguised as refactoring.
