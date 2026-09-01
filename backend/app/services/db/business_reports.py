"""business_reports.py — deterministic business narratives for known intents.

Built purely from the ACTUAL returned rows — no LLM, no hallucination.
Used as the fallback narrative when the synthesis LLM times out or returns
empty, so report-style requests still get a REAL business report instead of
generic statistics ("total X, average Y, std dev Z, outliers...").

Entry point: ``try_build_business_report(question, rows, src)`` returns a
markdown report string when the data matches a known business shape
(contract performance), else ``None`` so the caller keeps its generic path.

Why deterministic? The synthesis LLM (qwen3.6-27b via vLLM) is slow and
frequently times out; the previous fallback was a generic stats generator
that labelled columns "total_revenue" / "contract_price" and printed
std-dev / outlier trivia — noise for a business report. This module builds
the actual report (contracted vs delivered vs remaining, execution rate,
top customers, MoM) from the rows that were already fetched.

The report uses ``##`` markdown section headers: the chat frontend
suppresses the raw DataTableCard whenever the message contains ``##``
headers, so the user sees the report instead of the raw SQL/data table.

App-scoped by design (app-isolation rule): column names live in app code,
never in global skills.
"""

from __future__ import annotations

import re

# ──────────────────────────────────────────────────────────────────────
# Column resolution helpers (case-insensitive, underscore-normalized)
# ──────────────────────────────────────────────────────────────────────

def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _tokens(s: str) -> set[str]:
    """Word tokens of a column name, e.g. ``total_contract_quantity`` ->
    ``{'total', 'contract', 'quantity'}``."""
    return set(re.findall(r"[a-z0-9]+", (s or "").lower()))


def _is_ordered_subsequence(want: list[str], cand: list[str]) -> bool:
    """True when ``want`` tokens appear in ``cand`` in the SAME ORDER.

    ``contract_total`` ⊄ ``total_contract_quantity`` (total precedes
    contract in the candidate) but ⊆ ``contract_value_total``.
    """
    it = iter(cand)
    return all(w in it for w in want)


def _find_col(keys: list[str], *names: str) -> str | None:
    """Return the first key matching any of ``names``.

    Matching is EXACT first (normalized), then ORDERED-SUBSEQUENCE on word
    tokens: ``total_contract_quantity`` matches the wanted name
    ``contract_quantity`` ([contract, quantity] is an ordered subsequence
    of [total, contract, quantity]) while ``contract_total`` does NOT
    match it (order differs). Single-token names only match exactly.
    """
    wanted = [(_norm(n), re.findall(r"[a-z0-9]+", (n or "").lower())) for n in names]
    for k in keys:
        nk = _norm(k)
        toks = re.findall(r"[a-z0-9]+", (k or "").lower())
        for wn, wt in wanted:
            if nk == wn:
                return k
            if len(wt) >= 2 and _is_ordered_subsequence(wt, toks):
                return k
    return None


