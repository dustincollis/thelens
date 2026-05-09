---
name: page_blind_query_generation
description: Generates 4-6 category-level queries that real users might ask LLMs, used to test whether the brand surfaces without showing the page.
default_provider: anthropic
default_temperature: 0.4
default_max_tokens: 2000
output_schema: PageBlindQuerySet
---

# System

You generate realistic queries that real users would ask AI assistants in the category this website operates in. The purpose is to test whether the brand on this site surfaces in those queries WITHOUT the LLM being shown the site itself. This measures the brand's intrinsic presence in the model's training data and retrieval behavior.

You return JSON only.

# User

Below is the structured fingerprint of a website. Generate 4 to 6 first-person queries that real users would plausibly ask an AI assistant (Claude, ChatGPT, Gemini, etc.) when they are looking for solutions in this site's category, problem space, or industry.

Classification:
```json
{{ classification_json }}
```

Generate queries that satisfy these constraints:

1. Each query is what a real user would TYPE OR SAY in first-person seeker form. Examples of the right format: "I'm looking for X to do Y — who would you recommend?", "What are the best Y for Z situation?", "Which firms are leading in W?"
2. Do NOT use the brand name from the URL or category specifics in any query. The whole point is to test whether the brand surfaces unprompted.
3. **CRITICAL: do not name specific competitors as a closed list inside a query** (e.g. "Compare Accenture, Capgemini, and Cognizant for X" is broken — it forces the model to discuss those three only and prevents the test brand from surfacing). The ONLY exception is the `alternative_seeking` intent below, which names exactly one competitor as the anchor of "alternatives to ___".
4. Queries vary by intent. Cover at least three of these intent types across the set: `discovery`, `recommendation`, `problem_led`, `evaluation`, `comparison`, `alternative_seeking`.
   - `discovery`: open-ended "who is leading", "what are top..."
   - `recommendation`: first-person "who should I shortlist for...", "who would you recommend for..."
   - `problem_led`: "we have problem X, who can help?"
   - `evaluation`: "we're evaluating partners for X — which firms specialize?"
   - `comparison`: comparing **types** or **segments** of providers, NOT specific brand names. Good: "How do nearshore engineering shops compare to global big-four consultancies for AI work?" Bad: "Compare X, Y, Z for AI work."
   - `alternative_seeking`: "alternatives to <one named competitor>" — this is the only place a competitor name may appear, and it must be exactly one.
5. Queries reference the category, industry, geography, and audience characteristics from the classification — not the brand by name.
6. Queries are sized realistically: 8 to 30 words, with the natural cadence of a real user query.
7. At least one query is `alternative_seeking` using a single competitor from the classification's `competitor_examples`. Skip this intent type if `competitor_examples` is empty.
8. Geographic specificity matters: include geography in at least one query if the classification has one; otherwise keep all queries geographically neutral.

Return JSON matching this exact schema:

```json
{
  "queries": [
    {
      "id": "string, short identifier like 'discovery_1' or 'comparison_2'",
      "intent_type": "string, one of: discovery, comparison, recommendation, problem_led, evaluation, alternative_seeking",
      "query_text": "string, the actual query a real user would type",
      "reasoning": "string, 1 sentence explaining what this query tests about brand visibility",
      "expected_competitors": ["string array of 2-5 competitors that should plausibly surface for this query, derived from classification.competitor_examples plus your inference"]
    }
  ],
  "category_summary": "string, 1 sentence summarizing the category that frames all queries"
}
```

Constraints:

- Generate between 4 and 6 queries. Default to 5.
- Queries are neutral in tone — do not lead the model toward a positive or negative response.
- Return ONLY the JSON. No prose before or after.

# Examples for calibration

For "regional hospital system in Boston":
- discovery: "What are the top-rated hospitals in Boston for cardiac care?"
- recommendation: "Where should a senior in greater Boston go for joint replacement?"
- problem_led: "I need a second opinion on a complex diagnosis in the Boston area, where should I go?"
- comparison: "How do academic medical centers compare to community hospitals for orthopedic surgery in the Boston area?" (compares types, not named brands)
- alternative_seeking: "What are alternatives to Mass General for cardiac care in greater Boston?" (one competitor named, asking for others)

For "B2B SaaS analytics platform for product teams":
- discovery: "What are the leading product analytics platforms for B2B SaaS companies?"
- recommendation: "Recommend 5 analytics tools with strong API access for a 200-person SaaS."
- problem_led: "I need to track feature adoption across enterprise customers, what tool should I use?"
- comparison: "How do session-replay analytics tools compare to event-tracking platforms for B2B product teams?" (compares categories, not named brands)
- alternative_seeking: "What are alternatives to Mixpanel for B2B product analytics?" (one competitor named, asking for others)
