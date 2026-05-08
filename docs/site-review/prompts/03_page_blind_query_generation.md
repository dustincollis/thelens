---
name: page_blind_query_generation
description: Generates 4-6 category-level queries that real users might ask LLMs, used to test whether the brand surfaces without showing the page.
input_schema:
  classification: Classification
output_schema: PageBlindQuerySet
default_provider: anthropic
default_temperature: 0.4
default_max_tokens: 2000
---

# System

You generate realistic queries that real users would ask AI assistants in the category this website operates in. The purpose is to test whether the brand on this site surfaces in those queries WITHOUT the LLM being shown the site itself. This measures the brand's intrinsic presence in the model's training data and retrieval behavior.

You return JSON only.

# User

Below is the structured fingerprint of a website. Generate 4 to 6 queries that real users would plausibly ask an AI assistant (Claude, ChatGPT, Gemini, etc.) about this site's category, problem space, or industry.

Classification:
```json
{classification_json}
```

Generate queries that satisfy these constraints:

1. Each query is what a real user would TYPE OR SAY. It is not "Tell me about <brand name>." That is a vanity check, not a visibility check.
2. Queries vary by intent type. Cover at least three of these intents across the set: discovery, comparison, recommendation, problem-led, evaluation, alternative-seeking.
3. Queries reference the category, industry, geography, and audience characteristics from the classification, NOT the brand by name.
4. Queries are sized realistically: 5 to 25 words, with the natural cadence of a real query.
5. At least one query is "alternative-seeking" (looking for alternatives to a specific competitor) using a competitor name from the classification's `competitor_examples`. If `competitor_examples` is empty, skip this intent type.

Return JSON matching this exact schema:

```json
{
  "queries": [
    {
      "id": "string, short identifier like 'discovery_1' or 'comparison_2'",
      "intent_type": "string, one of: discovery, comparison, recommendation, problem_led, evaluation, alternative_seeking",
      "query_text": "string, the actual query text",
      "reasoning": "string, 1 sentence explaining what this query tests",
      "expected_competitors": ["string array of 2-5 competitors that should plausibly surface for this query, derived from classification.competitor_examples plus your inference"]
    }
  ],
  "category_summary": "string, 1 sentence summarizing the category that frames all queries"
}
```

Constraints:

- Generate between 4 and 6 queries. Default to 5.
- Do NOT use the brand name from the URL or page title in any query.
- Queries should be neutral in tone. Do not lead the model toward a positive or negative response.
- Geographic specificity matters: if the classification has a geography, include it in at least one query. If it does not, all queries should be geographically neutral.
- Return ONLY the JSON. No prose before or after.

# Examples for calibration

For a "regional hospital system in Boston":
- Discovery: "What are the top-rated hospitals in Boston for cardiac care?"
- Comparison: "Compare Mass General and Brigham and Women's for orthopedic surgery."
- Recommendation: "Where should a senior in greater Boston go for joint replacement?"
- Problem-led: "I need a second opinion on a complex diagnosis in the Boston area, where should I go?"

For "B2B SaaS analytics platform for product teams":
- Discovery: "What are the leading product analytics platforms for B2B SaaS companies?"
- Comparison: "Compare Mixpanel, Amplitude, and Heap for tracking user behavior."
- Recommendation: "Recommend 5 analytics tools with strong API access for a 200-person SaaS."
- Problem-led: "I need to track feature adoption across enterprise customers, what tool should I use?"
- Alternative-seeking: "What are alternatives to Mixpanel for B2B product analytics?"
