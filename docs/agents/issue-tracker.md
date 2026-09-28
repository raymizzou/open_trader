# Issue tracker: GitHub

Issues and specs live in `raymizzou/open_trader` on GitHub.
Use the `gh` CLI inside this repository.

## Conventions

- Create: `gh issue create --title "..." --body-file <file>`
- Read: `gh issue view <number> --comments`
- List: `gh issue list --state open --json number,title,body,labels`
- Comment: `gh issue comment <number> --body-file <file>`
- Apply labels: `gh issue edit <number> --add-label "..."`
- Remove labels: `gh issue edit <number> --remove-label "..."`
- Close: `gh issue close <number>`

For multiline bodies, write the exact Markdown to a temporary file
and pass it with `--body-file`.

“Publish to the issue tracker” means create a GitHub issue.
“Fetch the relevant ticket” means read the issue and its comments.
Follow the session's authorization rules for external writes.

## Pull requests as a triage surface

**PRs as a request surface: no.**

GitHub shares issue and PR numbers. If a number is ambiguous,
resolve with `gh pr view <number>`, falling back to `gh issue view`.

## Wayfinding operations

- Map: one issue labelled `wayfinder:map`, containing Notes,
  Decisions-so-far, and Fog.
- Child tickets: link as GitHub sub-issues; if unavailable, use a
  task list in the map and `Part of #<map>` in each child.
- Types: `wayfinder:research`, `wayfinder:prototype`,
  `wayfinder:grilling`, and `wayfinder:task`.
- Blocking: use native GitHub issue dependencies; if unavailable,
  use `Blocked by: #<number>` in the child.
- Frontier: choose the first open, unassigned child in map order
  with no open blockers.
- Claim: `gh issue edit <number> --add-assignee @me`.
- Resolve: record the answer, close the child, and link its result
  in the map's Decisions-so-far.
