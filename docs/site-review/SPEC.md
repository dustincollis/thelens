# Site Review: Specification

## 1. Purpose

A local-first command-line tool that audits a website from multiple perspectives and produces both an on-screen HTML report and a PowerPoint deck. The novel capabilities are:

- Multi-LLM evaluation: same questions run in parallel against Claude, GPT, Gemini, and Grok, with comparison views surfacing where the models agree and diverge.
- Two query modes per provider: page-aware (model sees the page content) and page-blind (model answers category-level questions about the brand without seeing the page).
- Dynamically generated review personas based on what the site actually is, rather than a fixed persona list.
- A structured technical and LLM-readiness audit that runs alongside the AI evaluations.

The user is the sole operator. The tool runs on a personal laptop and a work laptop, with project files synced via cloud folder.

## 2. Scope and non-goals

### In scope

- Single-URL audit (homepage or any specific page)
- Multi-provider LLM evaluation with deterministic, configurable question sets
- Dynamic persona generation and per-persona review
- Technical and structural audit (Lighthouse-style metrics, schema, render-mode comparison)
- HTML report and PPTX deck per run
- Local SQLite index of all runs for history and diff
- Optional Streamlit dashboard for viewing run history

### Explicitly out of scope

- Multi-user functionality, authentication, or sharing infrastructure
- Cloud hosting, serverless deployment, or any always-on service
- Crawling multiple pages of a site (Phase 2 candidate, not v1)
- Re-running on a schedule (Phase 2 candidate, not v1)
- Exporting to PDF (the HTML report is the human-readable artifact; PPTX is the deck deliverable)
- API access to the tool (it is a CLI for one user)
- Anything that requires a server process beyond Streamlit

## 3. High-level architecture

The tool is a sequential pipeline that produces a structured data object, plus two renderers that turn that object into an HTML file and a PPTX file. Every step writes its output to disk, so the pipeline is naturally checkpointed.

The pipeline:

1. **Fetch**: get raw HTML and a JS-rendered DOM of the URL, plus a screenshot.
2. **Audit**: run technical and structural checks on the fetched content. No AI calls.
3. **Classify** (Layer 1): one LLM call produces the site fingerprint (category, audience, goal, register).
4. **Generate personas** (Layer 2): one LLM call produces 3 to 5 review personas based on the fingerprint.
5. **Multi-LLM page-aware** (Layer 3a): the standard question set runs in parallel against all configured providers. The page text is in the prompt.
6. **Multi-LLM page-blind** (Layer 3b): one LLM call generates 4 to 6 category-level queries based on the fingerprint, then those queries run in parallel against all configured providers. The page is NOT in the prompt; web search is disabled where the API supports it.
7. **Persona reviews** (Layer 4): each generated persona reviews the page using a single configured model (default: Claude Opus).
8. **Synthesize** (Layer 5): one LLM call takes all prior outputs and produces convergence findings, divergence findings, and prioritized recommendations.
9. **Render HTML**: Jinja2 template fills with the structured data object.
10. **Render PPTX**: python-pptx walks the layout schema and produces the deck.

A `--resume` flag re-uses any existing artifacts in the run folder and only re-runs missing or failed steps. A `--force` flag skips the cache.

## 4. Tech stack

Lock these decisions. Do not substitute without updating this spec.

