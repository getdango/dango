# ADR-007: Frontend Approach

## Status
Accepted

## Context
Multiple v1 tasks require UI work: login page (TASK-018), schedule management UI (TASK-036), data catalog (TASK-059), first-run setup tour (TASK-068), and others. A frontend approach must be chosen that balances interactivity, simplicity, and maintainability.

The actual MVP architecture (prior to this decision) was:
- **Static HTML files** served via `file.read_text()` with string replacement for version injection — not Jinja2 templates
- **Heavy client-side JavaScript** — `app.js` (2,157 lines) fetches JSON from REST APIs and renders everything client-side
- **WebSocket** for real-time updates (sync status, activity log)
- The architecture was already a **vanilla JS client-side app** consuming JSON APIs, not a server-rendered app

This decision was deferred to TASK-086 (after TASK-085 completed web/app.py refactoring) so the route structure and API patterns were established first.

## Decision
Use **Alpine.js** for client-side interactivity combined with **Jinja2 base templates** for shared layout (header, navigation, footer). No build step required — Alpine.js loaded via CDN.

**Template infrastructure:**
- `web/templates/base.html` — shared layout with configurable blocks (`title`, `header_right`, `content`, `footer`, `scripts`)
- Page templates extend `base.html` and override blocks for page-specific content
- Navigation active state driven by `current_page` template variable
- Version string injected via Jinja2 `{{ version }}` context variable (replacing the old `{{DANGO_VERSION}}` string replacement)

**New pages** should use Alpine.js `x-data` for reactive state and `x-init` for data loading. Existing pages continue with vanilla JS and can be migrated incrementally.

## Rationale
Alpine.js matches the existing architecture: a client-side app consuming JSON APIs. The dashboard already fetches all data via REST endpoints and renders it in JavaScript — Alpine.js provides declarative reactivity for this pattern without requiring architectural changes.

**Why not htmx?** htmx returns HTML fragments from the server, which would require rewriting all API endpoints to return HTML instead of JSON. The existing endpoints serve both the web UI and the CLI (`dango` commands), so breaking the JSON API pattern would be disruptive. htmx is a better fit for server-rendered architectures, not client-side apps.

**Why not vanilla JS?** The current 2,157-line `app.js` demonstrates the maintenance cost of vanilla JS at scale — manual DOM manipulation, string-based HTML construction, and imperative state management. Alpine.js adds declarative reactivity (~17KB) without requiring a build step or changing the deployment model.

**Why Jinja2 templates?** The three existing pages (dashboard, health, logs) duplicated ~90 lines of identical header, navigation, and Tailwind configuration. Jinja2 template inheritance eliminates this duplication and makes adding new pages straightforward.

## Alternatives Considered

### Option A: Server-rendered Jinja2 only
Render all pages server-side with minimal JavaScript.
- **Rejected:** The existing architecture is already client-side — `app.js` fetches JSON and renders everything in the browser. Converting to server-side rendering would require rewriting the entire dashboard, and features like real-time WebSocket updates and interactive modals would still need JavaScript.

### Option B: htmx + Jinja2
Server-rendered templates enhanced with htmx for dynamic updates via HTML fragment swapping.
- **Rejected:** Would require all API endpoints to return HTML fragments in addition to (or instead of) JSON. The existing JSON API is consumed by both the web UI and CLI commands. Complex interactions (multi-step modals, real-time WebSocket updates, drag-and-drop) are awkward with fragment swapping.

### Option C: Full SPA (React/Vue/Preact)
Client-side rendering with a full JavaScript framework.
- **Rejected:** Introduces a build step (webpack/vite), `node_modules`, and significantly more tooling complexity. Overkill for Dango's UI complexity. Alpine.js provides sufficient reactivity without the operational burden.

## Consequences
- **New pages** use Alpine.js for interactivity + Jinja2 templates for layout. No build step.
- **Existing pages** (dashboard, health, logs) continue with their current vanilla JS. Migration to Alpine.js is optional and can happen incrementally.
- **Alpine.js loaded via CDN** (`cdn.jsdelivr.net`), same pattern as Tailwind CSS. No `node_modules` or build tooling.
- **Template inheritance** means shared layout changes (header, nav, footer) are made in one place (`base.html`).
- **Future migration path:** If the UI grows significantly more complex, Alpine.js components can be extracted into separate `.js` files. The JSON API pattern is preserved regardless of frontend approach.
