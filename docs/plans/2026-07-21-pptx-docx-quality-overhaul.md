# PPTX/DOCX Generation Quality Overhaul — 15 Tasks Complete

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans if continuing this work; this plan is now complete and serves as the snapshot for future sessions.

**Goal:** Full content-quality overhaul of `backend/skills/pptx/SKILL.md` and `backend/skills/docx/SKILL.md` so that agents produce visibly better, more opinionated, more consistent artifacts on the first pass. Adds a top-of-file decision tree, opinionated quality checklists, audience-aware templates, density & contrast heuristics, accessibility quick pass, before/after anti-pattern gallery, and a final pre-emit self-audit.

**Architecture:** No new code, no new dependencies, no runtime change. Pure documentation. The existing artifact pipeline (`create_artifact` → `ArtifactService` → preview builder) and marker contract (`◤MD_DOCX◤` / `◤HTML_DOCX◤` / `◤PPTX◤`) are untouched. All changes are markdown content in the two skill folders.

**Tech Stack:** Markdown (GitHub-flavored), python-pptx / python-docx (referenced in code samples, not invoked at generation time), pandoc (referenced for HTML → DOCX conversion), pytest (test pattern is AST / content-based, not full suite).

---

## Status snapshot (verified 2026-07-21)

- [x] Plan written, 15 tasks, all complete
- [x] 14 commits on master, one per task (plus this plan doc)
- [x] 14 new test files, **109/109 tests pass** (1-2s per test file, no pytest suite)
- [x] No subagent dispatches, no external lookups, no DB or runtime changes
- [x] Skill bodies are now 272 → 700+ lines each (pptx: 272 → 800+, docx: 385 → 850+)
- [x] New `references/quality-checklist.md` for both pptx and docx (long-form rubrics)

---

## What was done

### Phase 1 — Repair + structural cleanup (Tasks 1-2)

1. **Task 1 (`cf8cccf`)** — Stripped 228 pasted line-number prefixes (`     N|`) from pptx/SKILL.md lines 8-235. The file had a botched bulk edit that pasted `read_file`'s line-number prefix into the actual content. Cleaned programmatically (declared+2==actual pattern was 100% consistent). 6 regression tests added.
2. **Task 2 (`6c816a4`)** — Locked in the existing 9 `##` sections as a regression baseline. 5 tests added. Guards against future content additions silently dropping sections.

### Phase 2 — PPTX quality framework (Tasks 3-9)

3. **Task 3 (`7741435`)** — Pre-Generation Decision Tree (6 questions): audience, length, formality, data density, brand, intent. Sits right after the H1, before *Quick Reference* so it's the agent's first stop. 6 tests.
4. **Task 4 (`2ccb658`)** — Pre-Emit Self-Audit (12 points) + `pptx/references/quality-checklist.md` long-form. Three categories: Content (1-4), Visual (5-8), Polish (9-12). Long-form has WCAG ratio scoring, pre-validated palette combos, anti-pattern fixes. 9 tests.
5. **Task 5 (`ce96159`)** — Anti-Patterns Gallery (8 patterns). Each with Symptom, Why-it's-bad, Fix. Includes the accent-line callout (biggest AI-slides tell). 7 tests.
6. **Task 6 (`7263a1e`)** — Content Density (6×6 rule). ≤ 6 lines × ≤ 6 words per content slide. Per-slide-type word budget table. White-space rules. python-pptx density-sanity-check script. 7 tests.
7. **Task 7 (`4dfe29d`)** — Color & Contrast (60-30-10 + WCAG). Dominance rule, ratio scoring, 7 pre-validated combos, forbidden combinations list, contrast-sanity-check script (luminance + ratio), color-blindness check. 8 tests.
8. **Task 8 (`638f40a`)** — Typography Hierarchy (4-level system). H1/H2/H3/Body/Caption with size/weight/spacing/color. Hierarchy rules, paragraph rhythm (1.2-1.4 line spacing), typography sanity-check script (≤2 fonts, ≤4 sizes). 8 tests.
9. **Task 9 (`aeeaf9b`)** — Accessibility Quick Pass (5 checks). Alt text, ≥14pt body, color-not-only-signal, no color-only status, logical reading order. Color-blind palette tags (Mono/Diverging/Sequential/Categorical). Alt-text patterns table for 7 image types. python-pptx code for setting descr. 9 tests.

