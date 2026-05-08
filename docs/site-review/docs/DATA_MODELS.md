# Data Model Reference

This document defines every Pydantic model that crosses a pipeline step boundary or is written to disk. The agent should implement these models in `src/site_review/models.py` exactly as specified. JSON schemas in prompts must produce output that validates against the corresponding Pydantic model.

All models inherit from `pydantic.BaseModel` with `model_config = ConfigDict(extra="forbid")` to catch schema drift early.

## RunManifest

Top-level metadata for a run. One per run folder, written and updated as the pipeline progresses. Also denormalized into the SQLite `runs` table.

Fields:

- `run_id: str` (format: `YYYY-MM-DD_<sanitized-domain>_<6-char-hex>`)
- `url: HttpUrl`
- `started_at: datetime` (UTC)
- `completed_at: datetime | None` (UTC, set when status becomes complete or failed)
- `status: Literal["running", "complete", "failed"]`
- `providers_used: list[str]`
- `personas_generated: int`
- `estimated_cost_usd: float`
- `actual_cost_usd: float`
- `composite_score: int | None` (0-100, set after synthesis)
- `step_status: dict[str, Literal["pending", "running", "complete", "failed", "cached"]]`
- `step_durations_ms: dict[str, int]`
- `errors: list[StepError]` (empty when no failures)
- `pipeline_version: str` (semver, currently "1.0.0")

## TechnicalAudit

Layer 0 output. No AI calls; pure inspection of fetched content.

Fields:

- `url: HttpUrl`
- `fetched_at: datetime`
- `render_mode_diff: RenderModeDiff`
- `html_structure: HtmlStructure`
- `structured_data: StructuredData`
- `ai_crawler_access: AiCrawlerAccess`
- `llms_txt: LlmsTxtCheck`
- `trust_signals: TrustSignals`
- `page_size: PageSize`

### Sub-models

`RenderModeDiff`:
- `raw_text_chars: int`
- `rendered_text_chars: int`
- `js_trapped_pct: float` (computed as `(rendered - raw) / rendered * 100`, clamped to 0-100)

`HtmlStructure`:
- `h1_count: int`
- `heading_hierarchy_violations: int`
- `semantic_tag_usage: dict[str, int]` (keys: article, section, nav, main, header, footer, aside)
- `dom_to_content_ratio: float`
- `image_count: int`
- `images_missing_alt: int`
- `alt_text_coverage_pct: float`
- `low_quality_link_text_count: int` (links with text matching "click here", "read more", "here", or bare URL)

`StructuredData`:
- `json_ld_blocks: int`
- `json_ld_types: list[str]`
- `json_ld_valid: bool`
- `open_graph: dict[str, bool]` (keys: og:title, og:description, og:image, og:type, og:url, og:site_name)
- `twitter_card: bool`
- `missing_recommended_schemas: list[str]` (based on classification.category, computed in audit step)

`AiCrawlerAccess`:
- `robots_txt_present: bool`
- `crawler_rules: dict[str, Literal["allowed", "disallowed", "unspecified"]]` (keys: GPTBot, ClaudeBot, anthropic-ai, Google-Extended, PerplexityBot, CCBot, Bytespider, Applebot-Extended)

`LlmsTxtCheck`:
- `present: bool`
- `valid_markdown: bool | None` (None if not present)
- `size_bytes: int | None`

`TrustSignals`:
- `https: bool`
- `contact_info_present: bool`
- `privacy_policy_link: bool`
- `terms_of_service_link: bool`
- `author_byline: bool`
- `last_updated_date: bool`

`PageSize`:
- `html_bytes: int`
- `total_bytes_estimate: int` (HTML + linked CSS/JS/images, estimated)

## Classification

Layer 1 output. Schema matches the JSON shape defined in `prompts/01_classification.md` exactly.

Fields:

- `url: HttpUrl`
- `category: ClassificationCategory` (enum)
- `category_specifics: str`
- `audience_summary: str`
- `audience_segments: list[str]` (length 2-4)
- `evident_goal: EvidentGoal` (enum)
- `evident_goal_explanation: str`
- `content_maturity: ContentMaturity`
- `brand_register: BrandRegister` (enum)
- `industry: str`
- `geography: str | None`
- `competitor_examples: list[str]` (length 0-4)
- `confidence: Literal["high", "medium", "low"]`

### Enums

`ClassificationCategory`: ecommerce, b2b_saas, b2c_saas, publisher, news, nonprofit, government, healthcare, education, financial_services, professional_services, agency, portfolio, community, documentation, marketing_landing, ecommerce_brand, marketplace, other

`EvidentGoal`: lead_generation, direct_sale, signup_or_trial, education, brand_awareness, retention, fundraising, recruitment, support, other

`BrandRegister`: formal, technical, authoritative, casual, conversational, transactional, journalistic, academic

`ContentMaturity`: dataclass with boolean fields has_blog, has_documentation, has_pricing, has_case_studies, has_about_page, has_team_page

## PersonaSet and Persona

Layer 2 output.

`PersonaSet`:
- `personas: list[Persona]` (length 3-5)
- `generation_notes: str`

`Persona`:
- `name: str`
- `role: str`
- `context: str`
- `goal: str`
- `expertise_level: Literal["novice", "intermediate", "expert"]`
- `decision_authority: Literal["researcher", "influencer", "decision_maker", "end_user"]`
- `primary_concerns: list[str]` (length 3-5)
- `trust_posture: Literal["skeptical", "neutral", "trusting", "urgent"]`
- `is_llm_lens: bool`
- `rationale: str`

Validator: exactly one persona in the set must have `is_llm_lens: true`.

## PageAwareResponse and PageBlindResponse

Layer 3 output. One per provider per query mode.

