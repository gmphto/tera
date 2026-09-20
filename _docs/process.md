# Issue workflow

Version: 1.0. Scope: TERA issue work. Root `AGENTS.md` controls product,
testing, approval and review policy; global instructions supply defaults.
This catalog replaces the mandatory PM → Engineer → QA pipeline.

The issue owner is the single interpreter and sole transition authority.
The owner may implement, verify, review and close one issue at a time.
Reviewers return evidence and findings, not state mutations. Independent
review follows the risk categories in root instructions; no fixed agent
team or unconditional grooming stage is required.

This is a human/assistant protocol, not an executable engine. No automatic
locks, transactions, or exactly-once delivery are claimed. Before adding
discriminator branches, follow the global branch-count/spec check. Extend
this existing catalog rather than creating a parallel lifecycle.

## Named records

Keep a compact current record and transition journal in the task conversation
or an existing issue work log. Link it in handoffs; preserve pending receipts
across sessions. Do not duplicate full logs in completion reports.

| ID | Record | Required fields | Constraints |
|---|---|---|---|
| D1 | Work | issue, owner, state, revision, workflow_version, scope, criteria, change_revision, evidence, review, approvals, pending_effects, return_state, journal_reference | Initial state Intake, revision 0, version 1.0; optional values explicitly empty |
| D2 | Event | id, name, actor, reason, correlation_id, expected_revision, payload | Known event and payload schema; ID unique within issue |
| D3 | Snapshot | work, event, authorized, ownership_confirmed, evidence_current, checks_pass, criteria_supported, review_sufficient, blocking_findings, receipts_complete, blocker_resolved | Immutable; missing evidence never interpreted as true |
| D4 | Evidence | command_or_method, result, change_revision, working_tree_changes, environment, criteria_links | Results distinguish pass, fail, unavailable; relevant changes invalidate evidence |
| D5 | Effect | id, action, status, receipt, attempts, hypothesis, deadline, retry_limit, dependencies, recovery | Stable effect ID; status planned, confirmed, failed or uncertain; finite deadline/limit before execution; dependencies named |
| D6 | Result | event_id, status, transition_id, before_state, after_state, revision, reason, receipts | Status accepted, rejected or pending; rejected events preserve state/revision |
| D7 | Audit | event_id, transition_id, actor, timestamp, reason, correlation_id, authorization_reference, before_state, after_state, before_revision, after_revision, workflow_version, evidence_references, receipt_references, status | Offset timestamp; append corrections; absent transition ID explicitly empty |

Validate required fields, field types, enums, payload shapes, references and
expected revision before selection. Unknown/malformed inputs reject. An
authorization reference identifies the applicable user request, project rule
or dependency approval; tool access is not approval. Do not place audio,
private library paths, credentials or transcripts in shared journals.

Confirm latest journal revision before effects and transitions. Handoffs
explicitly release/acquire ownership. Competing ownership or changed revision
stops execution for reconciliation, never blind overwrite. This is cooperative
control, not an atomic lock. A future automated interpreter must enforce atomic
state/audit compare-and-update and durable effect intents/receipts before
claiming concurrent operation.

Pin in-flight work to its workflow version. Adoption maps the existing phase
to a declared state, records that mapping, and preserves valid work/evidence;
do not restart implementation. Incompatible versions require a recorded,
reviewed migration rather than silent reinterpretation.

## States

| ID | Name | Meaning | Required payload / constraint |
|---|---|---|---|
| S1 | Intake | Assess selected issue | issue, criteria; initial |
| S2 | Clarification | Resolve material scope questions | questions; only unclear/conflicting scope |
| S3 | Implementation | Perform authorized work | scope, approvals; preserve valid work |
| S4 | Verification | Collect checks | change_revision, check plan; root test scope |
| S5 | Review | Assess criteria/findings | evidence, review; root reviewer policy |
| S6 | Completion | Commit, close, report, name | accepted evidence, effect receipts |
| S7 | Blocked | Required progress unavailable | reason, return_state, receipts; return_state one of S1–S6 |
| S8 | Done | Completion confirmed | receipts; terminal |
| S9 | Cancelled | User ended work | user instruction, receipts; terminal; no automatic issue closure |

## Events

All events require D2; only the following names/payloads are valid.

