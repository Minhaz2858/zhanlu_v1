"""Forecast reasoning narratives — pure builders (no I/O).

Shared by:
  - forecast chart API (chart-page analysis panel)
  - decision_board_service.build_narrative       (decision-board AI 简评)

Everything here is a pure function of its inputs so it is trivially
unit-testable and stays template-based (no LLM calls).

Narrative structure (zh):
  【预测依据】 trend / model-ensemble / uncertainty evidence
  【建议逻辑】 why this action, what threshold was or was not met,
               and what would change the recommendation.
"""
from __future__ import annotations

from typing import Optional

# Decision-engine thresholds (mirrored from decision_engine.py so narrative
# text can reference them without importing the engine module).
BUY_P_THRESHOLD = 0.70
SELL_P_THRESHOLD = 0.30
BUY_MIN_CHANGE = 0.03
SELL_MIN_CHANGE = -0.03
EDGE_MIN_ACCURACY = 0.55

_CONFIDENCE_ZH = {"high": "高", "medium": "中", "low": "低"}


# ── Trend statistics ────────────────────────────────────────────────────

def compute_trend_stats(history_rows: list) -> dict:
    """Momentum stats from [(date_str, price), ...] (ascending date order).

    Returns keys: last_price, chg_7d_pct, chg_30d_pct, ma30, above_ma30.
    Any value may be None when the window is shorter than required.
    """
    prices = [float(v) for _d, v in history_rows if v is not None]
    out: dict = {
        "last_price": None,
        "chg_7d_pct": None,
        "chg_30d_pct": None,
        "ma30": None,
        "above_ma30": None,
    }
    if not prices:
        return out

    out["last_price"] = prices[-1]
    if len(prices) >= 8:
        prev = prices[-8]
        if prev:
            out["chg_7d_pct"] = (prices[-1] - prev) / prev
    if len(prices) >= 31:
        prev = prices[-31]
        if prev:
            out["chg_30d_pct"] = (prices[-1] - prev) / prev
    window = prices[-30:] if len(prices) >= 30 else prices
    if window:
        ma = sum(window) / len(window)
        out["ma30"] = ma
        out["above_ma30"] = prices[-1] >= ma
    return out


# ── Uncertainty statistics ──────────────────────────────────────────────

def compute_uncertainty_stats(horizon_payload: dict) -> dict:
    """Bull/bear spread stats from a horizon payload.

    Returns keys: base, bull, bear, spread_pct (=(bull-bear)/base).
    Uses the LAST point of each curve (end-of-horizon uncertainty).
    """
    out: dict = {"base": None, "bull": None, "bear": None, "spread_pct": None}
    if not isinstance(horizon_payload, dict):
        return out

    def _last(v):
        if isinstance(v, list) and v:
            return float(v[-1])
        if isinstance(v, (int, float)):
            return float(v)
        return None

    base, bull, bear = (
        _last(horizon_payload.get("base")),
        _last(horizon_payload.get("bull")),
        _last(horizon_payload.get("bear")),
    )
    out["base"], out["bull"], out["bear"] = base, bull, bear
    if base and bull is not None and bear is not None and base > 0:
        out["spread_pct"] = (bull - bear) / base
    return out


# ── Model-ensemble statistics ───────────────────────────────────────────

def compute_model_stats(model_detail: dict, below_naive_baseline: Optional[bool]) -> dict:
    """Ensemble composition + honesty stats from run.model_detail.

    Returns keys: model_count, model_names, ensemble_mape, naive_mape,
    beats_naive, below_naive_baseline.
    """
    md = model_detail or {}
    names = md.get("models_run") or []
    emape = md.get("ensemble_mape")
    nmape = md.get("naive_mape")
    beats = None
    if isinstance(emape, (int, float)) and isinstance(nmape, (int, float)):
        beats = emape < nmape
    return {
        "model_count": len(names),
        "model_names": list(names),
        "ensemble_mape": float(emape) if isinstance(emape, (int, float)) else None,
        "naive_mape": float(nmape) if isinstance(nmape, (int, float)) else None,
        "beats_naive": beats,
        "below_naive_baseline": below_naive_baseline,
    }


# ── Reasoning text (zh) ─────────────────────────────────────────────────

def _fmt_pct_signed(v: Optional[float], digits: int = 1) -> str:
    return "—" if v is None else f"{v * 100:+.{digits}f}%"


def _fmt_pct_plain(v: Optional[float], digits: int = 1) -> str:
    return "—" if v is None else f"{v * 100:.{digits}f}%"


