"""CEO Morning Digest — LLM narrative layer.

Gated by ``ALERT_DIGEST_LLM_ENABLED`` (default True).
On any failure, returns None → caller falls back to template rendering.
"""

from __future__ import annotations

import logging

from app.config import settings

logger = logging.getLogger(__name__)


class DigestNarrator:
    """Generate a professional morning brief narrative via LLM.

    The narrator receives pre-computed ranked items with ¥ impact
    and produces a concise, actionable CEO-perspective digest.
    It is instructed to NEVER invent figures — only rephrase what
    is provided.
    """

    async def narrate(self, items: list[dict], date: str) -> str | None:
        """Generate LLM narrative for the morning digest.

        Args:
            items: Ranked list of digest items with keys:
                   product_name, alert_type, change_7d_pct,
                   impact_yuan_per_day, avg_daily_volume_tons, etc.
            date: ISO date string (e.g. "2026-08-12").

        Returns:
            Narrative text (markdown), or None on failure → template fallback.
        """
        if not settings.ALERT_DIGEST_LLM_ENABLED:
            return None

        try:
            from app.services.llm_service import call_llm

            items_text = "\n".join(
                f"- {item.get('product_name', 'Unknown')}: "
                f"{item.get('alert_type', 'alert')} "
                f"change={item.get('change_7d_pct', 'N/A')}% "
                f"impact=¥{item.get('impact_yuan_per_day', 'N/A')}/day "
                f"volume={item.get('avg_daily_volume_tons', 'N/A')} tons/day"
                for item in items
            )

            prompt = (
                "You are a CEO morning brief narrator for a petrochemical company.\n"
                f"Generate a concise, professional morning digest in Chinese for {date}.\n\n"
                "Key principles:\n"
                "- NEVER invent or modify any figures — use ONLY the numbers provided\n"
                "- Rank items by ¥ impact per day\n"
                "- Be actionable: suggest what the CEO should focus on today\n"
                "- Keep it under 300 words\n"
                "- Use bullet points for readability\n\n"
                f"Data:\n{items_text}\n\n"
                "Format:\n"
                f"## 晨报 — {date}\n\n"
                "[2-3 sentence executive summary]\n\n"
                "### 重点关注\n"
                "[ranked items with ¥ impact]\n\n"
                "### 建议行动\n"
                "[1-2 actionable recommendations]"
            )

            result = await call_llm(
                prompt=prompt,
                temperature=0.3,
                task_type="digest_narrator",
            )
            narrative = result.get("response", "")
            if not narrative or len(narrative.strip()) < 20:
                logger.warning("Digest narrator returned empty/short response, falling back to template")
                return None

            return narrative.strip()

        except Exception as e:
            logger.warning("Digest narrator failed, falling back to template: %s", e, exc_info=True)
            return None
