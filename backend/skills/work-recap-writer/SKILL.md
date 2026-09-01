---
name: work-recap-writer
description: Use when the user mentions a weekly report, monthly report, work summary, sprint summary, status update, OKR progress, or needs to organize scattered records (notes, git logs, chat messages) into a formal recap. Produces structured recaps in data-driven, narrative, or OKR-aligned styles. Triggers on keywords: 周报, 月报, 工作总结, 工作汇报, 周总结.
---

# Work Recap Writer

Turn scattered work notes and git logs into structured weekly or monthly reports in data-driven, narrative, or OKR-aligned styles.

## When to use

- "Weekly report / monthly report / work summary"
- "Sprint summary / status update / OKR progress"
- "Organize my scattered records into a recap"
- User provides notes, git logs, chat messages, tickets — needs a formal recap

## Workflow

1. **Gather materials** — notes, git logs, tickets, chat messages, commits, PRs, metrics. If the user points at a repo, extract meaningful commit summaries (not raw hashes).
2. **Ask/identify the style** — three supported styles:
   - **Data-driven**: metrics, numbers, trends, charts (best for recurring status)
   - **Narrative**: story of the period — context, work, outcomes (best for stakeholder updates)
   - **OKR-aligned**: progress against objectives/key results (best for planning cycles)
3. **Extract the work** — per theme: what was done, what was completed vs in-progress, blockers, outcomes, next steps.
4. **Compose** using the style's structure (below).
5. **Verify** — every metric comes from the materials; don't invent numbers. Mark estimates as estimates.

## Structures by style

### Data-driven
1. Period & headline metrics (3-5 KPIs with deltas)
2. Metrics table per workstream
3. Trend chart(s) where data allows
4. Notable changes & what drove them
5. Risks & watch items

### Narrative
1. Period overview (what the period was about)
2. Key accomplishments (3-5, each: what + why it matters)
3. Challenges & how they were handled
4. Learnings
5. Next period focus

### OKR-aligned
1. Objective list with current confidence
2. Per objective: key results with progress % (evidence-linked)
3. Blockers per objective
4. Plans for next period
5. Help needed (if any)

## Pitfalls

- Never invent metrics — if the materials don't contain a number, say "not tracked"
- Distinguish completed vs in-progress precisely — vague status language gets corrected
- Git logs: summarize by theme (feature/bugfix/chore), don't list every commit
- Match the audience: data-driven for managers, narrative for execs, OKR for planning reviews