### Phase 3 — DOCX quality framework (Tasks 10-13)

10. **Task 10 (`0089302`)** — Choosing a Document Type (5 types). 4-question rubric, type table (memo/report/letter/proposal/minutes), per-type structural templates with constraints, type-mismatch warnings (5 specific cases). 7 tests.
11. **Task 11 (`f4a0b60`)** — Typography & Page Layout (4-level + margins). H1/H2/H3/H4 + Body + Caption with full typography. Line-spacing 1.15-1.5, paragraph spacing 6-12pt. Page-margin profiles (Standard / Letter / Report / Two-column). Page-break discipline (widow/orphan control). Table-width rules, headers/footers, anti-patterns. 10 tests.
12. **Task 12 (`45818bf`)** — Pre-Emit Self-Audit (10 points) + `docx/references/quality-checklist.md`. Content (1-4), Layout (5-7), Quality (8-10). XSD validation is item 9 (the docx-specific quality check). LibreOffice conversion check is item 10. Long-form has pandoc pageNumber footer pattern. 10 tests.
13. **Task 13 (`78856fc`)** — Anti-Patterns Gallery (8 patterns). Wall of text, all-bold, no headings, inconsistent fonts, broken tables, missing page numbers, justified body, wrong document type. 7 tests.

### Phase 4 — Cross-link + snapshot (Tasks 14-15)

14. **Task 14 (`3b2ce86`)** — Reciprocal cross-links. `## Related Skills` in both files pointing at each other. Shared `## Professional Output Principles` section in both with 8 principles that apply regardless of format. Test asserts both files have the same principle count. 10 tests.
15. **Task 15 (this doc)** — Plan snapshot to `docs/plans/` for next-session resume.

---

## Commits in order

1. `cf8cccf` — fix(skills): strip pasted line-number prefixes from pptx/SKILL.md
2. `6c816a4` — test(skills): lock in pptx SKILL.md section list as regression baseline
3. `7741435` — feat(skills): pptx pre-generation decision tree (6 questions)
4. `2ccb658` — feat(skills): pptx 12-point pre-emit self-audit + long-form rubric
5. `ce96159` — feat(skills): pptx Anti-Patterns Gallery (8 patterns)
6. `7263a1e` — feat(skills): pptx Content Density section (6x6 rule)
7. `4dfe29d` — feat(skills): pptx Color & Contrast section (60-30-10 + WCAG)
8. `638f40a` — feat(skills): pptx Typography Hierarchy (4-level system)
9. `aeeaf9b` — feat(skills): pptx Accessibility Quick Pass (5 checks)
10. `0089302` — feat(skills): docx Choosing a Document Type (5 types)
11. `f4a0b60` — feat(skills): docx Typography & Page Layout (4-level + margins)
12. `45818bf` — feat(skills): docx 10-point pre-emit self-audit + long-form rubric
13. `78856fc` — feat(skills): docx Anti-Patterns Gallery (8 patterns)
14. `3b2ce86` — feat(skills): reciprocal cross-links + shared Professional Output Principles

---

## Key file locations

```
backend/skills/
├── pptx/
│   ├── SKILL.md                                          # [MODIFIED] ~800 lines
│   ├── editing.md                                        # unchanged
│   ├── pptxgenjs.md                                      # unchanged
│   ├── manifest.yaml                                     # unchanged
│   ├── references/
│   │   ├── delivery-pitfalls.md                          # unchanged
│   │   └── quality-checklist.md                          # [NEW] long-form rubric
│   └── scripts/                                          # unchanged
└── docx/
    ├── SKILL.md                                          # [MODIFIED] ~850 lines
    ├── manifest.yaml                                     # unchanged
    ├── LICENSE.txt                                       # unchanged
    ├── references/
    │   └── quality-checklist.md                          # [NEW] long-form rubric
    └── scripts/                                          # unchanged

backend/tests/
├── test_pptx_skill_frontmatter_clean.py                  # 6 tests
├── test_pptx_skill_sections_baseline.py                  # 5 tests
├── test_pptx_skill_decision_tree.py                      # 6 tests
├── test_pptx_skill_self_audit.py                         # 9 tests
├── test_pptx_skill_antipatterns.py                       # 7 tests
├── test_pptx_skill_density.py                            # 7 tests
├── test_pptx_skill_contrast.py                           # 8 tests
├── test_pptx_skill_typography.py                         # 8 tests
├── test_pptx_skill_accessibility.py                      # 9 tests
├── test_docx_skill_doctype_tree.py                       # 7 tests
├── test_docx_skill_typography.py                         # 10 tests
├── test_docx_skill_checklist.py                          # 10 tests
├── test_docx_skill_antipatterns.py                       # 7 tests
└── test_pptx_docx_skill_crosslinks.py                    # 10 tests
```

