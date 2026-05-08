---
name: synthesis
description: Takes all prior outputs and produces the convergence findings, divergence findings, prioritized recommendations, identity clarity comparison, and composite scores.
input_schema:
  classification: Classification
  technical_audit: TechnicalAudit
  personas: PersonaSet
  persona_reviews: list[PersonaReview]
  page_aware_responses: dict[str, PageAwareResponse]
  page_blind_responses: dict[str, PageBlindResponse]
output_schema: Synthesis
default_provider: anthropic
default_temperature: 0.3
default_max_tokens: 6000
---

# System

You are a senior analyst synthesizing the results of a multi-evaluator website review. Your job is to find the patterns, name the contradictions, and produce a prioritized action list. You write for an executive audience: every claim is concrete and defensible.

You read all of the upstream outputs (technical audit, multi-LLM responses, persona reviews) and produce a single integrated synthesis. You do not invent findings. Every finding must trace to evidence in the inputs.

You return JSON only.

# User

Below are all upstream outputs from a website review. Synthesize them into the structured output defined by the schema.

Classification:
```json
{classification_json}
```

Technical audit:
```json
{technical_audit_json}
```

Personas:
```json
{personas_json}
```

Persona reviews:
```json
{persona_reviews_json}
```

Page-aware multi-LLM responses (one per provider):
```json
{page_aware_responses_json}
```

Page-blind multi-LLM responses (one per provider):
```json
{page_blind_responses_json}
```

Return JSON matching this exact schema:

```json
{
  "composite_score": "integer 0-100, weighted composite score across all dimensions",
  "composite_score_band": "string, one of: low (0-50), medium (51-75), high (76-100)",
  "composite_score_rationale": "string, 2-3 sentences explaining the score",

  "executive_summary": {
    "headline": "string, 1 sentence capturing the single most important takeaway",
    "top_findings": [
      {
        "text": "string, 1 sentence, max 18 words",
        "severity": "string, one of: critical, warning, info, healthy",
        "evidence_sources": ["string array, which inputs support this. Examples: 'persona_reviews', 'page_aware:gpt', 'technical_audit'"]
      }
    ]
  },

  "llm_readiness": {
    "composite": "integer 0-100",
    "crawlability": "integer 0-100",
    "structure": "integer 0-100",
    "self_sufficiency": "integer 0-100",
    "trust_signals": "integer 0-100",
    "identity_clarity": "integer 0-100",
    "explanations": {
      "crawlability": "string, 1 sentence",
      "structure": "string, 1 sentence",
      "self_sufficiency": "string, 1 sentence",
      "trust_signals": "string, 1 sentence",
      "identity_clarity": "string, 1 sentence"
    }
  },

  "identity_clarity": {
    "self_stated": "string, 1-2 sentences extracted from the page about how the brand describes itself",
    "page_aware_consensus": "string, 1-2 sentences synthesizing how the four LLMs described the site when shown the page",
    "page_blind_consensus": "string, 1-2 sentences synthesizing what the four LLMs said about the brand when NOT shown the page. If the brand was not mentioned in any page-blind response, state that explicitly.",
    "self_to_page_aware_gap": "string, 1 sentence describing the gap between what the brand says about itself and what LLMs say after reading the page",
    "page_aware_to_blind_gap": "string, 1 sentence describing the gap between what LLMs say with vs. without the page. This gap is the brand's intrinsic visibility problem."
  },

  "brand_visibility": {
    "mention_rate": "float 0.0-1.0, fraction of page-blind queries across all providers in which the brand was mentioned by name",
    "competitive_context": ["string array, top competitors that surfaced INSTEAD of the brand in page-blind queries, ordered by frequency"],
    "accuracy_when_mentioned": "string, one of: accurate, partially_accurate, outdated, incorrect, not_applicable",
    "citation_gaps": ["string array of queries where the brand should plausibly appear but did not, taken from the page-blind queries"]
  },

  "technical_findings": [
    {
      "text": "string, 1 sentence, max 18 words",
      "severity": "string, one of: critical, warning, info, healthy"
    }
  ],

  "persona_summary": [
    {
      "persona_name": "string",
      "score": "integer 1-10",
      "decision_outcome": "string, copied from the persona review",
      "top_friction": "string, 1 sentence summarizing the most severe friction for this persona"
    }
  ],

  "convergence": [
    {
      "finding": "string, 1 sentence describing something all or most evaluators flagged",
      "evaluators": ["string array of which evaluators flagged it. Examples: 'persona:Marcus', 'page_aware:claude', 'technical_audit'"],
      "severity": "string, one of: critical, warning, info, healthy"
    }
  ],

  "divergence": [
    {
      "topic": "string, 1 sentence naming the topic of disagreement",
      "positions": [
        {
          "evaluator": "string, who held this position",
          "position": "string, 1 sentence summarizing what they said"
        }
      ],
      "interpretation": "string, 1 sentence on what the disagreement reveals about the page"
    }
  ],

  "recommendations": [
    {
      "priority": "integer 1-N, where 1 is highest priority",
      "issue": "string, 1 sentence describing the issue",
      "fix": "string, 1-2 sentences describing the recommended fix",
      "effort": "string, one of: low, medium, high",
      "estimated_impact": "string, one of: low, medium, high",
      "category": "string, one of: technical, content, structure, llm_readiness, ux, trust"
    }
  ],

  "page_blind_queries": ["string array, copy of the actual queries that were run, for the deck slide"],

  "report_caveats": ["string array, things to note honestly. Example: 'Grok responses include real-time X data which may have influenced the page-blind results.'"]
}
```

Constraints:

- Composite score weighting: technical and structural audit (25%), persona reviews (30%), multi-LLM page-aware consensus (25%), brand visibility from page-blind (10%), trust signals (10%). Use this as guidance, not as a formula to publish.
- `top_findings` in the executive summary: between 3 and 7 items.
- `convergence`: between 2 and 8 items. Only include findings supported by at least 3 evaluators.
- `divergence`: between 1 and 5 items. Only include genuine disagreements, not surface-level wording differences.
- `recommendations`: between 5 and 12 items. Order by priority (1 = highest). Group related fixes into single recommendations rather than fragmenting.
- Every finding should be evidence-traceable. The `evidence_sources` and `evaluators` fields exist for this purpose.
- Do not invent recommendations not supported by the inputs. If the inputs do not surface a particular issue, do not add it.
- Return ONLY the JSON. No prose before or after.
