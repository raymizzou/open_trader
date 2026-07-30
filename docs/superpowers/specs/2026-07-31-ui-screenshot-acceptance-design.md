# UI Screenshot Acceptance Design

## Goal

Make screenshots a required handoff artifact for every user-visible UI change,
without adding a separate approval system or waiting for user confirmation.

## Decision

Update the project instructions only. The existing Dashboard acceptance already
captures desktop and mobile screenshots, so no new manifest, approval file, or
acceptance command is needed.

A change is a UI change when it alters visible content or interaction, including
layout, styling, labels, navigation, responsive behavior, or browser-rendered
states. When uncertain, treat the change as UI-facing.

## Acceptance Flow

1. Run focused verification while developing.
2. Run `make acceptance` as the final automated gate.
3. After `PASS`, deploy the exact accepted SHA and verify the live process.
4. Capture the affected view from the deployed URL.
5. Include the screenshot inline in the final user-facing response.

Desktop and mobile screenshots are both required when responsive behavior or
mobile layout changed. Otherwise, screenshots only need to cover the affected
view clearly.

The agent does not wait for user approval after sending the screenshot. A UI
task may be called accepted once the automated gate passed, the exact SHA was
deployed, and the final response includes the current deployed screenshot.

## Failure Rules

A UI task is not accepted when the required screenshot is missing, empty,
unreadable, stale, captured from a different SHA, or does not show the affected
view. A URL or textual description does not replace the screenshot.

## Scope

This change updates `AGENTS.md` only. It does not modify application code,
`make acceptance`, screenshot generation, or runtime deployment behavior.