def build_basis_zh(
    *,
    trend: dict,
    uncertainty: dict,
    models: dict,
) -> str:
    """【预测依据】 sentence: trend + ensemble + uncertainty evidence."""
    parts: list[str] = []

    # ① Trend
    chg7 = trend.get("chg_7d_pct")
    chg30 = trend.get("chg_30d_pct")
    above = trend.get("above_ma30")
    trend_bits: list[str] = []
    if chg7 is not None:
        trend_bits.append(f"近7日{_fmt_pct_signed(chg7)}")
    if chg30 is not None:
        trend_bits.append(f"近30日{_fmt_pct_signed(chg30)}")
    if above is not None:
        trend_bits.append("现价位于30日均线上方" if above else "现价位于30日均线下方")
    if trend_bits:
        parts.append("趋势:" + ",".join(trend_bits))

    # ② Model ensemble
    mc = models.get("model_count") or 0
    if mc > 0:
        m_bit = f"{mc} 模型集成"
        emape = models.get("ensemble_mape")
        nmape = models.get("naive_mape")
        if emape is not None and nmape is not None:
            rel = "优于" if models.get("beats_naive") else "弱于"
            m_bit += (
                f"(回测误差 {_fmt_pct_plain(emape)},{rel}朴素基线 {_fmt_pct_plain(nmape)})"
            )
        if models.get("below_naive_baseline"):
            m_bit += ",诚实门控已降级为保守预测"
        parts.append("模型:" + m_bit)

    # ③ Uncertainty
    spread = uncertainty.get("spread_pct")
    if spread is not None:
        level = "较小" if spread < 0.08 else ("适中" if spread < 0.18 else "较大")
        parts.append(f"不确定性:预测区间宽度 ±{_fmt_pct_plain(spread / 2)}({level})")

    return ";".join(parts) + "。" if parts else ""


def build_action_logic_zh(
    *,
    action: str,
    confidence: str,
    p_rise: Optional[float],
    expected_change_pct: Optional[float],
    directional_accuracy: Optional[float],
    directional_status: Optional[str],
    trust_tier: Optional[str],
) -> str:
    """【建议逻辑】 sentence: why this action + what would change it."""
    a = (action or "watch").lower()
    has_edge = (
        directional_status == "edge"
        and directional_accuracy is not None
        and directional_accuracy >= EDGE_MIN_ACCURACY
    )
    tier = (trust_tier or "low").lower()

    cond_bits: list[str] = []
    if p_rise is not None:
        cond_bits.append(f"上涨概率 {_fmt_pct_plain(p_rise, 0)}")
    if expected_change_pct is not None:
        cond_bits.append(f"预期{_fmt_pct_signed(expected_change_pct)}")

    if a == "buy":
        why = (
            f"满足备货信号(概率≥{_fmt_pct_plain(BUY_P_THRESHOLD, 0)} 且 "
            f"涨幅≥{_fmt_pct_plain(BUY_MIN_CHANGE, 0)} 且方向准确率达标)"
        )
        trigger = "若概率或准确率跌破阈值,将下调为观望"
    elif a == "sell":
        why = (
            f"满足出货信号(概率≤{_fmt_pct_plain(SELL_P_THRESHOLD, 0)} 且 "
            f"跌幅≥{_fmt_pct_plain(abs(SELL_MIN_CHANGE), 0)} 且方向准确率达标)"
        )
        trigger = "若概率回升或准确率下降,将下调为观望"
    elif a == "hold":
        why = "有方向优势但预期波动未达买卖阈值,建议按需跟进"
        trigger = (
            f"若涨幅超过 {_fmt_pct_plain(BUY_MIN_CHANGE, 0)} 且概率≥"
            f"{_fmt_pct_plain(BUY_P_THRESHOLD, 0)} 将升级为备货"
        )
    else:  # watch
        reasons: list[str] = []
        if tier == "low":
            reasons.append("可信度等级低")
        if not has_edge:
            if directional_accuracy is not None:
                reasons.append(
                    f"方向准确率 {_fmt_pct_plain(directional_accuracy, 0)} 未达"
                    f" {_fmt_pct_plain(EDGE_MIN_ACCURACY, 0)} 显著门槛"
                )
            else:
                reasons.append("方向预测无统计显著优势")
        why = "、".join(reasons) if reasons else "信号不明确"
        trigger = (
            f"若方向准确率升至 {_fmt_pct_plain(EDGE_MIN_ACCURACY, 0)} 以上"
            "且概率突破买卖阈值,将升级为操作建议"
        )

    cond = ",".join(cond_bits)
    head = f"当前{cond}," if cond else ""
    return (
        f"{head}{why}。{trigger}。"
        f"(置信度:{_CONFIDENCE_ZH.get(confidence, '低')})"
    )
