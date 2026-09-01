"""Single render entry point for chat-driven + exporter decks.

PHASE 1B functional core.  Every deck — whether produced by the in-process
exporter (``pptx_export``) or the sandbox renderer (``sandbox_runner``) —
is built from a ``DeckPlan`` through :func:`render_pptx_from_plan`, which:

1. Resolves the theme from the ``ExportContext`` into a flat hex-token dict.
2. Renders the plan to ``.pptx`` bytes via the shared ``layout_engine``
   (the single source of truth for all shape/text placement).
3. Audits the bytes with ``audit_deck`` (when ``PPT_AUDIT_ENABLED``).
4. Applies deterministic repairs (``repair_deck``, ≤ ``MAX_REPAIR_PASSES``).
5. Optionally runs one bounded LLM polish pass and re-renders.

Returns ``(pptx_bytes, audit_report)`` so callers can store the deck and the
report together.  The dispatcher NEVER raises for a render failure — on any
unexpected error it returns the best bytes it has plus a FAIL report, because
a deck render must not take down the chat turn.
"""

from __future__ import annotations

import asyncio
import logging
import tempfile
from pathlib import Path
from typing import Any, Optional, Union

from app.services.synexia.contracts import DeckPlan

logger = logging.getLogger(__name__)

# Hard cap on deterministic repair re-audits (defense against a
# non-converging repair loop — matches the quality loop's budget).
MAX_REPAIR_PASSES = 2


# ---------------------------------------------------------------------------
# ctx → layout_engine-compatible dict
# ---------------------------------------------------------------------------


def _build_layout_ctx(ctx: Any, *, plan: Any = None, user_message: str = "") -> dict[str, Any]:
    """Turn an ``ExportContext`` (or plain dict) into the dict ``layout_engine``
    expects: ``{"theme_tokens": {...hex...}, "style_recipe": "..."}``.

    Theme resolution now honors the 12 ``ThemePreset``s (via
    ``resolve_theme_tokens``): an explicit flat ``theme_tokens`` dict wins
    (brand kit), otherwise a named ``theme`` / keyword / deck_type auto-select
    resolves to a preset, falling back to the historical ``zhanlu-blue`` look.
    """
    from app.services.artifacts.themes import resolve_theme_tokens

    style_recipe = "sharp"
    # Provenance label threaded to layout_engine (source_citation footer).
    # ExportContext carries it as ``source``; plain-dict callers may use
    # either ``source`` or ``source_label``.
    source_label = ""

    if ctx is None:
        return {
            "theme_tokens": resolve_theme_tokens(None),
            "style_recipe": style_recipe,
            "source_label": source_label,
        }

    if isinstance(ctx, dict):
        style_recipe = ctx.get("style_recipe") or "sharp"
        if not isinstance(style_recipe, str):
            style_recipe = "sharp"
        source_label = str(ctx.get("source_label") or ctx.get("source") or "").strip()
        # Explicit flat tokens win as-is (brand kit / caller-supplied).
        raw = ctx.get("theme_tokens")
        if isinstance(raw, dict) and raw:
            return {
                "theme_tokens": dict(raw),
                "style_recipe": style_recipe,
                "source_label": source_label,
            }
        # Otherwise resolve preset / auto / default.
        return {
            "theme_tokens": resolve_theme_tokens(ctx, plan=plan, user_message=user_message),
            "style_recipe": style_recipe,
            "source_label": source_label,
        }

    # ExportContext dataclass path.
    style_recipe = getattr(ctx, "style_recipe", None) or "sharp"
    if not isinstance(style_recipe, str):
        style_recipe = "sharp"
    source_label = str(getattr(ctx, "source", "") or "").strip()
    raw = getattr(ctx, "theme_tokens", None)
    if isinstance(raw, dict) and raw:
        return {
            "theme_tokens": dict(raw),
            "style_recipe": style_recipe,
            "source_label": source_label,
        }
    return {
        "theme_tokens": resolve_theme_tokens(ctx, plan=plan, user_message=user_message),
        "style_recipe": style_recipe,
        "source_label": source_label,
    }


# ---------------------------------------------------------------------------
# Audit on bytes (audit_deck.audit reads from a path)
# ---------------------------------------------------------------------------


