# Phase 3.3 — Multilingual Enterprise Intelligence Design (2026-10-31)

## Root cause

`classify_failure` matched English substrings only — "查询超时" fell to
`unknown_error` (which changes retry semantics). `capability_fallback`
tokenizer was regex `[a-z0-9_]+` — CJK keywords never matched. Canonical
enums stay English (correct); the CLASSIFICATION INPUTS and PRESENTATION
were English-only.

## Design

1. **Classification inputs** — Chinese error markers merged into the existing
   `_MARKERS` tuples (programmatic `_MARKERS_ZH` merge; dict-literal duplicate
   keys would silently WIPE the English tuples — the regression that caught
   this is documented in the roadmap). Precedence unchanged
   (`_MARKER_ORDER`: permission first, timeout before generic connection).
   Categories covered: permission_denied, unsupported_operation, rate_limit,
   timeout, connection_error, invalid_arguments, missing_required_parameter,
   malformed_output, database_error, resource_not_found.
2. **Presentation** — `localized_recovery_hint(category, lang)` returns zh/en
   recovery hints; the default stored hint stays English. Localization is a
   presentation-boundary concern (SSE/事件流), never persisted into
   classifications. SSE `LIVE_EVENT_TEMPLATES` were already bilingual.
3. **Capability matching** — `capability_fallback._tokens` now extracts CJK
   character bigrams (no dictionary segmentation dependency), so Chinese
   keywords ("产量", "库存") match Chinese agent descriptions.

## Equivalence guarantee (tested)

For 9 paired (zh, en) error strings: identical `FailureCategory`, identical
`retryable`/`terminal` flags, identical `evaluate_observation` decision.
Chinese capability keywords select the same specialist as their English
equivalents.
