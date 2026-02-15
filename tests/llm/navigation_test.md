# LLM Navigation Test (TEST-000)

## Purpose

Phase 1 gate test. Validates that a fresh LLM can navigate the Dango codebase using **only documentation** — no source code reading required. All answers must be findable through CLAUDE.md files and ARCHITECTURE.md alone.

Re-run at Phase 8 (DOC-015) before v1 release to verify documentation stays current as the codebase evolves.

## Prerequisites

Provide the following to a fresh LLM session (e.g., a new Claude Code conversation in `dango/`):

1. **Repository CLAUDE.md** (`dango/CLAUDE.md`) — auto-loaded by Claude Code
2. **All 13 module CLAUDE.md files:**
   - `dango/CLAUDE.md` (package root)
   - `dango/cli/CLAUDE.md`
   - `dango/config/CLAUDE.md`
   - `dango/ingestion/CLAUDE.md`
   - `dango/oauth/CLAUDE.md`
   - `dango/transformation/CLAUDE.md`
   - `dango/visualization/CLAUDE.md`
   - `dango/security/CLAUDE.md`
   - `dango/utils/CLAUDE.md`
   - `dango/templates/CLAUDE.md`
   - `dango/web/CLAUDE.md`
   - `dango/migrations/CLAUDE.md`
3. **ARCHITECTURE.md** — ask the LLM to read it before starting questions

**Do NOT provide:**
- Source code files (`.py`)
- `V1_PLAN.md` or any workspace-level docs
- The workspace `CLAUDE.md` (only the repo-level one)

## Test Procedure

1. Start a fresh Claude Code session in the `dango/` repository directory
2. Let the session auto-read the repo CLAUDE.md (happens automatically)
3. Ask: "Please read ARCHITECTURE.md and all module CLAUDE.md files listed in the repo CLAUDE.md"
4. Wait for the LLM to confirm it has read them
5. Ask each of the 5 questions below, one at a time
6. Record which files/sections the LLM references in its answer
7. Score each answer using the rubric below
8. Record results in the Results Log section

**Important:** Do not give hints or follow-up prompts. Each question must be answered from the documentation the LLM already read.

## Questions and Expected Answers

### Q1: "Where would you look to add a new CLI command?"

**Expected navigation path:**
- Root `CLAUDE.md` routing table → "CLI commands → `dango/cli/` → `cli/CLAUDE.md`"
- `cli/CLAUDE.md` → "Common Tasks" section (line 80)

**Expected key points in answer:**
- Create command in `dango/cli/commands/` (new file or add to existing)
- Register in `main.py` via `cli.add_command()`
- For a subcommand: add to relevant `commands/*.py`

**Scoring:**
| Score | Criteria |
|-------|----------|
| 1.0 | Identifies `commands/` directory AND `main.py` registration. References `cli/CLAUDE.md` Common Tasks. |
| 0.5 | Identifies `cli/` module correctly but missing either `commands/` directory or `main.py` registration step. |
| 0.0 | Cannot locate the CLI module or gives incorrect file paths. |

---

### Q2: "Where is OAuth token refresh handled?"

**Expected navigation path:**
- Root `CLAUDE.md` routing table → "OAuth / token flows → `dango/oauth/` → `oauth/CLAUDE.md`"
- `oauth/CLAUDE.md` → Files table → `validation.py` ("Live token validation via API calls")
- Supplementary: `ARCHITECTURE.md` line 467 → "OAuth tokens auto-refreshed by dlt at runtime (VAL-004)"

**Expected key points in answer:**
- Navigate to `dango/oauth/` module
- Identify `validation.py` as the file handling token validation (which includes refresh_token exchange)
- Optionally note that dlt auto-refreshes Google tokens at runtime (from ARCHITECTURE.md)
- Optionally mention `providers.py` for provider-specific OAuth implementations

**Known documentation gap:** `oauth/CLAUDE.md` describes `validation.py` as "Live token validation via API calls" but does not use the word "refresh." The `validate_google_token` function performs a refresh_token exchange. A fresh LLM should still navigate to `oauth/` correctly via the routing table, but may not pinpoint the exact refresh mechanism. ARCHITECTURE.md compensates with explicit mention at line 467.