def _num(v) -> float | None:
    """Best-effort float conversion; None for non-numeric values."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace(",", "")
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def _fmt_money(v: float | None) -> str:
    if v is None:
        return "—"
    return f"¥{v:,.2f}"


def _fmt_qty(v: float | None) -> str:
    if v is None:
        return "—"
    return f"{v:,.2f}"


def _fmt_pct(v: float | None) -> str:
    if v is None:
        return "—"
    return f"{v:.1f}%"


# ──────────────────────────────────────────────────────────────────────
# Intent detection
# ──────────────────────────────────────────────────────────────────────

_CONTRACT_KEYWORDS = (
    r"contract",
    r"履约",       # performance / fulfillment
    r"合同",       # contract
)

_CONTRACT_DATA_COLUMNS = (
    "contract_fqty", "contract_quantity", "contract_qty",
    "out_notoutqty", "execution_rate_pct",
    "f_billno", "fbillno", "f_bill_no",
    # LLM-aggregated aliases (observed on qwen3.6-27b): the sub-agent
    # renames SUM(contract_fqty) → total_sales_volume / sales_volume and
    # SUM(contract_amount) → revenue / total_revenue. Without these the
    # deterministic report resolves no quantity/amount columns and the
    # docx table renders 0.00/— while the KPI cards show real numbers.
    "total_sales_volume", "sales_volume", "total_volume", "volume",
    "revenue", "total_revenue", "sales_amount", "total_amount",
    "contract_revenue", "shipment_revenue", "out_amount", "out_revenue",
    "average_execution_price", "avg_execution_price", "avg_price",
)


def _looks_like_contract_question(question: str) -> bool:
    q = question or ""
    return any(re.search(p, q, re.IGNORECASE) for p in _CONTRACT_KEYWORDS)


def _looks_like_contract_data(rows: list[dict]) -> bool:
    if not rows:
        return False
    keys = list(rows[0].keys())
    normalized = {_norm(k) for k in keys}
    for col in _CONTRACT_DATA_COLUMNS:
        if _norm(col) in normalized:
            return True
    # Delivered/contracted qty pair without the classic names
    if _find_col(keys, "contract_fqty", "contract_quantity", "contract_qty") and _find_col(
        keys, "out_fqty", "shipment_quantity", "ship_qty", "delivered_qty"
    ):
        return True
    return False


# ──────────────────────────────────────────────────────────────────────
# Contract performance report
# ──────────────────────────────────────────────────────────────────────

def _build_contract_performance_report(
    question: str, rows: list[dict], src: str
) -> str | None:
    keys = list(rows[0].keys())

    # ── Resolve columns ────────────────────────────────────────────────
    # 2026-08-26 (run3): the LLM data agent aliases aggregated contract
    # columns with business-y names (SUM(contract_fqty) AS
    # total_sales_volume, SUM(contract_amount) AS revenue, SUM(amt)/SUM(qty)
    # AS average_execution_price). Keep those aliases in the wanted-name
    # lists so the deterministic report still resolves qty/amount/price.
    cust_col = _find_col(keys, "CUST_NAME", "partner_name", "customer_name", "customer")
    qty_col = _find_col(keys,
        "contract_fqty", "contract_quantity", "contract_qty",
        "total_sales_volume", "sales_volume", "total_volume", "volume", "total_quantity",
    )
    out_col = _find_col(keys, "out_fqty", "shipment_quantity", "ship_qty", "delivered_qty",
        "total_shipment_quantity", "total_delivered_qty", "delivered_quantity",
        "delivery_quantities", "delivery_quantity", "total_delivery_quantity",
        "total_shipped_qty", "shipped_qty", "shipped_quantity", "total_shipped_quantity")
    amt_col = _find_col(keys, "contract_amount", "contract_revenue", "total_revenue", "amount",
        "revenue", "sales_amount", "total_amount", "contract_value", "total_contract_amount")
    price_col = _find_col(keys, "contract_price", "price", "unit_price", "FPRICE",
        "average_execution_price", "avg_execution_price", "avg_price", "execution_price")
    rate_col = _find_col(keys, "execution_rate_pct", "execution_rate", "fulfillment_rate")
    remain_col = _find_col(keys, "out_notoutqty", "remaining_qty", "not_out_qty",
                           "undelivered_quantities", "undelivered_quantity")
    date_col = _find_col(keys, "FDATE", "shipment_date", "FDELIVERYDATE", "contract_date", "date")
    contract_col = _find_col(keys, "FBILLNO", "contract_id", "contract_no", "FID", "id")
    mat_col = _find_col(keys, "material_name", "FMATERIALID", "material_code", "product_name")
    prior_qty_col = _find_col(keys, "prior_month_contract_qty", "prior_qty", "previous_month_qty")

    # 2026-08-27 (run10): the LLM answered a "contract performance" request
    # with a DELIVERED-ONLY query (order_number/customer/product/delivered_quantity/
    # unit_price/revenue — no contract_fqty/contract_amount at all). Without a
    # contracted-quantity column we cannot compute exec rate or "Contracted Qty"
    # (it would render 0.00 for every row) and "Contracted value" would be the
    # DELIVERED revenue mislabeled. Honest degradation: refuse the contract
    # shape and let the caller's generic fallback describe the delivered data.
    if not qty_col:
        return None

    prior_out_col = _find_col(keys, "prior_month_shipment_qty", "prior_shipment_qty")
    cur_qty_col = _find_col(keys, "current_month_contract_qty", "current_qty", "this_month_qty")
    cur_out_col = _find_col(keys, "current_month_shipment_qty", "current_shipment_qty")

    # ── Aggregate over rows ────────────────────────────────────────────
    n_lines = len(rows)
    total_qty = sum(_num(r.get(qty_col)) or 0 for r in rows) if qty_col else 0.0
    total_out = sum(_num(r.get(out_col)) or 0 for r in rows) if out_col else 0.0

    # ── Contracted value ─────────────────────────────────────────────
    # The LLM's aliases are UNRELIABLE about amount-vs-unit-price:
    #   "contract_price"         = ROUND(SUM(contract_amount)/SUM(contract_fqty),2) → unit price
    #   "total_contract_price"   = SUM(contract_amount)                              → TOTAL
    #   "contract_amount"        = SUM(contract_amount)                              → TOTAL
    # Rule: a column whose name carries total/sum/amount/value tokens is a
    # TOTAL (sum its values directly); a bare price/unit_price column is
    # per-unit (multiply by qty). If neither exists, omit contracted
    # value rather than fabricate one (price × qty on a misnamed total
    # double-counts — observed ¥2.098B from a ¥5.2M portfolio).
    _TOTAL_TOKENS = {"total", "sum", "amount", "value", "amt"}

    # Resolve the contract-value column ONCE so delivered value can be
    # required to come from a DIFFERENT column (2026-08-26 run6: the LLM
    # aliased SUM(contract_amount) AS total_revenue; _find_col matched it
    # for BOTH contract value and delivered value → "Delivered value
    # ¥249,590,340.57" duplicated "Contracted value". If the same column
    # feeds both, the query has no separate delivered-amount column and
    # delivered value must be omitted, not duplicated.)
    c_amt_col = _find_col(keys, "contract_amount", "contract_value", "contract_total",
                          "contract_revenue", "total_contract_amount",
                          "revenue", "total_revenue", "sales_amount", "total_amount")
    delivered_amt_col = _find_col(keys, "out_amount", "shipment_revenue", "out_revenue",
                                  "delivered_value", "shipment_amount")

    # 2026-08-27 (user docx): the LLM aliases contract_amount AS contract_price
    # (a TOTAL misnamed as price) AND out_amount AS revenue (delivered). The
    # resolver above picks revenue → "Contracted value" would show DELIVERED
    # value. Discriminate data-driven per row: price×qty ≈ amount ⇒ the price
    # column is genuinely per-unit; price ≈ amount ⇒ the price column actually
    # holds TOTALS (promote it to contract value, demote the resolved amount
    # column to delivered value). Classic fixture (price 7,637.5 × qty 14 =
    # 106,925 = total_revenue) scores unit; user shape (contract_price
    # 31,752,000 ≈ revenue 31,636,218.60) scores total.
    if price_col and c_amt_col and qty_col and price_col != c_amt_col:
        total_hits = unit_hits = 0
        for r in rows:
            p = _num(r.get(price_col))
            q = _num(r.get(qty_col))
            a = _num(r.get(c_amt_col))
            if p is None or q is None or a is None or a == 0:
                continue
            if abs(p - a) / abs(a) < 0.5:
                total_hits += 1
            if abs(p * q - a) / abs(a) < 0.5:
                unit_hits += 1
        if total_hits > unit_hits and total_hits > 0:
            delivered_amt_col = c_amt_col
            c_amt_col = price_col

    def _row_contract_value(r: dict) -> float | None:
        # NOTE: do NOT add out_amount / delivered-* names here — _find_col
        # scans KEYS in order and out_amount precedes contract_amount in raw
        # ERP rows, so contract value would resolve to the DELIVERED amount
        # (¥8,000) instead of the contracted amount (¥10,000). Delivered
        # value is computed separately via out_amt_col below.
        if c_amt_col:
            return _num(r.get(c_amt_col))
        if price_col and qty_col:
            pv = _num(r.get(price_col))
            qv = _num(r.get(qty_col))
            if pv is None or qv is None:
                return None
            if _tokens(price_col) & _TOTAL_TOKENS:
                return pv  # total-named price column IS the contract total
            return pv * qv  # bare price = per-unit
        return None

    contract_value = 0.0
    for r in rows:
        v = _row_contract_value(r)
        if v is not None:
            contract_value += v

    # Delivered value — must be a DIFFERENT column than contract value,
    # otherwise the query has no delivered-amount data and we omit it
    # rather than duplicating the contracted amount.
    delivered_value = 0.0
    if delivered_amt_col and delivered_amt_col != c_amt_col:
        delivered_value = sum(_num(r.get(delivered_amt_col)) or 0 for r in rows)

    # Execution rate: aggregate (total delivered ÷ total contracted) is
    # the business-correct measure — per-row rates overweight small
    # contracts (a 175% over-delivery line would skew a simple average).
    if qty_col and out_col and total_qty:
        exec_rate = total_out / total_qty * 100.0
    elif rate_col:
        rates = [
            v for v in (_num(r.get(rate_col)) for r in rows) if v is not None
        ]
        exec_rate = (sum(rates) / len(rates)) if rates else None
    else:
        exec_rate = None

    # Remaining qty: explicit column, else contract − delivered.
    if remain_col:
        remaining = sum(_num(r.get(remain_col)) or 0 for r in rows)
    elif qty_col and out_col:
        remaining = max(total_qty - total_out, 0.0)
    else:
        remaining = None

    # Date range
    date_min = date_max = None
    if date_col:
        dates = sorted(
            str(r.get(date_col) or "")[:10] for r in rows if r.get(date_col)
        )
        dates = [d for d in dates if re.match(r"\d{4}-\d{2}-\d{2}", d)]
        if dates:
            date_min, date_max = dates[0], dates[-1]

    # Customers
    customers: dict[str, dict] = {}
    if cust_col:
        for r in rows:
            name = str(r.get(cust_col) or "").strip()
            if not name or name.lower() in ("null", "none", "nan"):
                continue
            c = customers.setdefault(
                name,
                {"qty": 0.0, "out": 0.0, "value": 0.0, "lines": 0},
            )
            c["qty"] += _num(r.get(qty_col)) or 0 if qty_col else 0
            c["out"] += _num(r.get(out_col)) or 0 if out_col else 0
            v = _row_contract_value(r)
            if v is not None:
                c["value"] += v
            c["lines"] += 1
    top_customers = sorted(
        customers.items(), key=lambda kv: -kv[1]["value"] if kv[1]["value"] else -kv[1]["qty"]
    )[:10]

    # MoM comparison — from explicit comparison columns if present, else
    # bucket rows by the month of their date column.
    mom: dict | None = None
    if prior_qty_col and cur_qty_col:
        prior_qty = sum(_num(r.get(prior_qty_col)) or 0 for r in rows)
        cur_qty = sum(_num(r.get(cur_qty_col)) or 0 for r in rows)
        prior_out = sum(_num(r.get(prior_out_col)) or 0 for r in rows) if prior_out_col else None
        cur_out = sum(_num(r.get(cur_out_col)) or 0 for r in rows) if cur_out_col else None
        mom = {
            "prior_qty": prior_qty,
            "cur_qty": cur_qty,
            "prior_out": prior_out,
            "cur_out": cur_out,
        }
    elif date_col:
        months: dict[str, dict] = {}
        for r in rows:
            d = str(r.get(date_col) or "")[:7]
            if not re.match(r"\d{4}-\d{2}", d):
                continue
            m = months.setdefault(d, {"qty": 0.0, "out": 0.0})
            m["qty"] += _num(r.get(qty_col)) or 0 if qty_col else 0
            m["out"] += _num(r.get(out_col)) or 0 if out_col else 0
        if len(months) >= 2:
            sm = sorted(months.items())
            prior, cur = sm[-2], sm[-1]
            mom = {
                "prior_label": prior[0],
                "cur_label": cur[0],
                "prior_qty": prior[1]["qty"],
                "cur_qty": cur[1]["qty"],
                "prior_out": prior[1]["out"],
                "cur_out": cur[1]["out"],
            }

    # Execution risk — lines with rate < 80% and material remaining value
    risk_rows: list[tuple[str, float, float]] = []
    for r in rows:
        rate = None
        if rate_col:
            rate = _num(r.get(rate_col))
        elif qty_col and out_col:
            q = _num(r.get(qty_col)) or 0
            o = _num(r.get(out_col)) or 0
            if q > 0:
                rate = o / q * 100.0
        if rate is None or rate >= 80.0:
            continue
        label = str(r.get(cust_col) or r.get(mat_col) or r.get(contract_col) or "—").strip()
        if len(label) > 40:
            label = label[:40] + "…"
        q = _num(r.get(qty_col)) or 0
        o = _num(r.get(out_col)) or 0
        remaining_value = 0.0
        rv = _row_contract_value(r)
        if rv is not None:
            remaining_value = rv * max(1 - rate / 100.0, 0)
        elif q and o and price_col:
            p = _num(r.get(price_col)) or 0
            remaining_value = p * max(q - o, 0)
        risk_rows.append((label, rate, remaining_value))
    risk_rows.sort(key=lambda t: -t[2])
    risk_rows = risk_rows[:8]

    # ── Language ───────────────────────────────────────────────────────
    is_zh = any("\u4e00" <= ch <= "\u9fff" for ch in (question or ""))
    L = lambda en, zh: zh if is_zh else en

    # ── Title from the user request ────────────────────────────────────
    title = _derive_title(question)
    out: list[str] = []
    out.append(f"# {L(title or 'Contract Performance Report', title or '合同履约报告')}\n")

    # ── Scope ──────────────────────────────────────────────────────────
    scope_bits = [f"{n_lines} {L('contract line(s)', '条合同明细')} {L('from', '来自')} `{src}`"]
    if date_min and date_max:
        scope_bits.append(
            L(
                f"covering contract dates `{date_min}` to `{date_max}`",
                f"合同日期覆盖 `{date_min}` 至 `{date_max}`",
            )
        )
    out.append(f"**{L('Scope', '范围')}:** " + ", ".join(scope_bits) + ".\n")

    # ── Executive Summary ──────────────────────────────────────────────
    out.append(f"## {L('Executive Summary', '执行摘要')}\n")
    exec_lines = []
    if qty_col:
        exec_lines.append(
            L(
                f"Total contracted quantity was **{_fmt_qty(total_qty)}** across {n_lines} "
                f"contract lines" + (f" and {len(customers)} customers" if customers else "") + ".",
                f"合同总量为 **{_fmt_qty(total_qty)}**，共 {n_lines} 条合同明细"
                + (f"，{len(customers)} 家客户" if customers else "") + "。",
            )
        )
    if out_col and total_out is not None:
        exec_lines.append(
            L(
                f"Delivered quantity reached **{_fmt_qty(total_out)}** — an execution rate of "
                f"**{_fmt_pct(exec_rate)}**"
                + (f", leaving **{_fmt_qty(remaining)}** outstanding to deliver" if remaining is not None else "")
                + ".",
                f"已交付数量为 **{_fmt_qty(total_out)}**，履约率 **{_fmt_pct(exec_rate)}**"
                + (f"，待交付 **{_fmt_qty(remaining)}**" if remaining is not None else "")
                + "。",
            )
        )
    if contract_value:
        exec_lines.append(
            L(
                f"Contracted value was **{_fmt_money(contract_value)}**"
                + (f" with delivered value **{_fmt_money(delivered_value)}**" if delivered_value else "")
                + ".",
                f"合同金额为 **{_fmt_money(contract_value)}**"
                + (f"，已交付金额 **{_fmt_money(delivered_value)}**" if delivered_value else "")
                + "。",
            )
        )
    if mom and mom.get("cur_qty") is not None and mom.get("prior_qty") is not None:
        prior, cur = mom["prior_qty"], mom["cur_qty"]
        if prior > 0:
            delta = (cur - prior) / prior * 100.0
            label = mom.get("cur_label") or "current month"
            prev_label = mom.get("prior_label") or "prior month"
            exec_lines.append(
                L(
                    f"Compared with {prev_label}, contracted volume for {label} "
                    f"**{'rose' if delta >= 0 else 'fell'} {abs(delta):.1f}%** "
                    f"({_fmt_qty(prior)} → {_fmt_qty(cur)}).",
                    f"与 {prev_label} 相比，{label} 合同量"
                    f"**{'增长' if delta >= 0 else '下降'} {abs(delta):.1f}%**"
                    f"（{_fmt_qty(prior)} → {_fmt_qty(cur)}）。",
                )
            )
        else:
            exec_lines.append(
                L(
                    f"Prior-month contracted volume was zero; {mom.get('cur_label') or 'current month'} "
                    f"added {_fmt_qty(cur)}.",
                    f"上月合同量为零，{mom.get('cur_label') or '本月'}新增 {_fmt_qty(cur)}。",
                )
            )
    if risk_rows:
        exec_lines.append(
            L(
                f"**{len(risk_rows)} contract line(s)** are executing below 80% and need attention.",
                f"有 **{len(risk_rows)} 条合同明细**履约率低于 80%，需重点关注。",
            )
        )
    if not exec_lines:
        exec_lines.append(
            L(
                f"The query returned {n_lines} rows from `{src}`.",
                f"查询返回 {n_lines} 行数据，来源 `{src}`。",
            )
        )
    out.append(" ".join(exec_lines))
    out.append("")

    # ── Key Figures ────────────────────────────────────────────────────
    out.append(f"## {L('Key Figures', '关键指标')}\n")
    out.append("| " + L("Metric", "指标") + " | " + L("Value", "数值") + " |")
    out.append("|---|---|")
    if qty_col:
        out.append(f"| {L('Contracted quantity', '合同数量')} | {_fmt_qty(total_qty)} |")
    if out_col:
        out.append(f"| {L('Delivered quantity', '已交付数量')} | {_fmt_qty(total_out)} |")
    if exec_rate is not None:
        out.append(f"| {L('Execution rate', '履约率')} | {_fmt_pct(exec_rate)} |")
    if contract_value:
        out.append(f"| {L('Contracted value', '合同金额')} | {_fmt_money(contract_value)} |")
    if delivered_value:
        out.append(f"| {L('Delivered value', '已交付金额')} | {_fmt_money(delivered_value)} |")
    if remaining is not None:
        out.append(f"| {L('Outstanding to deliver', '待交付数量')} | {_fmt_qty(remaining)} |")
    out.append(f"| {L('Contract lines', '合同明细数')} | {n_lines} |")
    if customers:
        out.append(f"| {L('Customers', '客户数')} | {len(customers)} |")
    out.append("")

    # ── Top customers ──────────────────────────────────────────────────
    if top_customers:
        out.append(f"## {L('Top Customers by Contract Value', '合同金额 Top 客户')}\\n")
        # 2026-08-26 (run3): when the query has NO delivered-qty column
        # (out_col is None — e.g. the LLM aggregated only contract
        # qty/amount), do NOT render Delivered Qty / Execution Rate
        # columns. Previously they showed 0.00 / 0.0% — implying zero
        # delivery — when the truth is "no delivery data in this query".
        _has_out_col = bool(out_col)
        header = [L("Customer", "客户"), L("Lines", "明细数"), L("Contracted Qty", "合同数量")]
        if _has_out_col:
            header += [L("Delivered Qty", "已交付数量"), L("Execution Rate", "履约率")]
        header += [L("Contract Value", "合同金额")]
        out.append("| " + " | ".join(header) + " |")
        out.append("|" + "---|" * len(header))
        for name, c in top_customers:
            cells = [str(name)[:36], str(c["lines"]), _fmt_qty(c["qty"])]
            if _has_out_col:
                rate_c = (c["out"] / c["qty"] * 100.0) if c["qty"] else None
                cells += [_fmt_qty(c["out"]), _fmt_pct(rate_c)]
            cells.append(_fmt_money(c["value"]) if c["value"] else "—")
            out.append("| " + " | ".join(cells) + " |")
        out.append("")

    # ── Month-over-Month ───────────────────────────────────────────────
    if mom and mom.get("cur_qty") is not None:
        out.append(f"## {L('Month-over-Month Comparison', '月度环比对比')}\n")
        cur_label = mom.get("cur_label") or L("Current month", "本月")
        prior_label = mom.get("prior_label") or L("Prior month", "上月")
        out.append(
            "| " + L("Metric", "指标") + f" | {prior_label} | {cur_label} | "
            + L("Change", "变化") + " |"
        )
        out.append("|---|---|---|---|")
        if mom.get("prior_qty") is not None and mom.get("cur_qty") is not None:
            p, c = mom["prior_qty"], mom["cur_qty"]
            chg = ((c - p) / p * 100.0) if p else None
            out.append(
                f"| {L('Contracted qty', '合同数量')} | {_fmt_qty(p)} | {_fmt_qty(c)} | "
                + (_fmt_pct(chg) if chg is not None else L("n/a (base 0)", "基数 0") ) + " |"
            )
        if mom.get("prior_out") is not None and mom.get("cur_out") is not None:
            p, c = mom["prior_out"], mom["cur_out"]
            chg = ((c - p) / p * 100.0) if p else None
            out.append(
                f"| {L('Delivered qty', '已交付数量')} | {_fmt_qty(p)} | {_fmt_qty(c)} | "
                + (_fmt_pct(chg) if chg is not None else L("n/a (base 0)", "基数 0")) + " |"
            )
        out.append("")

    # ── Execution risk ─────────────────────────────────────────────────
    if risk_rows:
        out.append(f"## {L('Execution Risk — Below 80%', '履约风险（低于 80%）')}\n")
        out.append(
            "| " + " | ".join([
                L("Customer / Contract", "客户 / 合同"),
                L("Rate", "履约率"),
                L("Remaining Value", "剩余金额"),
            ]) + " |"
        )
        out.append("|---|---|---|")
        for label, rate, rv in risk_rows:
            out.append(f"| {label} | {_fmt_pct(rate)} | {_fmt_money(rv) if rv else '—'} |")
        out.append("")

    # ── Methodology ────────────────────────────────────────────────────
    out.append(f"## {L('Methodology', '方法论')}\n")
    out.append(
        L(
            f"Figures are aggregated from `{src}` ({n_lines} rows returned by the query). "
            "Contracted/delivered quantities and values are summed per contract line; "
            "execution rate = delivered ÷ contracted × 100; MoM splits the query window "
            "into the two most recent months present in the data.",
            f"数据来自 `{src}`（查询返回 {n_lines} 行）。合同数量、交付数量与金额按合同明细求和；"
            "履约率 = 已交付 ÷ 合同量 × 100；环比对比取数据中最近两个月的月份拆分。",
        )
    )
    out.append("")
    return "\n".join(out)


def _derive_title(question: str) -> str | None:
    """Extract a clean title from the user's question, if possible."""
    if not question:
        return None
    t = re.sub(r"\s+", " ", question.strip())
    t = re.sub(
        r"\s+(in|as|using|into)\s+(a\s+)?(docx|word|pdf|pptx|powerpoint|excel|xlsx|"
        r"markdown|md|html|file|document|deck|spreadsheet|formate?|format)\s*"
        r"(file|document|deck|spreadsheet)?\s*[.\?]?\s*$",
        "", t, flags=re.IGNORECASE,
    ).strip()
    t = re.sub(
        r"^(please\s+|can\s+you\s+|i\s+want\s+|i\s+need\s+|give\s+me\s+|"
        r"show\s+me\s+|make\s+me\s+|generate\s+|create\s+)\s*",
        "", t, flags=re.IGNORECASE,
    ).strip()
    if t and len(t) >= 3:
        t = t[0].upper() + t[1:]
        t = re.sub(r"\s+", " ", t).rstrip(" .?!")
        if not t.lower().endswith("report"):
            t = f"{t} Report"
        return t
    return None


# ──────────────────────────────────────────────────────────────────────
# Public entry point
# ──────────────────────────────────────────────────────────────────────

def try_build_business_report(
    question: str, rows: list[dict], src: str
) -> str | None:
    """Return a deterministic business report when the data/request matches
    a known business shape; ``None`` otherwise (caller keeps generic path)."""
    if not rows:
        return None
    if not isinstance(rows[0], dict):
        return None
    try:
        if _looks_like_contract_question(question) or _looks_like_contract_data(rows):
            return _build_contract_performance_report(question, rows, src)
    except Exception:  # never let the fallback break the response
        return None
    return None