def _audit_bytes(data: bytes) -> dict[str, Any]:
    """Write ``data`` to a temp file and run the semantic audit.

    Never raises — returns a synthetic FAIL report on any tooling error so
    the dispatcher can always return a structured result.
    """
    try:
        from app.services.artifacts.audits.audit_deck import audit
    except Exception as exc:  # pragma: no cover — import guard
        logger.warning("render_dispatcher: audit module unavailable: %s", exc)
        return {
            "tool": "audit_deck",
            "status": "WARN",
            "summary": {"pass": 0, "warn": 1, "fail": 0, "total": 1},
            "rules": [
                {
                    "id": "audit_unavailable",
                    "title": "Audit skipped",
                    "level": "WARN",
                    "detail": str(exc),
                    "evidence": [],
                }
            ],
        }

    try:
        with tempfile.NamedTemporaryFile(suffix=".pptx", delete=False) as tf:
            tf.write(data)
            tmp_path = tf.name
        report = audit(tmp_path)
    except Exception as exc:  # noqa: BLE001 — audit must not break a render
        logger.warning("render_dispatcher: audit raised: %s", exc)
        return {
            "tool": "audit_deck",
            "status": "WARN",
            "summary": {"pass": 0, "warn": 1, "fail": 0, "total": 1},
            "rules": [
                {
                    "id": "audit_error",
                    "title": "Audit error",
                    "level": "WARN",
                    "detail": str(exc),
                    "evidence": [],
                }
            ],
        }
    finally:
        try:
            Path(tmp_path).unlink(missing_ok=True)
        except Exception:
            pass
    return report


def _audit_enabled() -> bool:
    try:
        from app.config import settings

        return bool(getattr(settings, "PPT_AUDIT_ENABLED", False))
    except Exception:
        return False


def _blocking_enabled() -> bool:
    try:
        from app.config import settings

        return bool(getattr(settings, "PPT_AUDIT_BLOCKING_ENABLED", False))
    except Exception:
        return False


def _polish_enabled() -> bool:
    try:
        from app.config import settings

        return bool(getattr(settings, "PPT_LLM_POLISH_ENABLED", False))
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Render + quality loop
# ---------------------------------------------------------------------------


def _render_once(plan: Any, rows: list[dict], ctx: dict) -> bytes:
    from app.services.artifacts.layout_engine import render

    return render(plan, rows or [], ctx)


def _repair_bytes(data: bytes, report: dict[str, Any]) -> Optional[bytes]:
    """Apply deterministic repairs for FAIL rules. Returns repaired bytes or
    None when nothing was fixable.
    """
    try:
        from app.services.artifacts.repairs import repair_artifact

        return repair_artifact("pptx", data, report)
    except Exception as exc:  # noqa: BLE001 — repair is best-effort
        logger.warning("render_dispatcher: repair raised: %s", exc)
        return None


async def _maybe_polish(plan: DeckPlan, rows: list[dict], user_message: str) -> DeckPlan:
    """One bounded LLM polish pass (no-op when disabled or on any failure)."""
    if not _polish_enabled():
        return plan
    try:
        from app.services.artifacts.copy_polish import polish_deck

        return await polish_deck(plan, rows=rows, user_message=user_message)
    except Exception as exc:  # noqa: BLE001 — polish must never break a render
        logger.warning("render_dispatcher: polish raised: %s", exc)
        return plan


def _empty_report() -> dict[str, Any]:
    return {
        "tool": "audit_deck",
        "status": "PASS",
        "summary": {"pass": 0, "warn": 0, "fail": 0, "total": 0},
        "rules": [],
    }


