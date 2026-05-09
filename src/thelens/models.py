"""Pydantic data models for The Lens pipeline.

Every step in the pipeline writes its output to a JSON file. Each file maps
to a model in this module; reads validate against the model on load. The
schema-as-contract rule means LLM outputs must produce JSON matching these
shapes (validated immediately on receipt in later phases).

Phase 1 only defines `TechnicalAudit` and `RunManifest`. Later phases add
`Classification`, `Persona`, `PersonaSet`, `PageAwareResponse`,
`PageBlindResponse`, `PersonaReview`, and `Synthesis`.
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


StepStatus = Literal["pending", "running", "complete", "failed", "skipped"]
RunStatus = Literal["pending", "running", "complete", "failed"]
CrawlerStatus = Literal["allowed", "disallowed", "unknown"]

ClassificationCategory = Literal[
    "ecommerce",
    "b2b_saas",
    "b2c_saas",
    "publisher",
    "news",
    "nonprofit",
    "government",
    "healthcare",
    "education",
    "financial_services",
    "professional_services",
    "agency",
    "portfolio",
    "community",
    "documentation",
    "marketing_landing",
    "ecommerce_brand",
    "marketplace",
    "other",
]

EvidentGoal = Literal[
    "lead_generation",
    "direct_sale",
    "signup_or_trial",
    "education",
    "brand_awareness",
    "retention",
    "fundraising",
    "recruitment",
    "support",
    "other",
]

BrandRegister = Literal[
    "formal",
    "technical",
    "authoritative",
    "casual",
    "conversational",
    "transactional",
    "journalistic",
    "academic",
]

Confidence = Literal["high", "medium", "low"]
ExpertiseLevel = Literal["novice", "intermediate", "expert"]
DecisionAuthority = Literal["researcher", "influencer", "decision_maker", "end_user"]
TrustPosture = Literal["skeptical", "neutral", "trusting", "urgent"]


class RunManifest(BaseModel):
    """Top-level run metadata, written as `manifest.json` in the run folder.

    Mirrored into the SQLite `runs` table for history queries. The filesystem
    is the source of truth; SQLite is rebuildable via `lens reindex`.
    """

    model_config = ConfigDict(extra="forbid")

    run_id: str
    url: str
    started_at: datetime
    completed_at: datetime | None = None
    status: RunStatus = "pending"
    providers_used: list[str] = Field(default_factory=list)
    personas_generated: int = 0
    estimated_cost_usd: float = 0.0
    actual_cost_usd: float = 0.0
    composite_score: int | None = None
    step_status: dict[str, StepStatus] = Field(default_factory=dict)


class RenderModeDiff(BaseModel):
    model_config = ConfigDict(extra="forbid")

    raw_text_chars: int
    rendered_text_chars: int
    js_trapped_pct: float


class SemanticTagUsage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    article: int = 0
    section: int = 0
    nav: int = 0
    main: int = 0
    header: int = 0
    footer: int = 0
    aside: int = 0


class HtmlStructure(BaseModel):
    model_config = ConfigDict(extra="forbid")

    h1_count: int
    heading_hierarchy_violations: int
    semantic_tag_usage: SemanticTagUsage
    dom_to_content_ratio: float
    image_count: int
    images_missing_alt: int
    alt_text_coverage_pct: float
    low_quality_link_text_count: int


class StructuredData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    json_ld_blocks: int
    json_ld_types: list[str]
    json_ld_valid: bool
    open_graph: dict[str, bool]
    twitter_card: bool
    missing_recommended_schemas: list[str]


class AiCrawlerAccess(BaseModel):
    model_config = ConfigDict(extra="forbid")

    robots_txt_present: bool
    crawlers: dict[str, CrawlerStatus]


class LlmsTxt(BaseModel):
    model_config = ConfigDict(extra="forbid")

    present: bool
    valid_markdown: bool | None = None
    size_bytes: int | None = None


class TrustSignals(BaseModel):
    model_config = ConfigDict(extra="forbid")

    https: bool
    contact_info_present: bool
    privacy_policy_link: bool
    author_byline: bool
    last_updated_date: bool


class PageSize(BaseModel):
    model_config = ConfigDict(extra="forbid")

    html_bytes: int
    total_bytes_estimate: int


class TechnicalAudit(BaseModel):
    """Output of step 2 — pure-Python technical and structural audit."""

    model_config = ConfigDict(extra="forbid")

    url: str
    fetched_at: datetime
    render_mode_diff: RenderModeDiff
    html_structure: HtmlStructure
    structured_data: StructuredData
    ai_crawler_access: AiCrawlerAccess
    llms_txt: LlmsTxt
    trust_signals: TrustSignals
    page_size: PageSize


# ============================================================================
# Layer 1: Site classification
# ============================================================================


class ContentMaturity(BaseModel):
    model_config = ConfigDict(extra="forbid")

    has_blog: bool
    has_documentation: bool
    has_pricing: bool
    has_case_studies: bool
    has_about_page: bool
    has_team_page: bool


class Classification(BaseModel):
    """Output of step 3 (Layer 1). Site fingerprint that drives downstream personas and queries."""

    model_config = ConfigDict(extra="forbid")

    url: str
    category: ClassificationCategory
    category_specifics: str
    audience_summary: str
    audience_segments: list[str] = Field(min_length=1, max_length=6)
    evident_goal: EvidentGoal
    evident_goal_explanation: str
    content_maturity: ContentMaturity
    brand_register: BrandRegister
    industry: str
    geography: str | None
    competitor_examples: list[str] = Field(default_factory=list, max_length=6)
    confidence: Confidence


# ============================================================================
# Layer 2: Personas
# ============================================================================


class Persona(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    role: str
    context: str
    goal: str
    expertise_level: ExpertiseLevel
    decision_authority: DecisionAuthority
    primary_concerns: list[str] = Field(min_length=2, max_length=8)
    trust_posture: TrustPosture
    is_llm_lens: bool
    rationale: str


class PersonaSet(BaseModel):
    """Output of step 4 (Layer 2). 3–5 review personas. Exactly one is the LLM-as-reader lens."""

    model_config = ConfigDict(extra="forbid")

    personas: list[Persona] = Field(min_length=3, max_length=5)
    generation_notes: str

    @model_validator(mode="after")
    def _exactly_one_llm_lens(self) -> "PersonaSet":
        n = sum(1 for p in self.personas if p.is_llm_lens)
        if n != 1:
            raise ValueError(
                f"PersonaSet must contain exactly one persona with is_llm_lens=true; got {n}"
            )
        return self


# ============================================================================
# Shared: LLM usage tracking
# ============================================================================


class UsageInfo(BaseModel):
    """Token counts and cost for a single LLM call."""

    model_config = ConfigDict(extra="forbid")

    provider: str
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float


# ============================================================================
# Layer 3a: Page-aware question types (used in dynamic answer schema)
# ============================================================================


class ScoreAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    score: int = Field(ge=1, le=10)
    justification: str


class BooleanWithExplanation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: bool
    explanation: str


QuestionType = Literal["text", "list", "score", "boolean_with_explanation"]


class Question(BaseModel):
    """A single page-aware question loaded from `config/questions.yaml`."""

    model_config = ConfigDict(extra="forbid")

    id: str
    type: QuestionType
    prompt: str
    count: int | None = None
    max_length: int | None = None
    item_max_length: int | None = None


class PageAwareResponse(BaseModel):
    """One provider's page-aware answers, written as `llm/<provider>_page_aware.json`.

    `answers` is validated by the dynamic Pydantic model built from
    `config/questions.yaml`, then stored as a dict for portability — the
    field shape varies with the question set.
    """

    model_config = ConfigDict(extra="forbid")

    provider: str
    model: str
    requested_at: datetime
    response_received_at: datetime
    answers: dict[str, object]
    usage: UsageInfo
    hallucination_flags: "VerificationResult | None" = None


# ============================================================================
# Layer 3b: Page-blind query generation + per-query results
# ============================================================================


PageBlindIntent = Literal[
    "discovery",
    "comparison",
    "recommendation",
    "problem_led",
    "evaluation",
    "alternative_seeking",
]


class PageBlindQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    intent_type: PageBlindIntent
    query_text: str
    reasoning: str
    expected_competitors: list[str] = Field(default_factory=list, max_length=6)


class PageBlindQuerySet(BaseModel):
    """Output of the query-generation LLM call (one per run, not per provider)."""

    model_config = ConfigDict(extra="forbid")

    queries: list[PageBlindQuery] = Field(min_length=3, max_length=8)
    category_summary: str


class PageBlindQueryResult(BaseModel):
    """One query's result against one provider, with brand-mention post-processing."""

    model_config = ConfigDict(extra="forbid")

    query_id: str
    query_text: str
    response_text: str
    brand_mentioned: bool
    mention_position: int | None = None
    competitors_mentioned: list[str] = Field(default_factory=list)


