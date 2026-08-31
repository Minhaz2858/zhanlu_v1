"""Fallback message builders for the agent loop (P2-12 extraction).

Pure constants and builders extracted verbatim from ``app/routers/agents.py``.
The agents router re-imports these names into its own namespace so all
existing call sites (and ``from app.routers.agents import ...`` consumers)
keep working unchanged.

Note: ``_choose_fallback`` and ``_data_rows_fallback`` intentionally stay in
the agents router — they reference router-local helpers
(``_is_dashboard_request``, ``is_file_deliverable_request``,
``DATA_PRODUCING_TOOLS``).
"""

from __future__ import annotations

import re


_EMPTY_CONTENT_FALLBACK = (
    "I've completed the requested changes. "
    "Please review the agent configuration above."
)

_GENERIC_EMPTY_CONTENT_FALLBACK = (
    "I gathered some information but had trouble putting it all together. "
    "Could you try again with a more specific request?"
)

# Apology / soft-failure phrases the model emits instead of answering when it
# has data but "can't synthesize". These are NOT empty answers (so the
# empty-bubble guarantee misses them) but are equally unhelpful to the user.
# Used by the apology-guard (Fix 5): when content matches one of these AND
# actual rows were retrieved, we force one re-synthesis pass (in-loop) or swap
# the apology for a data-aware message (post-loop).
_APOLOGY_PATTERN_RE = re.compile(
    r"I (?:gathered|collected) (?:some )?information but had trouble"
    r"|I (?:was )?unable to (?:put|synthesize|compile)"
    r"|I (?:could|couldn'?t) (?:not )?(?:synthesize|compile|put together)"
    r"|had trouble putting it all together"
    # Chinese: "但无法将结果整合在一起" / "无法把数据汇总" etc. Note the
    # "无法" is usually preceded by "但" (but), not "我" (I), so the branch
    # starts at 无法 and scans forward within one clause.
    r"|无法(?:把|将|对)?[^，。！？]{0,30}(?:整合|汇总|组织|完成|给出)"
    r"|没有(?:给出|完成|提供|形成)"
    r"|未能(?:给出|完成|提供|形成)"
    r"|我(?:收集|汇总|获取).{0,40}(?:遇到|无法)(?:问题|困难)",
    re.IGNORECASE,
)

# Bounce-back pattern: the agent dumped raw data and invited the user to
# "ask for a summary" instead of actually answering. This is a non-answer
# even though data was retrieved — the user asked for an answer, not a
# data dump with a re-ask invitation. Works for both English and Chinese.
_BOUNCE_BACK_PATTERN_RE = re.compile(
    r"I retrieved \d+ (?:rows?|records?)"
    r"|retrieved \d+ (?:rows?|records?) from"
    r"|you can ask me (?:for|to)"
    r"|ask me (?:for|to) (?:a )?(?:summary|breakdown|chart|analysis)"
    r"|would you like (?:me to )?(?:provide|create|generate|make)"
    r"|I (?:can |will )?(?:provide|create|generate|make) (?:a )?(?:summary|breakdown|chart|report)"
    r"|let me know if you"
    r"|feel free to ask"
    # Chinese equivalents: "你可以让我/请让我..." / "需要我...吗"
    r"|你可以(?:让|请)?我"
    r"|需要我(?:提供|生成|制作|分析|总结)"
    r"|是否需要我"
    r"|我(?:可以|能)?(?:提供|生成|制作|分析|总结).{0,20}(?:报告|摘要|图表|分析|总结)",
    re.IGNORECASE,
)

_DASHBOARD_REDIRECT_FALLBACK = (
    "I gathered some information but had trouble putting it all together. "
    "Could you try again with a more specific request? "
    "If you asked me to build a dashboard, please say 'create dashboard' and I'll try again."
)


def _collect_artifact_titles(
    tool_calls_for_frontend: list[dict],
    orch_created: list[dict],
) -> list[str]:
    """Best-effort list of artifact titles produced this turn.

    Pulls titles from orchestrator-created artifacts (``_orch_created``) and
    from artifact/dashboard tool results recorded in ``tool_calls_for_frontend``.
    Also scans for ``report_card_payload.title`` (data-agent results) and
    any other ``results.title`` / ``results.summary.title`` fields so that
    report-card artifacts are never hidden by the fallback.
    Deduplicated, order-preserving. Used by the artifact-aware fallback so an
    empty ``assistant_content`` never hides a successfully produced artifact.
    """
    titles: list[str] = []
    for art in (orch_created or []):
        t = (art or {}).get("title")
        if t:
            titles.append(str(t))
    for tc in (tool_calls_for_frontend or []):
        name = tc.get("name")
        result = tc.get("results") or {}
        if not isinstance(result, dict):
            continue
        # Dashboard artifact
        if name == "create_dashboard":
            art = result.get("artifact")
            if isinstance(art, dict) and art.get("title"):
                titles.append(str(art["title"]))
        # Generic artifact / sandbox
        elif name in ("create_artifact", "run_sandbox_skill"):
            if result.get("title"):
                titles.append(str(result["title"]))
        # Data-agent report card (ask_data_agent)
        rcp = result.get("report_card_payload")
        if isinstance(rcp, dict) and rcp.get("title"):
            titles.append(str(rcp["title"]))
        # Generic title / summary.title on any tool result
        if result.get("title") and not (name in ("create_artifact", "run_sandbox_skill")):
            titles.append(str(result["title"]))
        summary = result.get("summary")
        if isinstance(summary, dict) and summary.get("title"):
            titles.append(str(summary["title"]))
    seen: set[str] = set()
    out: list[str] = []
    for t in titles:
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return out


