# UI Design Approval Gate

## Scope

This gate applies to every new feature that adds or changes a user-facing
interface. Backend-only features with no user interface are outside its scope.

## Required sequence

1. Create an isolated UI design or prototype before implementing the production
   interface.
2. Show the main states and both desktop and mobile layouts.
3. Ask the user for explicit approval.
4. Only after approval may production UI implementation, final acceptance, or
   deployment begin.

If the approved layout or interaction changes materially, obtain approval again
before continuing production UI work.

## Success criterion

`AGENTS.md` states this gate unambiguously so future agents cannot treat a
functional specification or information-field list as approved UI design.