class PageBlindResponse(BaseModel):
    """One provider's page-blind results, written as `llm/<provider>_page_blind.json`."""

    model_config = ConfigDict(extra="forbid")

    provider: str
    model: str
    requested_at: datetime
    query_results: list[PageBlindQueryResult]
    usage: UsageInfo


# ============================================================================
# Verification (separate pass over each page-aware response)
# ============================================================================


SupportLevel = Literal[
    "supported",
    "paraphrased",
    "partially_supported",
    "unsupported",
    "opinion_or_inference",
]

OverallSupportLevel = Literal[
    "fully_supported",
    "mostly_supported",
    "partially_supported",
    "weakly_supported",
    "unsupported",
]


class FieldCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field: str
    claim_summary: str
    support_level: SupportLevel
    notes: str


class HallucinationFlag(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field: str
    claim: str
    reason: str


class VerificationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    verified_at: datetime
    overall_support_level: OverallSupportLevel
    field_checks: list[FieldCheck]
    hallucinations: list[HallucinationFlag] = Field(default_factory=list)
    notable_omissions: list[str] = Field(default_factory=list, max_length=5)


# Resolve forward ref on PageAwareResponse
PageAwareResponse.model_rebuild()


# ============================================================================
# Layer 4: Persona reviews
# ============================================================================


GoalOutcome = Literal[
    "fully_achieved", "partially_achieved", "not_achieved", "blocked"
]
NextAction = Literal[
    "proceed", "research_more", "abandon", "contact_support", "look_elsewhere"
]


class PersonaReview(BaseModel):
    """One persona's structured review of the page.

    Output of step 7 — written as `persona_reviews/persona_<n>.json`. The
    LLM roleplays as the persona; the review reflects that perspective.
    """

    model_config = ConfigDict(extra="forbid")

    persona_name: str
    persona_role: str
    goal_outcome: GoalOutcome
    goal_outcome_explanation: str
    what_worked: list[str] = Field(min_length=1, max_length=8)
    what_failed: list[str] = Field(min_length=1, max_length=8)
    persona_satisfaction_score: int = Field(ge=1, le=10)
    score_justification: str
    next_action: NextAction
    next_action_explanation: str
    quotable_observation: str


# ============================================================================
# Layer 5: Synthesis
# ============================================================================


ImpactLevel = Literal["critical", "high", "medium", "low"]
SeverityLevel = Literal["critical", "high", "medium", "low"]
EffortLevel = Literal["trivial", "low", "medium", "high"]


class ConvergenceFinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    finding: str
    sources: list[str] = Field(min_length=2, max_length=10)
    confidence: Confidence
    impact: ImpactLevel


class DivergencePerspective(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: str
    view: str


class DivergenceFinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    finding: str
    perspectives: list[DivergencePerspective] = Field(min_length=2, max_length=6)
    likely_resolution: str


class Recommendation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str
    rationale: str
    severity: SeverityLevel
    effort: EffortLevel
    expected_impact: str


class ScoreBreakdown(BaseModel):
    model_config = ConfigDict(extra="forbid")

    clarity: int = Field(ge=0, le=100)
    llm_readability: int = Field(ge=0, le=100)
    audience_fit: int = Field(ge=0, le=100)
    trust: int = Field(ge=0, le=100)
    action_clarity: int = Field(ge=0, le=100)


class Synthesis(BaseModel):
    """Output of step 8 — the final cross-lens synthesis.

    Composite score + per-dimension breakdown + executive summary +
    convergence/divergence findings + prioritized recommendations.
    Written as `synthesis.json`.
    """

    model_config = ConfigDict(extra="forbid")

    composite_score: int = Field(ge=0, le=100)
    score_breakdown: ScoreBreakdown
    executive_summary: list[str] = Field(min_length=3, max_length=5)
    convergence_findings: list[ConvergenceFinding] = Field(
        min_length=1, max_length=10
    )
    divergence_findings: list[DivergenceFinding] = Field(
        default_factory=list, max_length=8
    )
    recommendations: list[Recommendation] = Field(min_length=1, max_length=12)
    notes: str | None = None
