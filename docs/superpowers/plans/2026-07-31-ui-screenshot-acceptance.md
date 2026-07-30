# UI Screenshot Acceptance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Require every user-visible UI change to include screenshots from the exact deployed SHA in the final user-facing response, without waiting for user approval.

**Architecture:** Extend the existing project instructions rather than changing `make acceptance` or adding approval state. Keep automated `PASS` as the machine gate, then require deployed screenshots as the final UI handoff artifact.

**Tech Stack:** Markdown project instructions, Git, existing `make acceptance` and Dashboard screenshot workflow.

## Global Constraints

- Do not add an approval file, manifest, dependency, or new acceptance command.
- User confirmation is not required after screenshots are sent.
- Screenshots must come from the exact deployed and accepted SHA.
- Desktop and mobile screenshots are both required only when responsive behavior or mobile layout changed.
- A missing, empty, unreadable, stale, wrong-SHA, or irrelevant screenshot blocks an accepted/completed claim.
- Update and commit `CHANGELOG.md` before merging to `main`.

---

### Task 1: Add the UI screenshot handoff gate

**Files:**
- Modify: `AGENTS.md`
- Modify: `CHANGELOG.md`

**Interfaces:**
- Consumes: the existing `make acceptance` `PASS` result and post-acceptance exact-SHA deployment proof.
- Produces: a project instruction requiring inline screenshots in the final response for every user-visible UI change.

- [ ] **Step 1: Add the project instruction**

Insert this section after `Post-Acceptance Review Deployment` in `AGENTS.md`:

```markdown
## UI Screenshot Handoff Gate

For any UI change that alters visible content or interaction, `make acceptance`
and deployment proof are not sufficient by themselves. After deploying the
exact accepted SHA, capture the affected view from the live review URL and
include the screenshot inline in the final user-facing response.

If responsive behavior or mobile layout changed, include both desktop and
mobile screenshots. Otherwise, screenshots only need to show the affected view
clearly. User confirmation is not required after the screenshots are sent.

Missing, empty, unreadable, stale, wrong-SHA, or irrelevant screenshots block
the task from being described as accepted, complete, or deployed successfully.
A URL or textual description does not replace the screenshot.
```

Extend the final `Task Handoff Gate` bullet from:

```markdown
- Only on `PASS` may the agent provide the deployed URL and ask the user to
  review the result.
```

to:

```markdown
- Only on `PASS` may the agent provide the deployed URL and ask the user to
  review the result. UI changes must also satisfy the UI Screenshot Handoff
  Gate before the task may be called accepted or complete.
```

- [ ] **Step 2: Add the dated operator-facing changelog entry**

Under `## 2026-07-31` in `CHANGELOG.md`, add:

```markdown
- Required every user-visible UI change to include screenshots from the exact
  deployed and accepted SHA in the final response. Responsive or mobile changes
  require desktop and mobile views; missing, stale, or irrelevant screenshots
  now block an accepted/completed claim without adding a user-approval wait.
```

Create the `## 2026-07-31` heading directly above `## 2026-07-30` if it does not
already exist.

- [ ] **Step 3: Verify the exact policy text and formatting**

Run:

```bash
rg -n \
  'UI Screenshot Handoff Gate|User confirmation is not required|exact accepted SHA|screenshot inline' \
  AGENTS.md
rg -n \
  'user-visible UI change|user-approval wait' \
  CHANGELOG.md
git diff --check
```

Expected:

```text
AGENTS.md contains the new gate and no-wait rule.
CHANGELOG.md contains the dated operator-facing entry.
git diff --check exits 0 with no output.
```

Do not run `make acceptance`: this task changes project instructions only and
does not change application behavior or UI output.

- [ ] **Step 4: Commit the instruction change**

```bash
git add AGENTS.md CHANGELOG.md
git commit -m "docs: require UI screenshots at handoff"
```

Expected: one documentation commit containing only `AGENTS.md` and
`CHANGELOG.md`.

---

### Task 2: Integrate the approved rule

**Files:**
- Verify: `AGENTS.md`
- Verify: `CHANGELOG.md`

**Interfaces:**
- Consumes: the committed instruction change from Task 1.
- Produces: local `main` containing the new acceptance rule while preserving unrelated dirty-root files.

- [ ] **Step 1: Synchronize with the latest local `main`**

Run in the isolated worktree:

```bash
git merge --no-edit main
```

Expected: clean merge or `Already up to date`. If conflicts occur, resolve only
`AGENTS.md` or `CHANGELOG.md` and preserve all newer operator entries.

- [ ] **Step 2: Re-run the documentation verification**

```bash
rg -n \
  'UI Screenshot Handoff Gate|User confirmation is not required|exact accepted SHA|screenshot inline' \
  AGENTS.md
rg -n \
  'user-visible UI change|user-approval wait' \
  CHANGELOG.md
git diff --check
git status --short --branch
```

Expected: the policy remains present, formatting is clean, and the worktree has
no uncommitted changes.

- [ ] **Step 3: Fast-forward local `main`**

Run in the main checkout:

```bash
git merge --ff-only docs/ui-screenshot-acceptance
```

Expected: `main` advances to the documentation commit without touching existing
unrelated uncommitted files.

- [ ] **Step 4: Verify the final main commit**

```bash
git merge-base --is-ancestor 80dd76a main
git merge-base --is-ancestor docs/ui-screenshot-acceptance main
git show --stat --oneline main
git status --short --branch
```

Expected: the design commit and instruction commit are ancestors of `main`;
`git show` lists only the intended documentation files for the final commit;
pre-existing unrelated dirty-root files remain unchanged.