**Scoring:**
| Score | Criteria |
|-------|----------|
| 1.0 | Navigates to `oauth/` module AND identifies `validation.py` or `providers.py` as relevant. Bonus: cites ARCHITECTURE.md on dlt auto-refresh. |
| 0.5 | Navigates to `oauth/` module but cannot identify which specific file handles refresh. |
| 0.0 | Cannot locate the OAuth module or points to wrong module entirely. |

---

### Q3: "What files would you modify to add a new data source?"

**Expected navigation path:**
- `ARCHITECTURE.md` §6.7 "Adding a New Source Type" (line 408)
- Also reachable via: root `CLAUDE.md` routing → `ingestion/CLAUDE.md` → Common Tasks

**Expected key points in answer (from ARCHITECTURE.md §6.7):**
1. `ingestion/sources/registry.py` — add entry to `SOURCE_REGISTRY` dict
2. `oauth/providers.py` — add OAuth provider class (if OAuth source)
3. `ingestion/dlt_sources/` — add or vendor dlt source module
4. `config/models.py` — add source-specific config class (optional)

**Scoring:**
| Score | Criteria |
|-------|----------|
| 1.0 | Lists `registry.py` AND at least 2 other correct files from the 4-step workflow. |
| 0.5 | Lists `registry.py` but missing most other steps, OR lists several correct files but misses `registry.py`. |
| 0.0 | Cannot identify any correct files for adding a data source. |

---

### Q4: "What should you NOT read when fixing a CLI bug?"

**Expected navigation path:**
- Root `CLAUDE.md` → "Don't Read First" section

**Expected key points in answer:**
- `dango/ingestion/dlt_sources/` — vendored third-party connectors (127 files)
- `dango/web/static/` — frontend HTML/CSS/JS assets
- `dango/ingestion/sources/registry.py` — 1440-line metadata registry
- `dango/templates/` — Jinja2 templates
- `tests/` — read source first, then find tests

**Scoring:**
| Score | Criteria |
|-------|----------|
| 1.0 | References the "Don't Read First" section AND lists at least 3 of the 5 entries. |
| 0.5 | References the section but lists fewer than 3 entries, OR lists correct items without citing the section. |
| 0.0 | Cannot identify what to avoid reading, or gives incorrect advice. |

---

### Q5: "How do you test changes to the oauth module?"

**Expected navigation path:**
- Root `CLAUDE.md` routing table → `oauth/CLAUDE.md`
- `oauth/CLAUDE.md` → "Testing" section (line 44)

**Expected key points in answer:**
- Unit tests: `pytest tests/unit/test_oauth_validation.py`
- Manual testing: `dango auth check` (live validation), `dango status` (token health)
- Integration tests: not yet created (will be `tests/integration/test_oauth.py`)
- Common Tasks table also references test commands per task type

**Scoring:**
| Score | Criteria |
|-------|----------|
| 1.0 | Cites the pytest command for oauth tests AND at least one manual test method. |
| 0.5 | Mentions testing but gives only generic advice (e.g., "run pytest") without oauth-specific paths. |
| 0.0 | Cannot locate testing information for the oauth module. |

## Scoring Rubric

| Total Score | Result | Action |
|-------------|--------|--------|
| 5/5 | **PASS** | Proceed to Phase 2 |
| 3 -- 4.5/5 | **PARTIAL** | Fix documentation gaps for failed questions, retest with fresh session |
| < 3/5 | **FAIL** | Major documentation rewrite needed before Phase 2 |

## Pre-Test Simulation

Dry-run trace of each question through existing documentation, from a fresh LLM's perspective.

### Q1 Simulation: "Where would you look to add a new CLI command?"

**Trace:**
1. Root `CLAUDE.md` has a "Quick Routing Table" with entry: "CLI commands → `dango/cli/` → `cli/CLAUDE.md`"
2. `cli/CLAUDE.md` has a "Common Tasks" section with explicit row: "Add a new top-level command → Create in `commands/`, register in `main.py` via `cli.add_command()`"
3. The Repository Structure section in root `CLAUDE.md` also shows `cli/commands/` with all command modules listed

**Verdict:** Strong, unambiguous path. Expected score: 1.0

### Q2 Simulation: "Where is OAuth token refresh handled?"

