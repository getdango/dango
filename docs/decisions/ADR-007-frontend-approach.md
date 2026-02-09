# ADR-007: Frontend Approach

## Status
Proposed

## Context
Multiple v1 tasks require UI work: login page (TASK-018), schedule management UI (TASK-036), data catalog (TASK-059), first-run setup tour (TASK-068), and others. The current MVP uses server-rendered Jinja2 templates with minimal JavaScript. Before building these features, a frontend approach must be chosen that balances interactivity, simplicity, and the team's ability to maintain the code.

This decision is deferred to TASK-086 (Batch 6) because TASK-085 (web/app.py refactoring) must first establish the route structure and API patterns that the frontend will consume.

## Decision
To be decided in TASK-086 after the web layer refactoring (TASK-085) establishes route structure and API conventions.

## Options Under Consideration

### Option A: Server-rendered Jinja2
Extend the current MVP approach. All pages rendered server-side with Jinja2 templates. Minimal JavaScript for form validation and progressive enhancement.

- **Pros:** Simplest architecture, no build step, no JavaScript framework to maintain, works without JavaScript enabled.
- **Cons:** Limited interactivity. Features like real-time sync status, drag-and-drop schedule configuration, or interactive data catalog would require significant custom JavaScript anyway.

### Option B: htmx + Jinja2
Server-rendered templates enhanced with htmx for dynamic updates. HTML fragments returned from the server replace DOM elements without full page reloads.

- **Pros:** Interactive feel without a JavaScript framework. Server-rendered HTML means no JSON API layer needed for the UI. Small library (~14KB). Familiar template-based development.
- **Cons:** Complex interactions (multi-step forms, real-time updates) can become awkward with HTML fragment swapping. Less ecosystem tooling than SPA frameworks.

### Option C: Lightweight SPA (Alpine.js or Preact)
Client-side rendering with a minimal JavaScript framework. Alpine.js (~15KB) for template-driven reactivity, or Preact (~3KB) for a React-like component model.

- **Pros:** Full interactivity, component-based architecture, rich ecosystem (especially Preact). Clean separation between API and UI.
- **Cons:** Requires a JSON API layer, introduces a JavaScript build step (or uses ESM imports), and adds frontend complexity that the team must maintain.

## Rationale
Deferred — the decision depends on the API patterns established by TASK-085 and the specific interactivity requirements surfaced during implementation of earlier UI tasks.

## Alternatives Considered
See options above. The final decision will weigh the interactivity needs of the actual UI tasks against the maintenance burden of each approach.

## Consequences
- Until this decision is made, UI tasks that need to start before TASK-086 should use server-rendered Jinja2 templates (the current approach). This work will not be wasted — all three options use or can coexist with Jinja2.
- The decision will affect the developer experience for all future UI work. Choosing a heavier framework means more capability but also more tooling to maintain.
- TASK-086 should produce a proof-of-concept implementing one UI feature (e.g., the schedule management page) with the chosen approach, to validate the decision before committing to it across all UI tasks.
