# Forecast Analyst — Smarter Decisions + LLM Brief Review

**Date:** 2026-08-04
**Status:** Confirmed by user (both decision-first, LLM analyst + evidence pack, all three surfaces)
**Trigger:** PRODUCT_B Decision Board brief was empty: "建议观望,等待更明确的信号(置信度:低)" with dashes for directional accuracy / fluctuation state — no *why*, no *what to watch*.

## Problem

The Decision Board's AI简评/AI建议 is 100% deterministic template strings (`forecasting/narrative.py`, `decision_board_service.py`). It can state numbers but never *reason*:

1. **No causal explanation** — cannot say *why* price moves (upstream transmission, seasonality, model drivers).
2. **Silent at low trust** — `decision_engine.py` gates on trust tier; when low, narrative collapses to "watch" + dashes, never explaining *why trust is low* or *what would change the call*.
3. **Rich evidence computed but unused** — XGBoost top-5 drivers (`explain.py`), price-chain percentiles (`get_price_chain()`), scenarios, SKU forecasts, seasonal stats all exist but none reach the narrative.
4. **Dormant domain brain** — `domain_signals.py` (product_c→sample product→downstream elasticities, monthly seasonality, ported from Acme) is gated off (`FORECAST_DOMAIN_SIGNALS_ENABLED`, default False).
5. **forecast_agent chat is disconnected** — the board never uses the LLM agent; chat answers don't share the board's evidence.

## Design Decisions (user-confirmed)

| Question | Decision |
|---|---|
| Scope | Both: decision quality first, then explanation quality |
| Narrative engine | LLM analyst + evidence pack (deterministic pack → LLM writes → verifier → cache) |
| Surfaces | Decision Board + forecast_agent chat + weekly report |
| Honesty boundary | **LLM explains, never decides.** All decisions remain 100% deterministic. |

## Architecture

One shared "analyst brain" (evidence pack + brief writer), three mouths (board / chat / weekly report).

```
Forecast Engine (existing, deterministic)
  → analyst/evidence_pack.py   (pure assembler, no LLM)
  → analyst/brief_writer.py    (LLM → validated JSON)
  → analyst/output verifier    (every number must exist in pack)
  → brief cache (key: product + day + forecast_run_id)
       ├→ Decision Board AI简评/AI建议
       ├→ forecast_agent chat (new forecast_brief tool)
       └→ weekly report product sections
Fallback: LLM timeout/error/verify-fail → enriched deterministic template.
```

## Section 1 — Decision smartening (substrate)

Evidence pack assembler pulls signals that exist but are unused/dormant:

1. **Domain signals enablement** — validate `domain_signals.py` via existing `domain_signals_ab.py` A/B harness; enable per-product where it beats baseline (flag stays per-product configurable).
2. **Price-chain divergence signal** (new) — `get_price_chain()` upstream 30d moves + 3yr percentiles; compute elasticity-implied move vs model forecast. Agreement → confidence support; divergence → explicit risk flag.
3. **Model agreement metric** — spread across 8 models' point forecasts; wide disagreement → lower confidence + narrative "模型间分歧较大".
4. **"Why low trust" reasons** — trust tier emits machine-readable reasons (short history / below naive baseline / high volatility / drift) instead of a bare label. Directly fixes the PRODUCT_B dashes case.
5. **Surface existing drivers** — XGBoost top-5 features (already in `explain.py`) + seasonal position ("8月为传统需求淡季") into the pack.

Decision engine thresholds (`p_rise ≥ 0.70` etc.) unchanged — we enrich evidence/explanation, not the gate.

## Section 2 — LLM brief writer

- **Input:** evidence pack JSON only. Strict analyst contract: "You are a industrial market analyst. Use ONLY the provided evidence. Every number must come from the evidence. Output JSON."
- **Output schema:** `{summary_zh, why_price_zh, why_action_zh, watch_triggers_zh[], risk_zh}` — structured sections, not one blob.
- **Verifier:** extract every number in LLM output, check it exists in pack (±tolerance). Hallucinated number → discard → template fallback. Anti-bullshit gate.
- **Caching:** keyed by `product + day + forecast_run_id`. New run invalidates; page loads cost zero LLM calls.
- **Fallback chain:** LLM timeout/error/verification-fail → enriched template (Section-1 pack rendered by much better deterministic templates — a big upgrade even without LLM).
- **Feature flag:** `FORECAST_ANALYST_LLM_ENABLED` (default off).

Example output for the PRODUCT_B case (same decision, now reasoned):

> 材料B现价 6,940 元/吨,处于近三年约 42% 分位。上游分解 sample product 近 30 日下跌 2.8%,按 0.66 弹性传导约 -1.9%,与模型预测 -4.0% 方向一致但幅度更深,8 模型间分歧偏大(区间 6,580–6,790)。当前为 8 月传统需求淡季,方向预测历史准确率未达 55% 显著门槛,故可信度低、建议观望。若上游 sample product 止跌或方向准确率回升至 55% 以上,将重新评估为可操作信号。

## Section 3 — Surfaces

- **Decision Board:** `build_decision_board()` reads cached brief (inline template fallback). Dashes filled: directional accuracy shows number + gating reason; "0 intelligence events" → "近期无重大市场事件,价格主要由供需基本面驱动".
- **forecast_agent chat:** new `forecast_brief` tool returning pack + cached brief — chat answers from the same brain, consistent with board.
- **Weekly report:** product sections call the same writer (batch, one LLM call per covered product), replacing thin template paragraphs.

## Section 4 — Testing & rollout

- Pack builders are pure functions → unit tests per signal (pattern of existing `narrative.py` tests).
- Writer tested with mocked LLM; verifier tested with adversarial outputs (invented numbers, wrong direction).
- Domain-signals enablement validated through A/B harness before flipping flags.
- Rollout: `FORECAST_ANALYST_LLM_ENABLED=false` default → enable on dev → verify board/chat/weekly → prod. Docker restart per usual workflow (`docker restart zhanlu-backend`, no --reload).

## Out of scope

- Changing decision thresholds or the buy/hold/sell/watch gate semantics.
- New data ingestion pipelines (intel ingestion improvements are a separate track).
- Real-time (uncached) LLM calls on page load.