async def render_pptx_from_plan(
    plan: Union[DeckPlan, dict],
    rows: list[dict[str, Any]],
    ctx: Any = None,
    *,
    user_message: str = "",
) -> tuple[bytes, dict[str, Any]]:
    """Render a ``DeckPlan`` to ``.pptx`` bytes with audit + repair + polish.

    Args:
        plan: A :class:`DeckPlan` (or a dict with the same shape).
        rows: The underlying data rows (used by ``data_table`` / chart layouts).
        ctx: An :class:`ExportContext` (or plain dict) carrying theme info.
        user_message: The user's original intent, passed to the polish pass.

    Returns:
        ``(pptx_bytes, audit_report_dict)``.  On catastrophic render failure
        returns ``(b"", FAIL_report)`` so callers can log + surface gracefully.
    """
    # Coerce a plain dict to a DeckPlan so the layout engine / polish path
    # get a valid, validated structure.  A malformed plan falls back to empty.
    plan_obj: DeckPlan
    if isinstance(plan, DeckPlan):
        plan_obj = plan
    else:
        try:
            plan_obj = DeckPlan.model_validate(plan)
        except Exception as exc:
            logger.warning(
                "render_dispatcher: invalid plan (%s); using empty deck",
                exc,
            )
            plan_obj = DeckPlan(title="", slides=[])

    # Resolve theme tokens (presets / auto-select / brand kit) now that we
    # have the validated plan + the user's original intent.
    layout_ctx = _build_layout_ctx(ctx, plan=plan_obj, user_message=user_message)

    # 1) First render.
    try:
        data = _render_once(plan_obj, rows, layout_ctx)
    except Exception as exc:  # noqa: BLE001 — never crash the chat turn
        logger.error("render_dispatcher: render failed: %s", exc, exc_info=True)
        fail = _empty_report()
        fail["status"] = "FAIL"
        fail["summary"] = {"pass": 0, "warn": 0, "fail": 1, "total": 1}
        fail["rules"] = [
            {
                "id": "render_error",
                "title": "Render error",
                "level": "FAIL",
                "detail": str(exc),
                "evidence": [],
            }
        ]
        return b"", fail

    # 2) Audit + deterministic repair loop.
    report = _empty_report()
    if _audit_enabled():
        report = _audit_bytes(data)
        passes = 0
        while report.get("status") == "FAIL" and passes < MAX_REPAIR_PASSES:
            repaired = _repair_bytes(data, report)
            if not repaired or repaired == data:
                break
            data = repaired
            report = _audit_bytes(data)
            passes += 1
        # Log the final audit outcome (matches existing audit logging style).
        if report.get("status") == "FAIL":
            for rule in report.get("rules", []):
                if rule.get("level") == "FAIL":
                    logger.warning(
                        "render_dispatcher: deck audit FAIL rule=%s", rule.get("id")
                    )
    else:
        logger.debug("render_dispatcher: PPT_AUDIT_ENABLED=False; skipping audit/repair")

    # 2b) Blocking audit gate — a deck that still FAILs after the repair loop
    # is NOT delivered: return empty bytes + the FAIL report so callers store
    # nothing and can surface the failure to the user.
    if report.get("status") == "FAIL" and _blocking_enabled():
        fail_count = report.get("summary", {}).get("fail", 0)
        logger.error(
            "render_dispatcher: blocking audit gate — deck FAIL after repair; "
            "refusing to deliver (%d fail rules)",
            fail_count,
        )
        return b"", report

    # 3) Optional one-shot polish → re-render.
    polished = await _maybe_polish(plan_obj, rows, user_message)
    if polished is not plan_obj and polished is not None:
        try:
            data = _render_once(polished, rows, layout_ctx)
            # Re-audit the polished deck so the returned report reflects the
            # bytes the caller will actually store.
            if _audit_enabled():
                report = _audit_bytes(data)
                # The gate applies to this final state too: a polished deck
                # that FAILs the re-audit must not be delivered either.
                if report.get("status") == "FAIL" and _blocking_enabled():
                    fail_count = report.get("summary", {}).get("fail", 0)
                    logger.error(
                        "render_dispatcher: blocking audit gate — polished deck "
                        "FAIL; refusing to deliver (%d fail rules)",
                        fail_count,
                    )
                    return b"", report
        except Exception as exc:  # noqa: BLE001 — keep the pre-polish bytes
            logger.warning(
                "render_dispatcher: polish re-render failed; keeping original: %s", exc
            )

    return data, report


def _apply_repair_patches(plan: DeckPlan, audit_report: dict[str, Any]) -> DeckPlan:
    """Map a FAIL audit report to a (best-effort) repaired DeckPlan.

    The deterministic ``repair_deck`` operates on rendered *bytes*, not the
    plan; this helper is the plan-side hook the orchestrator/sandbox can use
    to shorten overloaded bullets (``density_6x6`` FAIL) by delegating to the
    polish pass.  It returns the plan unchanged when disabled.
    """
    if not audit_report:
        return plan
    fail_ids = {
        r.get("id")
        for r in audit_report.get("rules", [])
        if isinstance(r, dict) and r.get("level") == "FAIL"
    }
    # Only the density rule is plan-repairable (shorten/split bullets).
    if "density_6x6" not in fail_ids:
        return plan  # nothing to do on the plan side
    try:
        import asyncio

        from app.services.artifacts.copy_polish import polish_deck

        return asyncio.get_event_loop().run_until_complete(
            polish_deck(plan, user_message="shorten dense bullets to fit")
        )
    except Exception as exc:  # noqa: BLE001 — must not break the caller
        logger.warning("render_dispatcher: _apply_repair_patches failed: %s", exc)
        return plan


def render_pptx_from_plan_sync(
    plan: Union[DeckPlan, dict],
    rows: list[dict[str, Any]],
    ctx: Any = None,
    *,
    user_message: str = "",
) -> tuple[bytes, dict[str, Any]]:
    """Blocking wrapper around :func:`render_pptx_from_plan`.

    `pptx_export.render_deck` and `ExportService._render_deck_pipeline` are
    synchronous, so they call this.  ``ExportService`` already has a
    ``_run_coro`` helper for the case where an event loop is already running
    (async chat path) — but this default drives the coroutine directly when no
    loop is active, mirroring that contract.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(
            render_pptx_from_plan(plan, rows, ctx, user_message=user_message)
        )

    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(
            asyncio.run,
            render_pptx_from_plan(plan, rows, ctx, user_message=user_message),
        ).result()


__all__ = [
    "render_pptx_from_plan",
    "render_pptx_from_plan_sync",
    "RenderError",
]


class RenderError(RuntimeError):
    """Raised by callers that want an exception instead of a ``(b"", report)``
    tuple when the deck cannot be rendered at all.
    """

    def __init__(self, report: dict[str, Any]):
        super().__init__("deck render failed")
        self.report = report
