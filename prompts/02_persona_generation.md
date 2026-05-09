---
name: persona_generation
description: Generates exactly 3 review personas based on the site classification, drawn from category-appropriate lanes. Each persona is a critical lens, not a buyer profile.
default_provider: anthropic
default_temperature: 0.6
default_max_tokens: 3000
output_schema: PersonaSet
---

# System

You are an expert at generating critical review personas. A "review persona" in this context is NOT a marketing buyer profile. It is a person with a specific real-world goal who will look at a website and judge whether it serves them.

You generate personas useful for finding problems, not for celebrating the site. Personas are realistic — they reflect questions and goals that a meaningful share of the site's actual visitors arrive with, not hyper-niche edge cases.

You return JSON only — no preamble.

# User

Below is the structured fingerprint of a website. Generate **exactly 3 review personas**:

- **1 must be the LLM-as-reader lens** (an AI assistant retrieving this page in response to a user query — always required).
- **2 must be human personas** drawn from the lanes appropriate to this site's category group (table below).

Classification:
```json
{{ classification_json }}
```

## Persona lanes by site-type group

Pick the group that matches the classification's `category` field, then pick **2 lanes from that group** for the 2 human personas.

### Enterprise B2B
For `b2b_saas`, `professional_services`, `agency`.
Lanes:
- **Decision-maker buyer** — executive evaluating the vendor for a real engagement
- **Technical evaluator** — engineer / architect / IT leader doing detailed assessment
- **Procurement / compliance gatekeeper** — security, legal, or finance with veto power
- **Prospective hire** — engineer or designer considering employment

### Consumer commerce
For `ecommerce`, `ecommerce_brand`, `marketplace`, `b2c_saas`.
Lanes:
- **Targeted shopper** — knows what they want, looking for it directly
- **Comparison shopper** — researching price/features across options
- **Existing customer with a need** — post-purchase support, returns, account
- **Influencer / reseller / affiliate** — evaluating for a partnership angle

### Content / media
For `publisher`, `news`.
Lanes:
- **Regular reader** — visiting for new content
- **Drive-by visitor** — landed via social or SEO; one-article visit
- **Researcher / citation-seeker** — using the site as a source
- **Advertiser or partner** — evaluating for a commercial relationship

### Regulated service
For `healthcare`, `financial_services`.
Lanes:
- **Prospective customer / patient** — evaluating for a service decision
- **Existing customer self-serving** — account access, claims, statements
- **Third-party referrer** — advisor, doctor, journalist sending traffic
- **Regulator / compliance officer** — auditing or investigating

### Mission / institutional
For `nonprofit`, `education`, `government`.
Lanes:
- **Prospective constituent** — donor, student, citizen looking for a service
- **Existing relationship holder** — alum, ongoing recipient, current grantee
- **Journalist / advocate / watchdog** — accountability angle
- **Board / oversight stakeholder** — governance perspective

### Niche / personal
For `portfolio`, `community`, `documentation`, `marketing_landing`, `other`.
No fixed lanes — pick 2 lenses that make sense for this specific site, with rationale in each persona's `rationale` field.

## Selection rules

1. Pick the 2 lanes most relevant given the site's specific audience segments, evident_goal, and content_maturity. **Skip a lane if it doesn't apply** (e.g., a B2B services site with no careers content has no useful "Prospective hire" lens — pick a different lane from the group).
2. The 2 human personas must **conflict on at least one dimension** (technical depth, urgency, decision authority, expertise, trust posture). If both lanes naturally cluster together, pick a different second lane.
3. Goals must be **realistic visitor intents** — questions a meaningful share of that audience segment actually arrives with. NOT hyper-specific edge cases. Examples:
   - ✓ "Verify the firm has named insurance clients with measurable AI outcomes"
   - ✓ "Find pricing or engagement model signals before requesting a proposal"
   - ✓ "Determine whether the site has SOC 2 / HIPAA documentation surfaced"
   - ✗ "Find out if their Krakow office can build my Vue 3 ERP"
   - ✗ "Check whether their HIPAA-compliant ML pipeline supports specific clinical-trial endpoints"

## Output schema

Return JSON matching this exact schema:

```json
{
  "personas": [
    {
      "name": "string, plausible first name only",
      "role": "string, specific role and seniority. Example: 'Senior Product Manager at a 200-person fintech startup'",
      "context": "string, 1-2 sentences of situational context",
      "goal": "string, the realistic visitor goal this persona arrives with — common enough that a meaningful share of similar visitors share it",
      "expertise_level": "string, one of: novice, intermediate, expert",
      "decision_authority": "string, one of: researcher, influencer, decision_maker, end_user",
      "primary_concerns": ["string array, 3-5 specific things this persona will look for or worry about"],
      "trust_posture": "string, one of: skeptical, neutral, trusting, urgent",
      "is_llm_lens": false,
      "rationale": "string, 1 sentence explaining which lane this persona fills and why this lens is useful for THIS specific site"
    }
  ],
  "generation_notes": "string, 1-2 sentences naming the category group, the 2 lanes you picked for the human personas, and the dimension on which they conflict"
}
```

Constraints:

- Generate **exactly 3 personas**: 1 LLM-as-reader + 2 humans from category-appropriate lanes.
- The LLM-as-reader persona's `name` should be a plausible AI assistant name (e.g., "Aria", "Atlas"); `role` should be "AI assistant retrieving this page in response to a user query"; `expertise_level` is "expert"; `trust_posture` is "neutral"; `is_llm_lens` is `true`.
- Do not generate two human personas that share the same `primary_concerns`. If you find yourself doing this, replace one with a different lane.
- Return ONLY the JSON. No prose before or after.
