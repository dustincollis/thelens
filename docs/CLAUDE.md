# CLAUDE.md / AGENTS.md

This file is read by Claude Code and Codex on every session. It is shorter than `SPEC.md` and contains conventions, gotchas, and pointers. The full specification lives in `SPEC.md`. When something is decided in `SPEC.md`, it takes precedence over what you might assume.

If you are using Codex, symlink or copy this file to `AGENTS.md`.

## How to use this repo

1. Read `SPEC.md` cover to cover before writing any code.
2. Build in the phases defined in `SPEC.md` section 14. Do not skip phases.
3. Each phase has acceptance criteria. Verify them before moving to the next phase.
4. If you encounter a decision not covered by `SPEC.md`, pause and ask the user. Do not improvise architectural decisions.

## Project conventions

- Python 3.12 only. Pinned in `.python-version`.
- Use `uv` for everything. Never use `pip` or `poetry`. Always run Python through `uv run python` and tests through `uv run pytest`.
- All inter-step data uses Pydantic v2 models. No untyped dicts crossing function boundaries.
- All file I/O is UTF-8. Never assume system default encoding.
- All datetimes are timezone-aware UTC. Use `datetime.now(timezone.utc)`.
- All paths are `pathlib.Path`. No string concatenation for paths.
- All LLM responses MUST be JSON. Use the provider-native JSON mode where available. Validate with the matching Pydantic model immediately on receipt.

## Style

- Functions over classes where possible. Classes only when state is genuinely needed (LLM clients, storage).
- Type hints on all function signatures, including return types.
- Docstrings on public functions only. Private helpers get a one-line comment if non-obvious.
- Error messages are user-facing and actionable. "Failed to fetch URL" is bad; "Failed to fetch https://example.com: connection refused after 30s" is good.
- No print statements. Use `rich.console.Console` for terminal output and Python's `logging` module for diagnostic logs.

## Prompt engineering

- Prompts live in `prompts/*.md` as markdown files. Do not inline prompts in Python.
- Each prompt file has a YAML frontmatter block with `name`, `description`, `input_schema`, and `output_schema` references, followed by the prompt body in markdown.
- Load prompts via a single helper in `src/thelens/llm/base.py` that reads the file, splits the frontmatter, and returns a structured object.
- Never modify prompts in code. If a prompt needs adjustment, update the markdown file.

## LLM client pattern

The base interface in `llm/base.py` defines:

```python
class LLMClient(Protocol):
    provider_name: str
    model: str

    async def complete(
        self,
        system: str,
        user: str,
        response_format: type[BaseModel],
        max_tokens: int = 2048,
        temperature: float = 0.3,
        disable_web_search: bool = False,
    ) -> tuple[BaseModel, UsageInfo]:
        ...
```

Each provider implements this. The function returns the parsed Pydantic model and a `UsageInfo` object containing token counts and cost.

Web search disable behavior per provider:

- **Anthropic**: do not include the `web_search` tool. Default behavior is no search.
- **OpenAI**: use the `tool_choice` parameter to forbid tool use, or use the standard chat completions endpoint without tools.
- **Gemini**: do not enable `google_search_retrieval`. Default behavior is no search.
- **xAI / Grok**: Grok's behavior is documented in the report rather than suppressed. Do not attempt to force-disable; instead, log the model version used and note in the report metadata.

## Async orchestration

- The pipeline orchestrator (`pipeline/run.py`) is an `async def` function.
- Use `asyncio.gather` for parallel LLM calls. Always pass `return_exceptions=True` and handle each exception per provider rather than failing the whole batch.
- A failed provider call writes a JSON file with `{"status": "failed", "error": "..."}` so the report can show partial results gracefully.
- Use one `asyncio.Semaphore` **per provider** (default 4 concurrent per provider, configurable in `models.yaml`). This lets the four providers run truly in parallel without a slow provider blocking the others, while keeping each one within its own rate limit.
- Wrap every LLM call in a retry helper with exponential backoff that handles 429 and transient 5xx responses. Three attempts max; a final failure writes the `{"status": "failed", ...}` JSON file described above.

## Storage rules

- Run folder names are immutable once created. Format: `YYYY-MM-DD_<sanitized-domain>_<6-char-hex>`.
- Sanitized domain = `urlparse(url).netloc.replace(".", "-").replace(":", "-")`.
- The 6-char hex is `secrets.token_hex(3)` at run creation, NOT a hash of the URL. Two runs of the same URL get different IDs.
- The SQLite table `runs` is rebuildable from filesystem at any time. Never store data in SQLite that does not exist in a run folder.

## Testing

- `pytest` only. No `unittest` boilerplate.
- Tests for `audit.py` use HTML fixtures in `tests/fixtures/`. Do not test against live URLs in the test suite.
- Tests for LLM clients are integration tests gated behind a `@pytest.mark.integration` marker. They are NOT run in the default `pytest` invocation.
- Run the full suite with `uv run pytest`. Run integration tests with `uv run pytest -m integration`.

## Things to avoid

- Do not add new dependencies without updating `SPEC.md` section 4.
- Do not invent new pipeline steps. The ten steps in `SPEC.md` section 3 are the entire pipeline.
- Do not add a database ORM. SQLite + raw SQL is the choice.
- Do not refactor the prompts to live in code. They stay in markdown.
- Do not generate the master deck programmatically. It is hand-built once.
- Do not add a web framework. Streamlit is the only UI layer.

## When in doubt

Re-read the relevant section of `SPEC.md`. If still unclear, ask the user. Improvising in this codebase is more expensive than asking a clarifying question.
