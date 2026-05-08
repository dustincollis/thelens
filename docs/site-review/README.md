# Site Review

Local-first website audit tool that runs technical, structural, and multi-LLM evaluations against a single URL and produces an HTML report and a PowerPoint deck.

## What it does

For any URL you give it, the tool:

- Fetches the page two ways (raw HTML and JS-rendered DOM) and screenshots it.
- Runs a structural and technical audit (Lighthouse-style metrics, schema, AI crawler access).
- Asks Claude, GPT, Gemini, and Grok the same standard questions about the page.
- Asks the same four models category-level questions WITHOUT showing them the page, to test brand visibility.
- Generates 3 to 5 review personas based on what the site actually is, then has each persona review it.
- Synthesizes all of the above into convergence findings, divergence findings, and prioritized recommendations.
- Renders an HTML report and a PowerPoint deck.

## Quick start

Prerequisites: Python 3.12 and [uv](https://github.com/astral-sh/uv) installed.

```bash
git clone <repo>
cd site-review
uv sync
uv run playwright install chromium
cp .env.example .env
# edit .env and add API keys
uv run site-review run https://example.com
```

The run completes in roughly 2 to 5 minutes for a typical marketing site. The HTML report opens automatically. The PPTX is in `runs/<run_id>/report.pptx`.

## Documentation

- `SPEC.md` is the master specification. Read this first if you are working on the codebase.
- `CLAUDE.md` (or `AGENTS.md`) is the conventions file for AI coding agents.
- `prompts/` contains all LLM prompts as markdown files.
- `config/` contains tunable configuration.

## CLI

```bash
site-review run <url>          # run the full pipeline
site-review run <url> --resume # re-use existing artifacts where possible
site-review list               # show recent runs
site-review open <run_id>      # open the HTML report
site-review estimate <url>     # print projected cost without running
site-review reindex            # rebuild the SQLite index from the runs folder
site-review dashboard          # launch Streamlit dashboard
```

## Two-machine setup

Put the project root inside iCloud Drive, Dropbox, or OneDrive. The `runs/` folder, `data/runs.db`, and code all sync. API keys in `.env` are per-machine (just copy `.env` once when setting up the second machine).

## License

Personal use.
