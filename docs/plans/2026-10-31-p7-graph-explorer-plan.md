# P7 — Ontology Graph Explorer: Implementation Plan v1 (2026-10-31)

Anchor audit: docs/audits/2026-10-31-p5-p7-stage-a-audit.md (G-P7: @xyflow/react
installed-unused; read endpoints exist but no aggregate; no graph UI).

## Invariants (carry over from the program)
- G1: per-viewer honesty — object nodes carry the SAME document-provenance
  filtering as /ontology/objects (doc_ctx per viewer, fail-closed).
- G2: org scoping — one endpoint, `_org_or_404` rules; no cross-org leak in any
  payload section (objects, links, bindings, metrics).
- G3: read-only — the graph endpoint never mutates (no ensure/seed side effects).
- G4: no fake data — evidence_status, lifecycle, reviewed_by are rendered as-is;
  metrics include draft/retired so governance state is visible, never implied
  published.

## Tasks

### P7-T1 — GET /api/ontology/graph (TDD, test_ontology_graph.py)
Aggregate read payload in routers/ontology.py:
`{org_id, domains[{id, code, name, object_count}], objects[object_to_dict w/
doc_ctx], links[link_to_dict], bindings[binding_to_dict], metrics[{
id, name, lifecycle, version, evidence_status, reviewed_by, base_table,
datasource_id, unit, aggregation}] (≤200, org-scoped, all lifecycles)}`.
Optional `domain_id` filter narrows objects+links only (domains/metrics stay
whole-org). Tests: shape, per-viewer document filtering (owner sees doc
evidence, missing-KB entry fail-closed hidden), cross-org isolation, optional
domain filter, plain authenticated member access.

### P7-T2 — Frontend OntologyGraph section (@xyflow/react 12)
New self-contained `frontend/src/components/ontology/OntologyGraph.jsx`:
- fetch `/api/ontology/graph` via authFetch; loading/error/empty states.
- object nodes clustered per domain (deterministic layout, domain-colored),
  edges = ontology links (label = link_type); evidence_status shown as node tint.
- node click → Drawer (components/ui/drawer.jsx) with full object payload:
  EvidenceBadge-style fields, provenance incl. document evidence, backing ref,
  last_validated_at/binding_status (P6 health) when present.
- Governed-metrics strip: name + lifecycle/evidence/reviewed_by badges
  (G4), sourced from the same payload.
- NO dependency on foreign-untracked libs (avoids @/lib/evidenceStatuses —
  inline the tiny style map locally).
Mount as a new section in pages/EnterpriseProfile.jsx (tracked-clean file)
between the Coverage Map and ObjectsPanel.

### P7-T3 — Verification
Backend: pytest ring. Frontend: `npm run build` in frontend/ (or at minimum a
vite build of the touched entry) + manual visual check note.