- **Python 3.12** (pinned in `.python-version`)
- **uv** for dependency management (not pip, not poetry)
- **Playwright** for JS-rendered DOM and screenshots (Chromium only)
- **httpx** for raw HTTP fetches and JSON-LD validation
- **BeautifulSoup4 + lxml** for HTML parsing
- **Pydantic v2** for all data models (every step's output is a Pydantic model serialized to JSON)
- **Jinja2** for the HTML report template
- **python-pptx** for the deck renderer
- **Streamlit** for the optional dashboard (Phase 2)
- **Typer** for the CLI
- **Rich** for terminal output
- **SQLite** via Python stdlib (no ORM; raw SQL is fine at this scale)
- **anthropic**, **openai**, **google-generativeai**, **xai-sdk** as the four LLM SDKs

The project does NOT use: LangChain, LlamaIndex, LiteLLM, FastAPI, Flask, Django, Celery, Redis, or any cloud SDK.

## 5. Folder structure

```
site-review/
├── .python-version
├── pyproject.toml
├── uv.lock
├── .env                      # gitignored
├── .env.example
├── .gitignore
├── README.md
├── SPEC.md                   # this file
├── CLAUDE.md                 # agent guidance
├── config/
│   ├── models.yaml
│   ├── questions.yaml
│   └── layout_schema.json
├── prompts/
│   ├── 01_classification.md
│   ├── 02_persona_generation.md
│   ├── 03_page_blind_query_generation.md
│   ├── 04_persona_review.md
│   ├── 05_synthesis.md
│   └── 06_verification.md
├── templates/
│   ├── master_deck.pptx      # hand-built; generated separately
│   └── report.html.j2        # Jinja2 template for HTML report
├── data/
│   └── runs.db               # SQLite, gitignored
├── runs/                     # gitignored, all per-run artifacts
│   └── <run_id>/
│       ├── manifest.json
│       ├── raw_html.html
│       ├── rendered_dom.html
│       ├── screenshot_full.png
│       ├── screenshot_viewport.png
│       ├── technical_audit.json
│       ├── classification.json
│       ├── personas.json
│       ├── llm/
│       │   ├── claude_page_aware.json
│       │   ├── gpt_page_aware.json
│       │   ├── gemini_page_aware.json
│       │   ├── grok_page_aware.json
│       │   ├── claude_page_blind.json
│       │   ├── gpt_page_blind.json
│       │   ├── gemini_page_blind.json
│       │   └── grok_page_blind.json
│       ├── persona_reviews/
│       │   ├── persona_1.json
│       │   └── ...
│       ├── synthesis.json
│       ├── report.html
│       └── report.pptx
├── src/
│   └── site_review/
│       ├── __init__.py
│       ├── cli.py            # Typer entry point
│       ├── app.py            # Streamlit dashboard (Phase 2)
│       ├── config.py         # config loader
│       ├── models.py         # Pydantic data models
│       ├── storage.py        # SQLite + filesystem helpers
│       ├── pipeline/
│       │   ├── __init__.py
│       │   ├── run.py        # orchestrator
│       │   ├── fetch.py
│       │   ├── audit.py
│       │   ├── classify.py
│       │   ├── personas.py
│       │   ├── multi_llm.py
│       │   ├── persona_review.py
│       │   └── synthesize.py
│       ├── llm/
│       │   ├── __init__.py
│       │   ├── base.py       # provider-agnostic interface
│       │   ├── anthropic_client.py
│       │   ├── openai_client.py
│       │   ├── gemini_client.py
│       │   └── xai_client.py
│       └── render/
│           ├── __init__.py
│           ├── html.py
│           └── pptx.py
└── tests/
    ├── conftest.py
    ├── test_audit.py
    ├── test_models.py
    ├── test_storage.py
    └── fixtures/
```

## 6. Pipeline layers in detail

### Layer 0: Fetch

Two fetches per run:

- **Raw HTML fetch** via httpx, with a desktop user agent. Follows redirects. Saves to `raw_html.html`.
- **Rendered DOM fetch** via Playwright Chromium. Waits for `networkidle`, then waits an additional 2 seconds. Captures full-page screenshot and viewport screenshot. Saves the full DOM to `rendered_dom.html`.

The text content of both fetches is extracted (BeautifulSoup, removing script/style/nav/footer) and held in memory for downstream steps. The text from the rendered DOM is the canonical "page content" used in LLM prompts unless otherwise noted.

### Layer 1: Technical and structural audit

A pure-Python pass that produces `technical_audit.json`. No AI calls. Output structure:

```json
{
  "url": "https://example.com",
  "fetched_at": "2026-05-08T14:30:00Z",
  "render_mode_diff": {
    "raw_text_chars": 4521,
    "rendered_text_chars": 12830,
    "js_trapped_pct": 64.7
  },
  "html_structure": {
    "h1_count": 1,
    "heading_hierarchy_violations": 0,
    "semantic_tag_usage": {
      "article": 0, "section": 4, "nav": 1, "main": 1,
      "header": 1, "footer": 1, "aside": 0
    },
    "dom_to_content_ratio": 8.4,
    "image_count": 12,
    "images_missing_alt": 3,
    "alt_text_coverage_pct": 75.0,
    "low_quality_link_text_count": 2
  },
  "structured_data": {
    "json_ld_blocks": 2,
    "json_ld_types": ["Organization", "WebSite"],
    "json_ld_valid": true,
    "open_graph": {
      "og:title": true, "og:description": true,
      "og:image": true, "og:type": false
    },
    "twitter_card": false,
    "missing_recommended_schemas": ["BreadcrumbList"]
  },
  "ai_crawler_access": {
    "robots_txt_present": true,
    "GPTBot": "allowed",
    "ClaudeBot": "allowed",
    "anthropic-ai": "allowed",
    "Google-Extended": "disallowed",
    "PerplexityBot": "allowed",
    "CCBot": "allowed",
    "Bytespider": "disallowed",
    "Applebot-Extended": "allowed"
  },
  "llms_txt": {
    "present": false,
    "valid_markdown": null,
    "size_bytes": null
  },
  "trust_signals": {
    "https": true,
    "contact_info_present": true,
    "privacy_policy_link": true,
    "author_byline": false,
    "last_updated_date": false
  },
  "page_size": {
    "html_bytes": 84210,
    "total_bytes_estimate": 1842000
  }
}
```

### Layer 2: Site classification (Layer 1 in the user-facing report numbering, but pipeline-internal "Layer 2")

Note on numbering: the report shows "Layer 1: Site fingerprint" through "Layer 5: Synthesis." Internally, the audit pass is Layer 0. The numbering in the report and in the file `01_classification.md` matches the user-facing layers.

Classification is a single LLM call using `prompts/01_classification.md`. Output is `classification.json` matching the `Classification` Pydantic model. Default model: Claude Opus.

### Layer 3: Persona generation

Single LLM call using `prompts/02_persona_generation.md`, with the classification object as input. Generates 3 to 5 personas. Output is `personas.json`.

### Layer 4: Multi-LLM evaluation

Two sub-steps run in parallel batches.

**Page-aware (4a):** for each provider in `models.yaml`, send a single prompt containing the page text and the full question set from `questions.yaml`. The model returns a structured JSON object with one field per question. Save to `llm/<provider>_page_aware.json`.

**Page-blind (4b):** first, one LLM call using `prompts/03_page_blind_query_generation.md` produces 4 to 6 category-level queries based on the classification. Then, for each provider in `models.yaml`, each query runs as a separate API call (NOT batched into one prompt, because the goal is to elicit the model's natural answer to each query). Web search must be disabled where the API supports it (Claude, OpenAI, Gemini). Grok's behavior is documented as a caveat in the report. Save to `llm/<provider>_page_blind.json`.

A separate verification pass (using `prompts/06_verification.md`) checks each page-aware response for hallucinations against the actual page content. The output of this pass is appended to each provider's page-aware JSON file under a `hallucination_flags` field.

### Layer 5: Persona reviews

For each persona in `personas.json`, one LLM call using `prompts/04_persona_review.md`. Default reviewer model: Claude Opus. Output saved as `persona_reviews/persona_<n>.json` per persona.

### Layer 6: Synthesis

Single LLM call using `prompts/05_synthesis.md`. Inputs: technical audit, classification, personas, all multi-LLM responses, all persona reviews. Output: `synthesis.json` containing convergence findings, divergence findings, and prioritized recommendations.

## 7. Data model

All inter-step communication is via Pydantic models serialized to JSON. The contract is the JSON schema, and prompts must produce JSON matching the schemas defined in `src/site_review/models.py`.

Top-level Pydantic models the AI agent must define (one model per file output):

- `TechnicalAudit` (Layer 0)
- `Classification` (Layer 1)
- `Persona`, `PersonaSet` (Layer 2)
- `PageAwareResponse`, `PageBlindResponse` (Layer 3, per provider)
- `PersonaReview` (Layer 4, per persona)
- `Synthesis` (Layer 5)
- `RunManifest` (top-level run metadata)

Each model has a corresponding JSON schema. The agent should implement these as Pydantic v2 models and use `model.model_dump_json(indent=2)` for file writes and `model.model_validate_json(...)` for reads.

The `RunManifest` is the index file:

```json
{
  "run_id": "2026-05-08_examplecom_a3b9f1",
  "url": "https://example.com",
  "started_at": "2026-05-08T14:30:00Z",
  "completed_at": "2026-05-08T14:34:12Z",
  "status": "complete",
  "providers_used": ["claude", "gpt", "gemini", "grok"],
  "personas_generated": 4,
  "estimated_cost_usd": 0.42,
  "actual_cost_usd": 0.39,
  "composite_score": 73,
  "step_status": {
    "fetch": "complete",
    "audit": "complete",
    "classify": "complete",
    "personas": "complete",
    "page_aware_claude": "complete",
    "page_aware_gpt": "complete",
    "page_aware_gemini": "complete",
    "page_aware_grok": "complete",
    "page_blind_claude": "complete",
    "page_blind_gpt": "complete",
    "page_blind_gemini": "complete",
    "page_blind_grok": "complete",
    "persona_reviews": "complete",
    "synthesis": "complete",
    "html_render": "complete",
    "pptx_render": "complete"
  }
}
```

## 8. Storage

Two storage locations, both inside the project root.

**SQLite (`data/runs.db`):** a single table `runs` mirroring the `RunManifest` flat fields, used for history queries and the dashboard. Created on first run via a schema in `storage.py`. No migrations framework; if the schema changes, drop and recreate.

**Filesystem (`runs/<run_id>/`):** every per-run artifact. The run_id format is `YYYY-MM-DD_<sanitized-domain>_<6-char-hex>`. The folder is fully self-contained: zipping it produces a portable artifact.

The two storage locations are not strictly synchronized. The filesystem is the source of truth; SQLite is a convenience index that can be rebuilt from the filesystem with a `site-review reindex` command.

## 9. CLI

Implemented in `cli.py` using Typer. Commands:

- `site-review run <url>` runs the full pipeline. Flags: `--resume`, `--force`, `--providers claude,gpt`, `--no-personas`, `--no-page-blind`, `--budget 1.00`.
- `site-review list` shows recent runs from SQLite, most recent first.
- `site-review open <run_id>` opens the HTML report in the default browser. Accepts partial run_id.
- `site-review reindex` rebuilds the SQLite index from the runs folder.
- `site-review estimate <url>` prints projected token and dollar cost without running anything.
- `site-review dashboard` launches the Streamlit dashboard on localhost.

Every long-running command shows a Rich progress display with one line per pipeline step.

## 10. HTML report

Single self-contained HTML file produced by Jinja2 from `templates/report.html.j2`. Inline CSS, no external assets except the screenshot which is referenced as a relative path next to the HTML file in the run folder.

Sections, in order:

1. Header with URL, run date, composite score
2. Executive summary (top 5 findings)
3. Site fingerprint
4. LLM-readiness scorecard (5 sub-scores)
5. Technical audit
6. Identity clarity (3-column compare)
7. Multi-LLM page-aware results (matrix)
8. Multi-LLM page-blind brand visibility
9. Persona reviews
10. Convergence findings
11. Divergence findings
12. Prioritized recommendations
13. Methodology

The HTML report is the comprehensive view. The PPTX is the curated executive cut.

## 11. PPTX report

Generated by `render/pptx.py` using python-pptx. The renderer follows these rules without exception:

- Reads `templates/master_deck.pptx` for slide layouts. Never creates new layouts at runtime.
- Reads `config/layout_schema.json` to map data sections to layouts.
- Produces between 15 and 20 slides per deck based on persona count.
- 16:9 aspect ratio only.
- Score band colors and severity colors come from `layout_schema.json` and are global.
- Footer on every slide: site URL on left, run date on right, slide number bottom-right.

The master deck is a hand-built `.pptx` file containing exactly 11 slide layouts named in `layout_schema.json`. The renderer matches by layout name, so renaming a layout in the master deck breaks the build until the schema is updated.

## 12. Streamlit dashboard

Phase 2 deliverable. A single-file `app.py` that reads the SQLite index and renders a list of runs with links to the HTML reports. Includes a "diff two runs" view that compares scores and finding counts side by side. Run with `streamlit run src/site_review/app.py` or via `site-review dashboard`.

## 13. Cost controls and caching

The `models.yaml` file sets a per-run hard budget. The `estimate` command and the pre-run estimate inside `run` use a token-counting heuristic per provider plus published per-million-token prices.

If projected cost exceeds the configured budget, the run aborts and prompts for `--budget <higher>` or `--providers <subset>` to proceed.

Per-step caching: each step checks for its expected output file before running. The `--resume` flag honors this cache. The `--force` flag deletes the existing run folder and starts clean.

A `--cache-fetch <hours>` flag allows reusing the raw HTML and rendered DOM from a previous run on the same URL within the time window, useful when iterating on prompts.

## 14. Two-machine sync

The intended workflow:

- The project root lives inside an iCloud Drive, Dropbox, or OneDrive folder.
- Code, config, prompts, and the master deck are also in git, with the same cloud folder being a git working copy. Either sync mechanism alone works; both together gives belt-and-suspenders.
- `.env` (API keys) is per-machine, not synced via git, but is fine to sync via the cloud folder since it stays inside the same trust boundary as the rest of the personal data.
- `data/runs.db` is a single file and syncs cleanly. Concurrent runs on both machines could create conflicts; treat this as a known limitation rather than designing around it.
- `runs/` syncs as plain directories. Each run folder is self-contained and survives partial syncs.

If the cloud folder produces a conflict file (e.g., `runs.db (Conflicted Copy).db`), the resolution is to run `site-review reindex` after resolving the file conflict manually.

## 15. Build phases

The agent should build in this order. Each phase ends with an explicit acceptance test that must pass before moving on.

### Phase 0: Environment setup

- Initialize repo with `uv init`
- Create the folder structure from section 5
- Create `pyproject.toml` with all dependencies pinned
- Create `.env.example` and `.gitignore`
- Run `uv sync` and `playwright install chromium`
- Verify each LLM SDK can be imported

**Acceptance:** `uv run python -c "import anthropic, openai, google.generativeai, xai_sdk, playwright, pptx, jinja2, typer, rich, pydantic; print('ok')"` prints `ok`.

### Phase 1: Fetch and audit

- Implement `pipeline/fetch.py` (raw HTML, rendered DOM, screenshots)
- Implement `pipeline/audit.py` (all checks in section 6, Layer 0)
- Implement `models.py` for `TechnicalAudit` and `RunManifest`
- Implement `storage.py` for the runs folder and SQLite index
- Implement `cli.py` with the `run` command supporting only fetch+audit so far
- Implement `cli.py` with `list` and `reindex`

**Acceptance:** `site-review run https://anthropic.com` produces a complete run folder with `raw_html.html`, `rendered_dom.html`, both screenshots, `technical_audit.json`, and `manifest.json`. `site-review list` shows the run.

### Phase 2: Classification and personas

- Implement the `LLMClient` base class in `llm/base.py`
- Implement `llm/anthropic_client.py` only (other providers come in Phase 3)
- Implement `pipeline/classify.py` and `pipeline/personas.py`
- Wire both into `pipeline/run.py`
- Add the `Classification`, `Persona`, `PersonaSet` Pydantic models

**Acceptance:** A run produces valid `classification.json` and `personas.json` matching the schemas. The personas explicitly reflect the site's category (manual sanity check).

### Phase 3: Multi-LLM evaluation

- Implement the remaining three LLM clients
- Implement `pipeline/multi_llm.py` with both page-aware and page-blind sub-steps
- Implement the verification pass
- Add `PageAwareResponse` and `PageBlindResponse` Pydantic models

**Acceptance:** A run produces eight LLM JSON files (4 providers x 2 modes). Each page-aware file has answers to all configured questions plus hallucination flags. Each page-blind file has the same structure across providers.

### Phase 4: Persona reviews and synthesis

- Implement `pipeline/persona_review.py`
- Implement `pipeline/synthesize.py`
- Add `PersonaReview` and `Synthesis` Pydantic models

**Acceptance:** A run produces one persona review JSON per persona, plus `synthesis.json` with non-empty convergence, divergence, and recommendation lists.

### Phase 5: HTML report

- Build `templates/report.html.j2` covering all 13 sections from section 10
- Implement `render/html.py`
- Wire into `pipeline/run.py`
- Implement `cli.py` `open` command

**Acceptance:** `site-review open <run_id>` opens a complete, self-contained HTML report in the default browser. The report renders correctly with no missing data sections.

### Phase 6: PPTX report

- Build `templates/master_deck.pptx` by hand with 11 named layouts (note: this is a manual step the user does in PowerPoint or Keynote; the agent does not generate the master deck)
- Verify `config/layout_schema.json` matches the master deck layout names
- Implement `render/pptx.py`
- Wire into `pipeline/run.py`

**Acceptance:** A run produces a `report.pptx` between 15 and 20 slides, opens cleanly in PowerPoint and Keynote, and has consistent formatting across all slides.

### Phase 7: Polish and dashboard

- Cost estimation in `cli.py estimate`
- Budget enforcement in `cli.py run`
- `--cache-fetch` flag
- Streamlit dashboard `app.py`
- Tests in `tests/` for `audit.py`, `models.py`, `storage.py`

**Acceptance:** `site-review estimate <url>` prints a projected cost. `site-review dashboard` launches and lists all runs. Test suite passes.

## 16. Acceptance criteria for v1

The tool is "v1 done" when:

- A fresh checkout on a new machine reaches a working state in under 10 minutes
- A run on a typical marketing site completes end-to-end in under 5 minutes
- A run produces a valid HTML report and a valid PPTX deck
- Re-running with `--resume` after a mid-pipeline failure picks up where it left off
- The run folder is self-contained and can be moved or zipped without breakage
- The dashboard lists all runs and opens reports correctly

## 17. Things explicitly not decided in this spec

These are deferred and the agent should NOT improvise solutions:

- The exact visual design of slides (the master deck is hand-built later)
- The exact wording of the executive summary headlines (the synthesis prompt produces them; tuning happens after Phase 4)
- The composite scoring weights (the synthesis prompt assigns the composite score; tuning happens after a few real runs)
- Whether to add Perplexity as a fifth provider (not v1; revisit after seeing how the four-provider matrix reads)

If the agent encounters a decision point not covered by this spec, it should pause and ask rather than improvise.