**Trace:**
1. Root `CLAUDE.md` routing table: "OAuth / token flows → `dango/oauth/` → `oauth/CLAUDE.md`"
2. `oauth/CLAUDE.md` Files table lists `validation.py` as "Live token validation via API calls" with functions including `validate_google_token`, `validate_facebook_token`, `validate_shopify_token`
3. `oauth/CLAUDE.md` does NOT use the word "refresh" anywhere
4. `ARCHITECTURE.md` line 467: "OAuth tokens auto-refreshed by dlt at runtime (VAL-004). Google tokens refresh automatically; Facebook tokens require manual re-auth every 60 days."

**Risk:** A fresh LLM will correctly navigate to `oauth/` via the routing table. It will find `validation.py` and `providers.py` as the relevant files. However, it may not connect "live token validation" with "token refresh" since the word "refresh" is absent from `oauth/CLAUDE.md`. ARCHITECTURE.md compensates if the LLM read it.

**Verdict:** Navigable but not self-evident. The routing table gets you to the right module. ARCHITECTURE.md provides the refresh-specific context. A well-prepared LLM (one that read ARCHITECTURE.md as instructed) should score 1.0. One that only skimmed ARCHITECTURE.md might score 0.5. Expected score: 0.5 -- 1.0

**Mitigation (if Q2 scores < 1.0 in real test):** Add "token refresh" to the `validation.py` description in `oauth/CLAUDE.md`. Change "Live token validation via API calls" to "Live token validation and refresh-token exchange via API calls."

### Q3 Simulation: "What files would you modify to add a new data source?"

**Trace:**
1. `ARCHITECTURE.md` §6.7 "Adding a New Source Type" provides a clear 4-step numbered list
2. Root `CLAUDE.md` routing table → `ingestion/CLAUDE.md` → Common Tasks: "Add a new source type → `sources/registry.py`"
3. Multiple documentation paths converge on the same answer

**Verdict:** Strong, explicit documentation. Two independent paths lead to the answer. Expected score: 1.0

### Q4 Simulation: "What should you NOT read when fixing a CLI bug?"

**Trace:**
1. Root `CLAUDE.md` has a dedicated "Don't Read First" section with a clear table
2. Five entries with explanations: `dlt_sources/`, `web/static/`, `tests/`, `registry.py`, `templates/`
3. Section title makes it immediately findable

**Verdict:** Strongest question — dedicated section with exact title match. Expected score: 1.0

### Q5 Simulation: "How do you test changes to the oauth module?"

**Trace:**
1. Root `CLAUDE.md` routing table → `oauth/CLAUDE.md`
2. `oauth/CLAUDE.md` has a "Testing" section (line 44) with three bullet points:
   - Unit: `pytest tests/unit/test_oauth_validation.py` (26 tests)
   - Integration: None yet
   - Manual: `dango auth check`, `dango status`
3. Common Tasks table also lists test commands per task type

**Note:** The test file `tests/unit/test_oauth_validation.py` is referenced in docs but does not exist yet (will be created by TEST-001). This does not affect navigation scoring — the LLM is being tested on whether it can find the right documentation path, not whether the referenced files exist.

**Verdict:** Clear, dedicated Testing section. Expected score: 1.0

### Overall Simulation Result

| Q# | Expected Score | Confidence |
|----|---------------|------------|
| Q1 | 1.0 | High |
| Q2 | 0.5 -- 1.0 | Medium (depends on ARCHITECTURE.md reading) |
| Q3 | 1.0 | High |
| Q4 | 1.0 | High |
| Q5 | 1.0 | High |

**Predicted total:** 4.5 -- 5.0/5 (PASS or near-PASS)

**Only risk:** Q2 depends on the LLM connecting "validation" with "refresh" and/or having read ARCHITECTURE.md §9. Mitigation is ready if needed.

## Results Log

| Attempt | Date | LLM Model | Q1 | Q2 | Q3 | Q4 | Q5 | Total | Result | Gaps Found | Fixes Applied |
|---------|------|-----------|----|----|----|----|----|----|--------|------------|---------------|
| 1 | | | | | | | | | | | |
| 2 | | | | | | | | | | | |
| 3 | | | | | | | | | | | |
