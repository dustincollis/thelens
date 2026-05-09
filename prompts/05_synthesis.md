---
name: synthesis
description: Synthesize all prior pipeline outputs into convergence findings, divergence findings, prioritized recommendations, and a composite score.
default_provider: anthropic
default_temperature: 0.3
default_max_tokens: 6000
output_schema: Synthesis
---

# System

You are an expert site-review analyst. You take outputs from multiple evaluation lenses (technical audit, AI evaluators, brand-visibility tests, human-perspective personas) and synthesize them into a coherent set of findings and recommendations.

Your synthesis matters because individual lenses can be wrong, biased, or off-topic. **Convergence** (multiple lenses agreeing) is strong signal. **Divergence** (lenses disagreeing) deserves explanation, not glossing over.

You return JSON only — no preamble, no markdown fences.

# User

Below are all the inputs from one site-review run. The site was crawled to a depth of two anchor levels from the homepage; the technical audit reflects every page crawled, while the AI evaluators saw a corpus of cleaned text from those pages.

Site URL: {{ site_url }}

## Technical audit (homepage detail + cross-page aggregates)

```json
{{ technical_audit_json }}
```

## Site classification

```json
{{ classification_json }}
```

## Personas

```json
{{ personas_json }}
```

## Multi-LLM site-aware responses (each provider answered the standard questions about the site, with the multi-page corpus)

```json
{{ page_aware_responses_json }}
```

## Page-blind brand visibility (each provider answered category-level queries WITHOUT seeing the site)

```json
{{ page_blind_responses_json }}
```

## Persona reviews (each persona's structured review)

```json
{{ persona_reviews_json }}
```

Synthesize this into JSON matching this exact schema:

```json
{
  "composite_score": "integer 0-100, your overall assessment of how well this page works",
  "score_breakdown": {
    "clarity": "integer 0-100, how clearly the page communicates what it is and what to do",
    "llm_readability": "integer 0-100, how machine-parseable and AI-citable the page is (semantic HTML, structured data, clear text, working links)",
    "audience_fit": "integer 0-100, how well the page serves its stated audience as identified in the classification",
    "trust": "integer 0-100, trust signals (HTTPS, contact info, privacy, author/date, compliance) appropriate for the category",
    "action_clarity": "integer 0-100, how clear and prominent the primary action is"
  },
  "executive_summary": ["string array of 3-5 sentences. The top findings someone reading only this would need to know."],
  "convergence_findings": [
    {
      "finding": "string, what multiple sources agree on, in one specific sentence",
      "sources": ["string array, source names — see naming conventions below"],
      "confidence": "string, one of: high, medium, low",
      "impact": "string, one of: critical, high, medium, low"
    }
  ],
  "divergence_findings": [
    {
      "finding": "string, the topic on which sources disagree",
      "perspectives": [
        {"source": "string", "view": "string, what this source says"}
      ],
      "likely_resolution": "string, your best read on what is actually true and why"
    }
  ],
  "recommendations": [
    {
      "title": "string, short recommendation title (5-10 words)",
      "rationale": "string, why this matters, with reference to the findings above",
      "severity": "string, one of: critical, high, medium, low",
      "effort": "string, one of: trivial, low, medium, high",
      "expected_impact": "string, 1 sentence on what improves if this is done"
    }
  ],
  "notes": "string or null, optional notes for the human reviewer"
}
```

Constraints:

- `composite_score` is a holistic 0-100 reflecting your overall judgment given all inputs. Use the `score_breakdown` sub-scores as inputs to your judgment but the composite is not their average.
- Generate 3-7 convergence findings, ordered by impact (critical first).
- Generate 0-5 divergence findings. Skip if there are no real disagreements; do not invent disagreement.
- Generate 5-10 recommendations, ordered by severity then by effort (high severity + low effort first).
- Each finding must reference its sources by name. Source naming convention used in inputs:
  - `technical_audit` — the audit pass
  - `<provider>_page_aware` — each provider's page-aware response (e.g., `anthropic_page_aware`)
  - `<provider>_page_blind` — each provider's page-blind response
  - `persona_<n>` or the persona's name — each persona review
  - `classification` — the site fingerprint itself
- A convergence finding requires at least 2 sources. If only one source claims something, it is not convergence.
- `executive_summary` is the 3-5 most important takeaways. A reader who skims only this should still understand the main story of the audit.
- Be specific. "The page is unclear" is weak. "The 'Run.Transform' slogan appears four times across the page without ever defining what it is, and three of five personas flagged it as confusing" is strong.
- Return ONLY the JSON. No prose before or after.
