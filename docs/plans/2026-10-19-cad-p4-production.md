# CAD P4 — CAD-to-Production Implementation Plan

**Goal:** BOM generation from the built model + manufacturing notes, and the
documented handoff to the approval-gated procurement quote pipeline.

**v1 scope:**
1. `app/services/dfm/bom.py` — pure: normalize/dedupe BOM rows, totals,
   warnings (missing material/mass), `mfg_notes()` (setups, ops, finishing,
   DFM repairs outstanding).
2. Tool `cad_bom_generate` (cad toolset) — components from the build →
   normalized BOM JSON + HTML artifact (BOM table + mfg notes) via _save_file.
3. SKILL.md: P4 flow — after quote: BOM → (user approval) → create_quote_draft
   (ontology action; est_unit_price = quote.unit_cost, est_amount = total_mid).
4. Restart + full regression.
