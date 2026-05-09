# Beads

This project tracks work as **beads**: small, agent-sized units of work that can be picked up, implemented, tested, and shipped in a single agent run. Beads are strung together through epics and explicit dependencies so that progress is visible end-to-end on the GitHub issue tracker.

## What a bead is

A bead is an issue that satisfies all of the following:

1. **Small enough for one agent run.** If a bead cannot be completed in one focused pass, split it.
2. **Belongs to an epic.** Every bead is filed under exactly one epic issue. The architecture and work-plan documents describe the epics; see `docs/work-plan.md`.
3. **Has explicit acceptance criteria.** Observable, testable outcomes. The bead is not done until every criterion is checked.
4. **Has explicit dependencies.** Other beads it depends on are recorded with `Depends on #NNN`; beads it unblocks use `Blocks #NNN`. GitHub renders these cross-references automatically.
5. **Ships with tests.** New and changed code is covered by tests, and all tests pass locally and in CI before the bead is closed.

## How beads relate to epics

- Epics are tracking issues. They are not implemented directly; they are decomposed into beads.
- Prefer **GitHub native sub-issues** to attach a bead to its epic. If sub-issues are not appropriate (for example, when a bead spans two epics), reference the epic from the bead's body and add the bead to the epic's task list.
- An epic is closed only when all of its beads are closed and its acceptance criteria are met.

## Filing a bead

Open a new issue using the **Agent bead** template (`.github/ISSUE_TEMPLATE/agent-bead.yml`). The template enforces:

- a parent epic reference,
- in-scope and out-of-scope sections,
- an acceptance-criteria checklist,
- explicit `Depends on` / `Blocks` dependency references,
- a test plan,
- a definition-of-done checklist that cannot be skipped.

If a field does not apply, say so explicitly rather than leaving it blank.

## Dependencies

Beads must record the issues they depend on using the GitHub issue tracker's native cross-reference syntax:

- `Depends on #NNN` — this bead cannot ship until `#NNN` is merged.
- `Blocks #NNN` — `#NNN` cannot ship until this bead is merged.

GitHub renders these references in the timeline and in linked-issue panels, so dependency state is visible without leaving the tracker. A bead with no dependencies must say so explicitly (for example, "No upstream dependencies — foundational bead under epic #NN").

## Tests and coverage

A bead is not done unless:

- every code path it adds or changes is covered by tests,
- those tests run in CI on the pull request that closes the bead, and
- the full test suite passes.

If tests cannot be added for a particular reason (for example, a bead that only edits documentation), the bead must justify it explicitly in its test plan. "I'll add tests later" is not acceptable; that work belongs in a follow-up bead linked with `Blocks`.

## Definition of done

The agent-bead template includes a definition-of-done checklist. Every box must be checked before the closing pull request is merged:

- [ ] Acceptance criteria are all checked.
- [ ] New and changed code is fully covered by tests.
- [ ] All tests pass locally and in CI.
- [ ] Linked to the parent epic via sub-issue or task list.
- [ ] Bead dependencies are recorded with `Depends on` / `Blocks` references.
- [ ] Documentation is updated where the change is user- or operator-visible.

## Sizing

If a bead grows past a single agent run during implementation, stop and split it. The preferred split is along the natural seams of the pipeline (ingest, indices, change, candidates, events, dashboard), not arbitrary file boundaries. Each resulting bead must independently satisfy the definition of done.