| ID | Name | Required payload / constraint |
|---|---|---|
| E1 | Scope clear | scope, criteria; actionable |
| E2 | Scope unclear | material questions |
| E3 | Scope resolved | scope, criteria; required answers recorded |
| E4 | Changes ready | change_revision, change set |
| E5 | Checks passed | current evidence references |
| E6 | Checks failed | failed evidence references; unavailable uses E9 |
| E7 | Review accepted | review reference |
| E8 | Changes required | review reference, findings; also used when completion evidence becomes invalid |
| E9 | Blocked | unavailable resource/approval/evidence reason |
| E10 | Resume | resolution references |
| E11 | Completion confirmed | completion receipt references |
| E12 | Cancel | explicit user instruction reference |

## Guards

All rows require C: valid schema, authorized event, confirmed owner and
matching expected revision. Evaluate only immutable D3: no side effects,
external calls, mutable reads or clock reads. Collect evidence/authorization
before capture; changed evidence invalidates the snapshot. Audit timestamps
are data, not hidden guard inputs.

| ID | Predicate | Required data / constraint |
|---|---|---|
| G1 | Scope actionable, required approvals present | scope, criteria, approvals |
| G2 | Material scope questions remain | nonempty questions |
| G3 | Changes match authorized scope | scope, changes, approvals |
| G4 | Current required checks pass | evidence_current, checks_pass |
| G5 | Failed check evidenced | failed check result |
| G6 | Review accepts current work | evidence_current, checks_pass, criteria_supported, review_sufficient true; no blocking_findings |
| G7 | Changes required evidenced | findings or unsupported criteria |
| G8 | Completion confirmed and acceptance remains valid | receipts_complete and G6 |
| G9 | Required progress unavailable | nonempty blocker reason |
| G10 | Blocker resolved | blocker_resolved |
| G11 | User cancelled | explicit instruction |

## Actions

Actions return evidence/events and cannot assign state. A transition records
entry into a phase, not successful completion of its action.

| ID | Name | Inputs → outputs | Execution / recovery |
|---|---|---|---|
| A1 | Clarify | questions → scope | Ask only needed questions; wait for required answers |
| A2 | Implement | scope, baseline, approvals → changes | Preserve work; no automatic rollback |
| A3 | Verify | revision, impact, plan → evidence | Documentation checks for docs; affected/contract checks normally; full suite only at root checkpoints |
| A4 | Review | revision, evidence, risk → findings | Root reviewer policy; no duplicate comprehensive suite |
| A5 | Complete | accepted work, receipts → completion receipts | Named dependency chain: commit → confirm committed content/evidence → close → report → title |
| A6 | Record blocker | reason, prior state, receipts → blocker | Save prior state as return_state; report unavailable checks |
| A7 | Recover | resolution, receipts → resumed plan | Resume unfinished phase action; never repeat confirmed effects |
| A8 | Cancel | instruction, receipts → cancellation record | Stop pending work; retain changes; no automatic external reversal |

Record D5 before effects and receipts afterward. Stable effect IDs recognize
repetitions; reconcile commit IDs, issue status, report references and titles
before repeating uncertain effects. Default automatic retry limit is zero.
A deliberate retry requires a changed hypothesis and finite new attempt limit.
Select a finite tool timeout before dispatch; expiry means uncertain/failed,
not success. Check whether timed-out operations continue before retrying.

External effects are not one transaction. Preserve partial receipts, submit
Blocked, and Resume only unfinished work. No automatic revert of commits,
deletion of user changes, reopening issues or reversal of external effects.
Compensation requires an authorized recovery plan. Invalidation after closure
must be reported and external state reconciled with required authorization;
never mark Done using invalid evidence.

## Transition catalog

Every row requires C and its guard. `none` means no action. No wildcards.

