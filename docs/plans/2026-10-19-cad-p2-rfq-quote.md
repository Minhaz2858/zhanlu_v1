# CAD P2 — RFQ → Design → DFM → Quote (MVP wedge) Implementation Plan

**Goal:** the commercial loop: requirement → (model via existing pipeline) →
DFM (P1) → deterministic cost estimate → quote-package artifact, human-approval
gated.

**Architecture decisions (v1):**
- **Material/process catalog = code constants** (`dfm/quote.py`), reusing the
  P1 material profiles for density/stock. Per-tenant DB tables come later —
  v1 optimizes for determinism + testability.
- **Stateless tools.** The agent does intake/clarify in conversation (goal
  contract machinery already handles clarifying questions); the deterministic
  surface is ONE tool, `rfq_quote`, which (a) validates requirement
  completeness and names exactly what's missing, (b) computes the quote, (c)
  emits an HTML `quote_package` file (draft_pending_approval). Traceability
  rides on AgentRunStep + the saved file.
- Mass comes from `fusion360_physical_properties` (probe) or volume×density —
  the tool REQUIRES mass_g or volume_cm3 and refuses to guess.

Tasks:
1. `backend/tests/test_dfm_quote.py` — RED → `backend/app/services/dfm/quote.py` GREEN
   (cost math: material×waste, per-feature time, setup amortization, ±20% range, quantity scaling, assumptions list)
2. `backend/tests/test_rfq_quote_tool.py` — RED →
   `backend/app/services/tool_handlers/rfq_quote.py` GREEN (validation,
   missing-input clarifications, HTML package emission via _save_file, registry)
3. Restart + full regression suite (CAD + DFM + guards)
