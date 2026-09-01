---
name: incident-review-guide
description: Use when writing incident reviews, postmortems, RCAs, or analyzing production outages — keywords like postmortem, incident report, root cause analysis, 5 Whys, blameless review. Produces structured SRE-style incident reports with timeline, root cause analysis, action items, and lessons learned.
---

# Incident Review Guide

Blameless postmortem and incident review writing based on SRE best practices.

## When to use

- "Write a postmortem / incident report / RCA"
- "Analyze this production outage"
- Keywords: postmortem, incident report, root cause analysis, 5 Whys, blameless
- Any reliability incident that needs a structured writeup

## Core principles

- **Blameless by default** — the report analyzes systems and processes, not people. Never name individuals as the cause; name the conditions that allowed the failure.
- **Facts over narrative** — every claim about what happened is tied to evidence (logs, metrics, timestamps, PRs, alerts).
- **Actionable** — every root cause has an owner + deadline + verification method, or it is not closed.

## Report structure

1. **Summary** — one paragraph: what happened, impact, duration, severity, status (Resolved/Monitoring)
2. **Impact** — user-facing and internal impact: error rates, latency, revenue, affected components, duration in minutes/hours
3. **Timeline** — chronological, timezone-stamped: detection, escalation, mitigation steps, resolution. Include what was KNOWN at each point, not just what we know now.
4. **Root cause analysis — 5 Whys** — chain from symptom to root cause, each "why" grounded in evidence. If multiple causes, separate chains.
5. **Contributing factors** — conditions that amplified or allowed the failure (missing alert, no runbook, config drift, single point of failure)
6. **Trigger** — the specific event that started the chain
7. **Detection & response gaps** — how long to detect, how long to mitigate, what slowed response
8. **Action items** — table: Action | Type (prevent/mitigate/detect/process) | Owner | Due | Verification
9. **Lessons learned** — 3-5 generalizable lessons for the team
10. **Appendix** — relevant dashboards, links, data

## Timeline writing rules

- Use exact timestamps; convert to a single timezone
- Separate "observed" facts from "interpreted" facts inline
- Include the human response loop explicitly (who noticed, who escalated)

## Pitfalls

- Never write "human error" as a root cause — dig to the system condition that made the error possible
- Don't assign action items without owners and due dates; an action without an owner is a wish
- Keep severity/status language precise — use the team's actual severity taxonomy
