"""AI analysis for Contract Performance report.

Key design notes:
1. Uses zhanlu's `stream_chat_completion` (async generator) directly, no
   `asyncio.to_thread` needed for the sync stream + queue fan-in.
2. LLM prompt is a single string (zhanlu API), not (system, user) pair.
   System instructions are prepended to the user data with a blank separator.
3. 5 parallel LLM calls fan into a shared asyncio.Queue; the SSE producer
   yields (event, data) tuples consumed by the router's StreamingResponse.
4. 6-hour in-memory cache keyed by (org, start_date, end_date, material).
"""

import asyncio
import json as _json
import logging
import threading
import time
from typing import AsyncGenerator, Optional

from app.services.llm_service import stream_chat_completion
from app.schemas.reports import AiAnalysisResult

logger = logging.getLogger(__name__)

# ── Cache ────────────────────────────────────────────────────────────────

_ai_cache: dict[tuple, dict] = {}
_ai_cache_lock = threading.Lock()
_AI_CACHE_TTL = 6 * 3600  # 6 hours


def _sse(event: str, data: dict | str) -> bytes:
    if isinstance(data, dict):
        payload = _json.dumps(data, ensure_ascii=False)
    else:
        payload = data
    return f"event: {event}\ndata: {payload}\n\n".encode("utf-8")


# ── Prompt templates ─────────────────────────────────────────────────────

def _make_stream_prompt(system: str, user: str) -> str:
    """Combine system instruction + user data into a single prompt.

    Zhanlu's stream_chat_completion accepts only one `prompt` string.
    generate_completion_stream expects a (system, user) tuple.
    We concatenate with a clear separator.
    """
    return f"指令：{system}\n\n数据：{user}"


def build_ai_context(
    execution_chart: list[dict],
    unshipped_contracts: list[dict],
    org_label: str,
    start_date: str,
    end_date: str,
) -> str:
    """Build a compact text representation of chart data for the LLM."""
    lines = []
    lines.append(f"=== {start_date} 至 {end_date} {org_label}销售合同执行情况 ===")
    if execution_chart:
        lines.append("各产品执行数据：")
        for item in execution_chart:
            lines.append(
                f"  {item.get('product','?')}: "
                f"合同量{item.get('contract_qty',0)}吨, "
                f"出库量{item.get('out_qty',0)}吨, "
                f"执行率{item.get('execution_rate',0)}%"
            )
    if unshipped_contracts:
        lines.append(f"\n未出库≥30吨合同（共{len(unshipped_contracts)}条）：")
        for item in unshipped_contracts[:10]:
            lines.append(
                f"  {item.get('product','?')} | {item.get('customer','?')} | "
                f"合同量{item.get('contract_qty',0)}吨 | "
                f"未出量{item.get('unshipped_qty',0)}吨 | "
                f"交期{item.get('delivery_date','?')}"
            )
    return "\n".join(lines)


