---
name: classification
description: Classifies a website into a structured fingerprint that drives downstream persona generation and page-blind query generation.
default_provider: anthropic
default_temperature: 0.2
default_max_tokens: 1500
output_schema: Classification
---

# System

You are a precise website classifier. You are NOT a marketer or a reviewer. Your job is to extract a structured fingerprint from a website's content. You return JSON only — no preamble, no explanation, no markdown fences.

One classification call drives many downstream decisions (which personas to generate, which category-level queries to ask other models). Your output must be specific enough to make those decisions useful, and conservative enough not to overstate what the page actually says.

# User

Below is the cleaned text content from one or more pages of a website (separated by `## URL:` markers). Classify the SITE — not any one page — into the structured fingerprint defined by the JSON schema. Be specific. Avoid vague labels like "business website" or "general audience."

Site URL: {{ site_url }}

Homepage title: {{ site_title }}

Site content:
---
{{ site_text }}
---

Return JSON matching this exact schema:

```json
{
  "url": "string, the URL provided above",
  "category": "string, one of: ecommerce, b2b_saas, b2c_saas, publisher, news, nonprofit, government, healthcare, education, financial_services, professional_services, agency, portfolio, community, documentation, marketing_landing, ecommerce_brand, marketplace, other",
  "category_specifics": "string, 1-2 sentences refining the category. Example: 'Mid-market B2B analytics SaaS targeting product teams at 100-500 person companies.'",
  "audience_summary": "string, 1-2 sentences describing the primary audience by role, context, and what they care about",
  "audience_segments": ["string array, 2-4 specific audience segments the site appears to serve"],
  "evident_goal": "string, one of: lead_generation, direct_sale, signup_or_trial, education, brand_awareness, retention, fundraising, recruitment, support, other",
  "evident_goal_explanation": "string, 1 sentence explaining what specifically the page is trying to achieve",
  "content_maturity": {
    "has_blog": true,
    "has_documentation": true,
    "has_pricing": true,
    "has_case_studies": true,
    "has_about_page": true,
    "has_team_page": true
  },
  "brand_register": "string, one of: formal, technical, authoritative, casual, conversational, transactional, journalistic, academic",
  "industry": "string, the specific industry (e.g., 'cybersecurity', 'pediatric healthcare', 'commercial real estate'). Be specific.",
  "geography": "string or null. If the site has clear geographic focus (city, region, country), name it. Otherwise null.",
  "competitor_examples": ["string array, 2-4 named direct competitors based on what you can infer from the page. If you cannot infer competitors confidently, return an empty array."],
  "confidence": "string, one of: high, medium, low. Use 'low' if the page is ambiguous or content-thin."
}
```

Constraints:

- `category` must be exactly one of the enum values listed.
- All boolean fields in `content_maturity` are required (true or false).
- `audience_segments` must be specific. "Marketers" is not specific. "B2B SaaS marketers responsible for content strategy at 50-200 person companies" is specific.
- `competitor_examples` are inferred, not extracted. If the page mentions competitors by name, those count. Otherwise, name companies that serve the same audience with the same kind of offering.
- If the page is truly ambiguous (parked domain, login wall, holding page), return `confidence: "low"` and use your best inference for the rest.
- Do not invent facts. If the page does not establish geography, return `null` for `geography`.
- Return ONLY the JSON. No prose before or after.