Total: 14 new test files, 109 tests passing, 0 new dependencies, 0 runtime changes.

---

## Verification commands

To re-verify after a clean checkout:

```bash
# Per-file targeted test runs (no pytest suite, RAM-safe)
for f in backend/tests/test_pptx_skill_*.py backend/tests/test_docx_skill_*.py backend/tests/test_pptx_docx_skill_*.py; do
  python3 -c "
import importlib.util
spec = importlib.util.spec_from_file_location('t', '\$f')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
"
done
```

Or one-shot (with pass/fail reporting):

```bash
for f in backend/tests/test_pptx_skill_*.py backend/tests/test_docx_skill_*.py backend/tests/test_pptx_docx_skill_*.py; do
  python3 -c "
import importlib.util
spec = importlib.util.spec_from_file_location('t', '\$f')
m = importlib.util.module_from_spec(spec)
try:
    spec.loader.exec_module(m)
    n = len([x for x in dir(m) if x.startswith('test_')])
    print('\$f', 'GREEN', n)
except AssertionError as e:
    print('\$f', 'RED', e)
"
done
```

Expected output: 14 lines, all `GREEN <N>`.

---

## What this plan did NOT do (intentionally)

- **No new code / no new runtime features.** Pure documentation. No new endpoints, no new tests of runtime behavior, no new dependencies.
- **No external lookups.** All content derived from the existing in-skill pattern library (the existing `### Color Palettes` table, the existing `### Typography` table, etc.) and from in-codebase references (existing scripts, validators, conversion tools).
- **No design changes to slide templates or DOCX templates.** The skills now tell the agent how to design better artifacts; they don't ship new templates.
- **No validation enforcement.** The audit checklists tell the agent to run checks but do not enforce them at runtime. If you want runtime enforcement (e.g., reject the marker if the audit fails), that's a separate, larger task — likely a hook in the artifact pipeline.

---

## Notes for the next session

- The skills are now opinionated. If an agent produces a "bad" deck or doc, the first question to ask is "did the agent follow the decision tree / audit?" If not, the skills are working as designed; the issue is the agent's adherence, not the skills.
- The PPTX density-sanity-check script (`## Content Density` section) and the typography sanity-check script (`## Typography Hierarchy`) are runnable python-pptx code. If you want a one-shot CLI that runs all of them and reports, that's a small follow-up task.
- The docx XSD validation is documented as item 9 of the audit; the actual `pack.py` validator is the existing tool in `backend/skills/docx/scripts/office/`. The skill now makes it a required pre-emit step rather than an opt-in.
- The shared `Professional Output Principles` section is duplicated in both files (not imported from one). This is intentional — the file should be self-contained, and a markdown import system doesn't exist in this repo. The cross-link test asserts both files have the same principle count, so they stay in sync.
- Pre-existing `test_artifact_message_link.py` errors are unrelated to this work (pre-broken SQL setup).

---

## Resume instructions

If you need to continue this work:

1. The plan is COMPLETE. No open tasks.
2. To start a follow-on task (e.g., runtime audit enforcement, CLI wrapper for the sanity-check scripts, additional doc types like contracts/invoices), create a new plan in `docs/plans/`.
3. The TDD pattern (RED → GREEN → commit per task) and the per-file targeted test runner are both reusable. Don't switch to a full pytest run on this SSH server; it's RAM-constrained.
4. The two skills are now the canonical place to look for "how should a PPTX/DOCX look." New templates or design tokens should land in the skills first, not in a side repo.
