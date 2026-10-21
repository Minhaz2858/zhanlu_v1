"""cad_revision_create / cad_revision_release — engineering-change
intelligence (roadmap P3).

The ECO flow: parameter_set → verify_build → re-probe → dfm_check →
cad_revision_create (deterministic impact report, pending_approval) → user
approves in conversation → cad_revision_release(user_approved=True).
"""
from __future__ import annotations

import uuid


def _bad(error: str) -> dict:
    return {"success": False, "error": error}


def _ctx() -> dict:
    try:
        from app.services.agent_tools import TOOL_CONTEXT
        return TOOL_CONTEXT or {}
    except Exception:  # noqa: BLE001
        return {}


def _cad_revision_create(args: dict, db, user_id: str | None) -> dict:
    """Snapshot a proposed change and generate the deterministic impact report."""
    change_summary = str(args.get("change_summary") or "").strip()
    if not change_summary:
        return _bad("change_summary is required — what is changing and why")
    before = args.get("before_measurements")
    after = args.get("after_measurements")
    if not isinstance(after, dict) or not after:
        return _bad(
            "after_measurements (object) is required — re-probe the geometry "
            "after the change (fusion360_physical_properties) and pass the values"
        )
    if before is None:
        before = {}

    from app.services.dfm.impact import compute_impact
    report = compute_impact(
        before, after,
        before_dfm=args.get("before_dfm") if isinstance(args.get("before_dfm"), dict) else None,
        after_dfm=args.get("after_dfm") if isinstance(args.get("after_dfm"), dict) else None,
    )

    from app.models.cad_revision import CadRevision
    ctx = _ctx()
    prev = []
    if db is not None:
        prev = (
            db.query(CadRevision)
            .filter_by(conversation_id=ctx.get("conversation_id"))
            .all()
        )
    rev_no = (max((r.revision_no or 0) for r in prev) + 1) if prev else 1
    row = CadRevision(
        id=str(uuid.uuid4()),
        org_id=ctx.get("org_id") or "default-org",
        app_id=ctx.get("agent_app_id") or ctx.get("app_id") or "default-app",
        conversation_id=ctx.get("conversation_id"),
        agent_id=ctx.get("agent_app_id"),
        revision_no=rev_no,
        parent_revision_id=(prev[-1].id if prev else None),
        title=str(args.get("title") or change_summary)[:300],
        change_summary=change_summary[:2000],
        status="pending_approval",
        before_snapshot={"measurements": before,
                         "dfm": args.get("before_dfm")},
        after_snapshot={"measurements": after,
                        "dfm": args.get("after_dfm"),
                        "quote_ref": args.get("quote_ref")},
        impact_report=report,
        created_by_id=user_id,
    )
    if db is not None:
        db.add(row)
        db.commit()

    if report["requires_attention"]:
        note = ("WARNING: this change degrades manufacturability — review the "
                "impact report with the user BEFORE asking for approval.")
    else:
        note = ("No manufacturability degradation detected. Present the impact "
                "report and ask the user for explicit approval; then call "
                "cad_revision_release with user_approved=true.")
    return {
        "success": True,
        "revision_id": row.id,
        "revision_no": row.revision_no,
        "status": row.status,
        "impact_report": report,
        "message": (f"Revision {rev_no} created (pending_approval): "
                    f"{change_summary}. " + note),
    }


def _cad_revision_release(args: dict, db, user_id: str | None) -> dict:
    """Release a revision — REQUIRES the user's explicit approval."""
    rev_id = str(args.get("revision_id") or "").strip()
    if not rev_id:
        return _bad("revision_id is required")
    if args.get("user_approved") is not True:
        return _bad(
            "user_approved must be true — get the user's EXPLICIT approval in "
            "the conversation first (the impact report must have been shown). "
            "Never set this flag on your own judgment."
        )

    from app.models.cad_revision import CadRevision
    row = db.get(CadRevision, rev_id)
    if row is None:
        return _bad(f"revision '{rev_id}' not found")
    row.status = "released"
    row.approved_by_id = str(user_id) if user_id else None
    db.commit()
    return {
        "success": True,
        "revision_id": row.id,
        "revision_no": row.revision_no,
        "status": row.status,
        "message": (f"Revision {row.revision_no} released"
                    + (f" (approved by user {user_id})" if user_id else "")
                    + ". Export artifacts and update documentation as needed."),
    }


from app.services.tool_registry import registry  # noqa: E402

_MEASUREMENTS_DESC = (
    "Measured values in mm/g from fusion360_probe / "
    "fusion360_physical_properties — e.g. mass_g, wall_thickness_mm, "
    "bbox_x_mm, hole_edge_distance_mm. Only compare what you measured."
)

CAD_REVISION_CREATE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "cad_revision_create",
        "description": (
            "Create an engineering-change revision (ECO): store before/after "
            "measurement snapshots, compute the DETERMINISTIC impact report "
            "(changed metrics, DFM errors new/resolved), and set status "
            "pending_approval. Call AFTER applying the change "
            "(fusion360_parameter_set) and re-verifying "
            "(fusion360_verify_build + fusion360_dfm_check)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Short revision title."},
                "change_summary": {"type": "string", "description": "What is changing and why (e.g. 'bracket thickness 4mm → 5mm for stiffness')."},
                "before_measurements": {"type": "object", "description": _MEASUREMENTS_DESC},
                "after_measurements": {"type": "object", "description": _MEASUREMENTS_DESC + " (post-change — required)"},
                "before_dfm": {"type": "object", "description": "The fusion360_dfm_check result from BEFORE the change."},
                "after_dfm": {"type": "object", "description": "The fusion360_dfm_check result from AFTER the change."},
                "quote_ref": {"type": "string", "description": "Optional rfq_quote filename if a quote is affected."},
            },
            "required": ["change_summary", "after_measurements"],
        },
    },
}

CAD_REVISION_RELEASE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "cad_revision_release",
        "description": (
            "Release a pending revision — REQUIRES the user's explicit "
            "in-conversation approval (user_approved=true only after the user "
            "actually agrees; never from your own judgment)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "revision_id": {"type": "string", "description": "Revision id from cad_revision_create."},
                "user_approved": {"type": "boolean", "description": "true ONLY after the user explicitly approved."},
                "approver_note": {"type": "string", "description": "Optional note from the approver."},
            },
            "required": ["revision_id", "user_approved"],
        },
    },
}

registry.register(
    name="cad_revision_create",
    schema=CAD_REVISION_CREATE_SCHEMA,
    handler=_cad_revision_create,
    category="cad",
    toolset="cad",
    enabled_by_default=True,
    description="Create an ECO revision: impact report + pending_approval.",
    emoji="🔁",
)
registry.register(
    name="cad_revision_release",
    schema=CAD_REVISION_RELEASE_SCHEMA,
    handler=_cad_revision_release,
    category="cad",
    toolset="cad",
    enabled_by_default=True,
    description="Release a revision (requires explicit user approval).",
    emoji="✅",
)
