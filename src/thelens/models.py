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

from pydantic import BaseModel, ConfigDict, Field


StepStatus = Literal["pending", "running", "complete", "failed", "skipped"]
RunStatus = Literal["pending", "running", "complete", "failed"]
CrawlerStatus = Literal["allowed", "disallowed", "unknown"]


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