def _artifact_aware_fallback(titles: list[str]) -> str:
    """Build a user-facing message that references the produced artifact(s).

    Prefer this over ``_GENERIC_EMPTY_CONTENT_FALLBACK`` when the loop ended
    with empty assistant text but an artifact WAS produced — the artifact is
    the real deliverable, so the message should point at it instead of
    claiming the agent "had trouble putting it together".

    FIX 2026-08-23: previously returned a thin one-liner that started with
    "I've completed your request. Here's the artifact: …". That text
    *hid* the analysis the user actually wanted and was indistinguishable
    from a generic LLM fallback. Now we emit a more substantive opener
    that invites the user to scroll into the artifact / ask for
    clarification, while still naming the artifact.
    """
    if not titles:
        return (
            "Your deliverable is attached above. Open it for the full "
            "data, tables, and charts. If anything looks off, tell me "
            "what you'd like to refine (time range, grouping, metric) "
            "and I'll re-run."
        )
    if len(titles) == 1:
        return (
            f"Your deliverable — **{titles[0]}** — is attached above. "
            f"It contains the full data, tables, and charts I pulled "
            f"from your warehouse. Open the artifact for the complete "
            f"view; if any number, ranking, or chart needs a different "
            f"angle, tell me which one to refine."
        )
    joined = ", ".join(f"**{t}**" for t in titles)
    return (
        f"Your deliverables — {joined} — are attached above. "
        f"They contain the full data, tables, and charts I pulled from "
        f"your warehouse. Open the artifacts for the complete view; if "
        f"any number, ranking, or chart needs a different angle, tell "
        f"me which one to refine."
    )


def _data_summary_fallback(titles: list[str], user_content: str = "") -> str:
    """Fallback when data-agent report cards exist but synthesis text is empty.

    This is more specific than the generic fallback: it names the reports
    that were produced and avoids the misleading "create dashboard" redirect
    unless the user actually asked for a dashboard.
    """
    if len(titles) == 1:
        return (
            f"I produced **{titles[0]}** above with the full data. "
            f"Let me know if you'd like a more detailed analysis or a combined dashboard."
        )
    joined = ", ".join(f"**{t}**" for t in titles)
    return (
        f"I produced {joined} above with the full supply chain data. "
        f"Let me know if you'd like a single combined dashboard or a deeper analysis."
    )


# NOTE: ``_automation_scheduled_confirmation`` lives in
# ``app.routers.agents`` (router-local pattern, alongside
# ``_artifact_aware_fallback`` and ``_data_summary_fallback``) so that
# ``_choose_fallback`` can call it without an import. The agents router
# is the source of truth for this helper; this module is intentionally
# kept free of redundant copies.


_SYS_COL_PREFIXES = ("FENTRYID", "FID", "FCUSTMATID", "FCUSTMATNAME")


def _is_degenerate_dataset(rows: list[dict] | None) -> bool:
    """True when every row has zero / None / "" in all apparent measure columns.

    A 'measure' column is any key that looks monetary (revenue, margin, amount,
    price, total, sales) but NOT a count/quantity column (line_count, qty,
    quantity, count, orders) which may legitimately be non-zero while the money
    columns are broken.  Used to prevent building a report card around a query
    that mapped the wrong column (e.g. revenue = 0 for every row).
    """
    rows = rows or []
    if not rows:
        return True
    _measure_re = re.compile(
        r"^(revenue|margin|amount|price|total|sales|income|profit|cost)$",
        re.IGNORECASE,
    )
    _count_re = re.compile(r"^(line_count|qty|quantity|count|orders|num)$", re.IGNORECASE)
    measure_cols: set[str] = set()
    for row in rows:
        if isinstance(row, dict):
            for k in row:
                if _measure_re.search(k) and not _count_re.search(k):
                    measure_cols.add(k)
    if not measure_cols:
        return False  # no measure columns to judge by
    for row in rows:
        if not isinstance(row, dict):
            continue
        for col in measure_cols:
            v = row.get(col)
            if v not in (None, "", 0, 0.0):
                return False
    return True


__all__ = [
    "_EMPTY_CONTENT_FALLBACK",
    "_GENERIC_EMPTY_CONTENT_FALLBACK",
    "_APOLOGY_PATTERN_RE",
    "_BOUNCE_BACK_PATTERN_RE",
    "_DASHBOARD_REDIRECT_FALLBACK",
    "_collect_artifact_titles",
    "_artifact_aware_fallback",
    "_data_summary_fallback",
    "_SYS_COL_PREFIXES",
    "_is_degenerate_dataset",
]