`PageAwareResponse`:
- `provider: str` (anthropic, openai, gemini, xai)
- `model: str`
- `requested_at: datetime`
- `response_received_at: datetime`
- `answers: PageAwareAnswers`
- `usage: UsageInfo`
- `hallucination_flags: VerificationResult | None` (populated by verification pass)

`PageAwareAnswers`: fields are dynamically generated from `config/questions.yaml`. The agent should construct this model at runtime by reading the config. Each question in the config produces one field. Text questions become `str`. List questions become `list[str]`. Score questions become a `ScoreAnswer` object. Boolean-with-explanation become `BooleanWithExplanation` object.

`ScoreAnswer`:
- `score: int` (1-10)
- `justification: str`

`BooleanWithExplanation`:
- `value: bool`
- `explanation: str`

`PageBlindResponse`:
- `provider: str`
- `model: str`
- `requested_at: datetime`
- `query_results: list[PageBlindQueryResult]` (one per generated query)
- `usage: UsageInfo`

`PageBlindQueryResult`:
- `query_id: str`
- `query_text: str`
- `response_text: str` (the model's full response)
- `brand_mentioned: bool` (post-processed by string match)
- `mention_position: int | None` (1-indexed position in a list-style response, null if not mentioned or not in a list)
- `competitors_mentioned: list[str]` (post-processed by string match against expected_competitors)

`UsageInfo`:
- `input_tokens: int`
- `output_tokens: int`
- `cost_usd: float` (computed from token counts and provider pricing)

## VerificationResult

Output of the verification pass. Schema matches `prompts/06_verification.md`.

Fields:

- `verified_at: datetime`
- `overall_support_level: Literal["fully_supported", "mostly_supported", "partially_supported", "weakly_supported", "unsupported"]`
- `field_checks: list[FieldCheck]`
- `hallucinations: list[HallucinationFlag]`
- `notable_omissions: list[str]` (max length 5)

`FieldCheck`:
- `field: str`
- `claim_summary: str`
- `support_level: Literal["supported", "paraphrased", "partially_supported", "unsupported", "opinion_or_inference"]`
- `notes: str`

`HallucinationFlag`:
- `field: str`
- `claim: str`
- `reason: str`

## PersonaReview

Layer 4 output. One per persona.

Fields:

- `persona_name: str`
- `task_completion_likelihood: int` (1-10)
- `task_completion_explanation: str`
- `first_impression: str`
- `what_works: list[str]` (length 2-4)
- `top_friction: list[str]` (length 2-4)
- `missing_information: list[str]` (max length 5)
- `trust_signals: PersonaTrustSignals`
- `decision_outcome: str` (see prompt for valid values, varies by is_llm_lens)
- `decision_outcome_explanation: str`
- `honest_summary: str`

`PersonaTrustSignals`:
- `score: int` (1-10)
- `what_built_trust: list[str]`
- `what_eroded_trust: list[str]`

## Synthesis

Layer 5 output. The most complex model. Schema matches `prompts/05_synthesis.md`.

Top-level fields:

- `composite_score: int` (0-100)
- `composite_score_band: Literal["low", "medium", "high"]`
- `composite_score_rationale: str`
- `executive_summary: ExecutiveSummary`
- `llm_readiness: LlmReadiness`
- `identity_clarity: IdentityClarity`
- `brand_visibility: BrandVisibility`
- `technical_findings: list[Finding]`
- `persona_summary: list[PersonaSummaryItem]`
- `convergence: list[ConvergenceItem]`
- `divergence: list[DivergenceItem]`
- `recommendations: list[Recommendation]`
- `page_blind_queries: list[str]`
- `report_caveats: list[str]`

### Sub-models

`ExecutiveSummary`:
- `headline: str`
- `top_findings: list[Finding]` (length 3-7)

`Finding`:
- `text: str` (max 18 words)
- `severity: Literal["critical", "warning", "info", "healthy"]`
- `evidence_sources: list[str]` (omitted from technical_findings)

`LlmReadiness`:
- `composite: int` (0-100)
- `crawlability: int` (0-100)
- `structure: int` (0-100)
- `self_sufficiency: int` (0-100)
- `trust_signals: int` (0-100)
- `identity_clarity: int` (0-100)
- `explanations: dict[str, str]`

`IdentityClarity`:
- `self_stated: str`
- `page_aware_consensus: str`
- `page_blind_consensus: str`
- `self_to_page_aware_gap: str`
- `page_aware_to_blind_gap: str`

`BrandVisibility`:
- `mention_rate: float` (0.0-1.0)
- `competitive_context: list[str]`
- `accuracy_when_mentioned: Literal["accurate", "partially_accurate", "outdated", "incorrect", "not_applicable"]`
- `citation_gaps: list[str]`

`PersonaSummaryItem`:
- `persona_name: str`
- `score: int` (1-10)
- `decision_outcome: str`
- `top_friction: str`

`ConvergenceItem`:
- `finding: str`
- `evaluators: list[str]` (minimum 3)
- `severity: Literal["critical", "warning", "info", "healthy"]`

`DivergenceItem`:
- `topic: str`
- `positions: list[DivergencePosition]` (minimum 2)
- `interpretation: str`

`DivergencePosition`:
- `evaluator: str`
- `position: str`

`Recommendation`:
- `priority: int` (1-N)
- `issue: str`
- `fix: str`
- `effort: Literal["low", "medium", "high"]`
- `estimated_impact: Literal["low", "medium", "high"]`
- `category: Literal["technical", "content", "structure", "llm_readiness", "ux", "trust"]`

## StepError

Used in `RunManifest.errors`.

Fields:

- `step: str`
- `error_type: str`
- `message: str`
- `traceback: str | None`
- `recoverable: bool`
- `occurred_at: datetime`
