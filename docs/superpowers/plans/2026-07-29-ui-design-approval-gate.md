# UI Design Approval Gate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a project instruction that prevents production UI implementation before the user approves an isolated design or prototype.

**Architecture:** This is a documentation-only policy change. Add one gate to `AGENTS.md`; do not change application code, tests, or runtime configuration.

**Tech Stack:** Markdown, Git

## Global Constraints

- Apply only to new features that add or change a user-facing interface.
- Backend-only features with no user interface are outside the gate.
- Require main states plus desktop and mobile layouts.
- Require explicit user approval before production UI implementation, final acceptance, or deployment.
- Require renewed approval after a material layout or interaction change.

---

### Task 1: Add the UI design approval gate

**Files:**
- Modify: `AGENTS.md`

**Interfaces:**
- Consumes: `docs/superpowers/specs/2026-07-29-ui-design-approval-gate-design.md`
- Produces: a project-wide agent instruction under `## UI Design Approval Gate`

- [x] **Step 1: Add the approved policy**

Insert this section after `Worktree Baseline`:

```markdown
## UI Design Approval Gate

For every new feature that adds or changes a user-facing interface, create an
isolated UI design or prototype before implementing the production interface.
Show the main states and both desktop and mobile layouts, and obtain the user's
explicit approval.

Do not implement the production UI, run final acceptance, or deploy the feature
before that approval. If the approved layout or interaction changes materially,
obtain approval again before continuing production UI work. Backend-only
features with no user interface are outside this gate.
```

- [x] **Step 2: Verify the policy text**

Run:

```bash
rg -n -A12 '^## UI Design Approval Gate$' AGENTS.md
git diff --check
```

Expected: one gate section containing `isolated UI design or prototype`,
`desktop and mobile`, `explicit approval`, and the backend-only exception;
`git diff --check` exits `0`.

- [x] **Step 3: Commit**

```bash
git add AGENTS.md docs/superpowers/plans/2026-07-29-ui-design-approval-gate.md
git commit -m "docs: require UI design approval"
```