async def stream_ai_analysis(
    execution_chart: list[dict],
    unshipped_contracts: list[dict],
    org_meta: dict,
    start_date: str,
    end_date: str,
    material: str = "",
) -> AsyncGenerator[bytes, None]:
    """SSE stream generator for contract-performance AI analysis.

    Event protocol:
      meta:    {"status": "started" | "cached"}
      delta:   {"field": "...", "text": "<incremental>"}
      partial: {"summary_bullets": ["..."]}
      done:    AiAnalysisResult as dict
      error:   {"message": "..."}
    """

    cache_key = (org_meta["key"], start_date, end_date, material or "")

    # Cache hit: fast path
    with _ai_cache_lock:
        cached = _ai_cache.get(cache_key)
        if cached and (time.time() - cached["ts"]) < _AI_CACHE_TTL:
            logger.info("stream_ai_analysis: cache hit key=%s", cache_key)
            yield _sse("meta", {"status": "cached"})
            yield _sse("done", cached["data"])
            return

    data_str = build_ai_context(
        execution_chart, unshipped_contracts,
        org_meta["label"], start_date, end_date,
    )

    # ── Prompt definitions ────────────────────────────────────────────
    ct_sys = (
        "你是一位资深化工行业业务分析师。根据提供的各产品合同执行数据，"
        "用1-2句话生成图表上方的概述分析，要引用具体数字。"
        "直接返回分析文本，不要任何额外格式或前缀。"
    )
    ct_usr = f"以下是{start_date}至{end_date}期间各产品合同执行情况数据：\n{data_str}\n请生成图表上方的概述分析（1-2句）。"

    cb_sys = (
        "你是一位资深化工行业业务分析师。根据提供的各产品合同执行数据，"
        "用1-2句话生成图表下方的趋势总结，指出执行率最高和最低的产品，给出业务建议。"
        "直接返回分析文本，不要任何额外格式或前缀。"
    )
    cb_usr = f"以下是{start_date}至{end_date}期间各产品合同执行情况数据：\n{data_str}\n请生成图表下方的趋势总结（1-2句）。"

    sp_sys = (
        "你是一位资深化工企业运营分析师。根据报表数据，"
        "用一段话概述整体履约风险，要引用具体产品名、数量、日期。"
        "语言专业简洁，适合管理层阅读。直接返回分析文本，不要任何额外格式或前缀。"
    )
    sp_usr = f"以下是{start_date}至{end_date}期间{org_meta['label']}销售合同执行报表完整数据：\n{data_str}\n请生成总结段落。"

    sb_sys = (
        "你是一位资深化工企业运营分析师。根据报表数据，给出5条核心业务洞察要点，"
        "每条1-2句，涵盖液体vs固体产品对比、同比变化、库存压力、完成率评估、高风险合同预警。"
        "每条要点单独占一行，以「•」开头，语言专业简洁，适合管理层阅读。"
        "直接返回要点列表，不要序号、不要其他格式。"
    )
    sb_usr = f"以下是{start_date}至{end_date}期间{org_meta['label']}销售合同执行报表完整数据：\n{data_str}\n请生成5条核心业务洞察要点。"

    ua_sys = (
        "你是一位供应链与物流分析师。根据未出库30吨以上的合同明细数据，"
        "用1-2句话指出哪些产品积压最严重、涉及多少份合同、未出数量范围、"
        "交货日期集中情况，并给出紧急建议。"
        "直接返回分析文本，不要任何额外格式或前缀。"
    )
    unshipped_list = [
        {
            "product": uc.get("product", ""),
            "customer": uc.get("customer", ""),
            "contract_qty": uc.get("contract_qty", 0),
            "unshipped_qty": uc.get("unshipped_qty", 0),
            "delivery_date": uc.get("delivery_date", ""),
        }
        for uc in unshipped_contracts[:10]
    ]
    ua_usr = (
        f"未出库30吨以上的合同明细：\n"
        f"{_json.dumps(unshipped_list, ensure_ascii=False, indent=2)}\n"
        f"请生成分析文本（1-2句）。"
    )

    yield _sse("meta", {"status": "started"})

    queue: asyncio.Queue = asyncio.Queue()
    ai_result: dict = {
        "chart_top": "",
        "chart_bottom": "",
        "summary_paragraph": "",
        "summary_bullets": [],
        "unshipped_analysis": "",
    }

    # ── 5 parallel streaming workers (async) ──────────────────────────

    async def _text_worker(prompt: str, field: str):
        tokens: list[str] = []
        try:
            async for delta in stream_chat_completion(prompt, temperature=0.7):
                if delta:
                    tokens.append(delta)
                    await queue.put(_sse("delta", {"field": field, "text": delta}))
            ai_result[field] = "".join(tokens)
        except Exception as e:
            logger.warning("AI stream worker %s failed: %s", field, e)
        finally:
            await queue.put(None)  # sentinel

    async def _bullets_worker():
        prompt = _make_stream_prompt(sb_sys, sb_usr)
        parts: list[str] = []
        try:
            async for delta in stream_chat_completion(prompt, temperature=0.7):
                if delta:
                    parts.append(delta)
            full = "".join(parts)
            bullets = [
                b.lstrip("•·-●\uff65 ").strip()
                for b in full.split("\n")
                if b.strip()
            ]
            ai_result["summary_bullets"] = bullets
            await queue.put(
                _sse("partial", {"summary_bullets": bullets})
            )
        except Exception as e:
            logger.warning("AI bullets worker failed: %s", e)
        finally:
            await queue.put(None)

    # Launch all 5 workers
    tasks = [
        asyncio.create_task(
            _text_worker(_make_stream_prompt(ct_sys, ct_usr), "chart_top")
        ),
        asyncio.create_task(
            _text_worker(_make_stream_prompt(cb_sys, cb_usr), "chart_bottom")
        ),
        asyncio.create_task(
            _text_worker(_make_stream_prompt(sp_sys, sp_usr), "summary_paragraph")
        ),
        asyncio.create_task(
            _text_worker(_make_stream_prompt(ua_sys, ua_usr), "unshipped_analysis")
        ),
        asyncio.create_task(_bullets_worker()),
    ]

    # Drain queue — None is per-worker sentinel
    done_count = 0
    num_workers = 5
    while done_count < num_workers:
        item = await queue.get()
        if item is None:
            done_count += 1
        else:
            yield item

    # Wait for all tasks to complete (should be nearly done)
    await asyncio.gather(*tasks, return_exceptions=True)

    # Save to cache
    with _ai_cache_lock:
        _ai_cache[cache_key] = {"data": dict(ai_result), "ts": time.time()}

    yield _sse("done", ai_result)
