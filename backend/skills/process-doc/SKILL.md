---
name: process-doc
description: Use when formalizing a business process that lives in someone's head, building a RACI to clarify who owns what, writing an SOP for a handoff or audit, or capturing the exceptions and edge cases of how work actually gets done. Produces process flowcharts, RACI matrices, and SOP documents.
---

# Process Documentation

Document business process flowcharts, RACI matrices, and SOPs — turning tacit knowledge into written, auditable process.

## When to use

- "Document our process", "formalize how we do X"
- Building a RACI to clarify ownership
- Writing an SOP for a handoff or audit
- Capturing exceptions and edge cases of how work actually gets done

## Approach

1. **Elicit the process** — interview the user (or read the materials) to map the ACTUAL flow, not the ideal one. Ask: What happens first? Who does it? What do they need? What can go wrong? What happens then?
2. **Map the happy path** — linear steps with clear inputs/outputs per step.
3. **Map branches & exceptions** — decision points, error paths, edge cases, fallbacks. Real processes live in the exceptions.
4. **Assign ownership (RACI)** — for each activity, classify: Responsible / Accountable / Consulted / Informed. One accountable per activity; avoid R=A overlap where possible.
5. **Write the SOP** — numbered steps, roles, tools, timing, quality criteria, escalation path.
6. **Validate** — walk the doc against the real flow; flag gaps and open questions for the user.

## Document formats

### Process flowchart
- Visual diagram (SVG/HTML) with swimlanes by role, decision diamonds, explicit start/end
- Every step numbered and referenced in the SOP

### RACI matrix
| Activity | Role A | Role B | Role C | ... |
|---|---|---|---|---|
| Step 1 | R | A | C | |
- One and only one "A" per activity
- "R" can be multiple; "A" should be singular
- Add a legend explaining each letter and the default for unlisted roles ("I by default")

### SOP
1. Purpose & scope
2. Roles & responsibilities (RACI summary)
3. Prerequisites / inputs
4. Step-by-step procedure (numbered, with tools and expected outcomes)
5. Exceptions & edge cases
6. Quality criteria / definition of done
7. Escalation path
8. Revision history

## Pitfalls

- Document the real process, not the aspirational one — note where reality deviates and needs fixing
- Don't leave "miscellaneous" steps — every step needs an owner and an outcome
- RACI: never two A's; if two people feel accountable, the process has an ownership bug — surface it