| ID | State | Event | Guard | Next state | Action |
|---|---|---|---|---|---|
| T01 | Intake | Scope clear | G1 | Implementation | A2 |
| T02 | Intake | Scope unclear | G2 | Clarification | A1 |
| T03 | Clarification | Scope resolved | G1 | Implementation | A2 |
| T04 | Implementation | Changes ready | G3 | Verification | A3 |
| T05 | Verification | Checks passed | G4 | Review | A4 |
| T06 | Verification | Checks failed | G5 | Implementation | A2 |
| T07 | Review | Review accepted | G6 | Completion | A5 |
| T08 | Review | Changes required | G7 | Implementation | A2 |
| T09 | Completion | Completion confirmed | G8 | Done | none |
| T10 | Intake | Blocked | G9 | Blocked | A6 |
| T11 | Clarification | Blocked | G9 | Blocked | A6 |
| T12 | Implementation | Blocked | G9 | Blocked | A6 |
| T13 | Verification | Blocked | G9 | Blocked | A6 |
| T14 | Review | Blocked | G9 | Blocked | A6 |
| T15 | Completion | Blocked | G9 | Blocked | A6 |
| T16 | Blocked | Resume | G10 and return_state = Intake | Intake | A7 |
| T17 | Blocked | Resume | G10 and return_state = Clarification | Clarification | A7 |
| T18 | Blocked | Resume | G10 and return_state = Implementation | Implementation | A7 |
| T19 | Blocked | Resume | G10 and return_state = Verification | Verification | A7 |
| T20 | Blocked | Resume | G10 and return_state = Review | Review | A7 |
| T21 | Blocked | Resume | G10 and return_state = Completion | Completion | A7 |
| T22 | Intake | Cancel | G11 | Cancelled | A8 |
| T23 | Clarification | Cancel | G11 | Cancelled | A8 |
| T24 | Implementation | Cancel | G11 | Cancelled | A8 |
| T25 | Verification | Cancel | G11 | Cancelled | A8 |
| T26 | Review | Cancel | G11 | Cancelled | A8 |
| T27 | Completion | Cancel | G11 | Cancelled | A8 |
| T28 | Blocked | Cancel | G11 | Cancelled | A8 |
| T29 | Completion | Changes required | G7 | Implementation | A2 |

Unlisted pairs or false guards reject without state/revision change or action.
Resume guards are exclusive because return_state has exactly one value.
Correction cycles have success, blocking and cancellation exits. Done and
Cancelled are intentional terminals; later work starts a new record.

T29 invalidates acceptance and completion receipts tied to the changed content.
Keep historical receipts, assign new effect IDs to genuinely new effects, and
recheck affected evidence. Reconcile any already-closed issue before completion;
do not repeat closure blindly or hide an inconsistent external state.

## Interpreter and audit

```text
load and validate the versioned catalog
repeat for each event:
    read and validate the event
    capture a current immutable snapshot
    require common guard C
    find rows matching state and event
    evaluate their pure guards
    require exactly one enabled row
    append accepted transition with next revision and action intent
    derive current state from that record
    execute named action through its effect plan; record receipts
    enqueue evidence event or explicit Blocked event
```

The journal is authoritative; updating its current-state summary is not another
transition. An incomplete journal write prevents effects until reconciled.
Rejected attempts record D6/D7 with unchanged revision and no action. Accepted
transitions increment revision once. Every attempt records actor, timestamp,
reason, correlation and authorization, including recovery and rejection.
Duplicate IDs with identical payload return their recorded result and reconcile
pending effects; changed payload with an existing ID rejects. Reviewers, tools,
role guides and templates cannot bypass this dispatcher.

This defines the sole dispatcher for the documented protocol, not proof of a
software implementation. Runtime conformance requires inspecting all state
writers; do not add an engine or dependencies to satisfy a document workflow.

## Verification matrix

Each catalog row supplies a test: C and guard true produce its exact target
and action; false preserves state/revision with no action. Additional cases:

| State | Event / condition | Expected state | Expected actions |
|---|---|---|---|
| Verification | Checks passed; required check unavailable | Verification | none; explicit Blocked then uses T13 |
| Review | Review accepted; required independent review absent | Review | none |
| Review | Review accepted; stale evidence or unsupported criterion | Review | none |
| Blocked | Resume; resolved, return_state Completion | Completion | A7; confirmed closure not repeated |
| Blocked | Resume; unresolved or invalid return_state | Blocked | none |
| Completion | Completion confirmed; missing report/title receipt | Completion | none |
| Completion | Changes required; acceptance evidence invalidated | Implementation | A2; invalidate content-dependent receipts |
| Any | Unknown input, absent pair, stale revision, unauthorized actor | unchanged | none; audit rejection |
| Any | Multiple enabled rows | unchanged | none; reject ambiguity |
| Any | Duplicate ID, identical payload | recorded result | no repeat of confirmed effects |
| Any | Duplicate ID, changed payload | unchanged | none; reject |
| Done or Cancelled | Any new event | unchanged | none; reject |

Validate unique IDs and references, reachability from Intake, explicit terminals,
paths from every nonterminal to a terminal, Resume exclusivity and acyclic
effect dependencies. These are documentation checks, not product tests. Root
instructions still govern test scope and the after-two-issues comparison.
