"""P4 T8 — FAIL-CLOSED document access gate (D6) + per-entry runtime
filtering (D7).

WHY THIS EXISTS: ``access_policy_service.resolve_user_policies`` FAILS
OPEN — no policy rows, a missing principal, or a resolution error all
return ``has_policies=False`` = FULL ACCESS. That default is right for
SQL narrowing but is exactly wrong for disclosing a restricted document.
This gate inverts the default at the document boundary while REUSING the
policy service for evaluation.

Five states (plan Part B):
- EXPLICITLY_AUTHORIZED — owner / policy grant / org-unrestricted member
  (the basis is always recorded) → ALLOW
- EXPLICITLY_DENIED — explicit policy deny, or the document belongs to
  another org
- NO_APPLICABLE_POLICY — a project-scoped document and the principal has
  no policy grant for it (fail-closed: "no rows" is NOT access)
- MISSING_PRINCIPAL — no authenticated user (API-key principals get no
  document-derived content by default)
- RESOLUTION_FAILURE — the resolver or gate itself blew up → DENY

admin ≠ document-reader: ``require_org_admin`` grants ROUTE access only.
This gate applies to admins like everyone else — an org-admin WITHOUT
source-document permission can neither approve a proposal nor see its
entries (the router calls this gate before every document-evidence
operation).
"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.models.knowledge_base import KnowledgeBase
from app.services.access_policy_service import resolve_user_policies

logger = logging.getLogger(__name__)

AUTHORIZED = "EXPLICITLY_AUTHORIZED"
DENIED = "EXPLICITLY_DENIED"
NO_POLICY = "NO_APPLICABLE_POLICY"
MISSING_PRINCIPAL = "MISSING_PRINCIPAL"
RESOLUTION_FAILURE = "RESOLUTION_FAILURE"

_STATES = (AUTHORIZED, DENIED, NO_POLICY, MISSING_PRINCIPAL,
           RESOLUTION_FAILURE)


def _decision(allowed: bool, state: str, basis: str | None = None) -> dict:
    return {"allowed": allowed, "state": state, "basis": basis}


def document_access_state(db: Session, user, kb: KnowledgeBase | None) -> dict:
    """Evaluate the D6 gate for ONE user against ONE source KB. Never
    raises — every internal failure lands in RESOLUTION_FAILURE (deny)."""
    try:
        if user is None or not (getattr(user, "id", None) or "").strip():
            # API-key principals get no document-derived content by default.
            return _decision(False, MISSING_PRINCIPAL,
                             "no authenticated user principal")
        if kb is None or not (kb.id or "").strip():
            return _decision(False, NO_POLICY, "source document missing")

        # Org wall first: an explicit isolation rule, never policy-resolved.
        if (kb.org_id or "") != (getattr(user, "org_id", "") or ""):
            return _decision(False, DENIED,
                             "document belongs to another org")

        if kb.created_by_id and kb.created_by_id == user.id:
            return _decision(True, AUTHORIZED, "owner")

        if not (kb.project_id or "").strip():
            # Org-level (unrestricted) document: any authenticated member of
            # the same org may access it — UNLESS an explicit deny policy
            # row targets this user + KB (explicit denies always win, D6).
            from app.models.resource_access_policy import ResourceAccessPolicy

            explicit_deny = (
                db.query(ResourceAccessPolicy)
                .filter(
                    ResourceAccessPolicy.user_id == user.id,
                    ResourceAccessPolicy.kb_id == kb.id,
                    ResourceAccessPolicy.mode == "deny",
                    ResourceAccessPolicy.is_deleted == False,  # noqa: E712
                )
                .first()
            )
            if explicit_deny is not None:
                return _decision(False, DENIED, "explicit policy deny")
            return _decision(True, AUTHORIZED, "org-unrestricted member")

        # Project-scoped document: policies are the ONLY path in. The
        # resolver's fail-open default-allow is re-read here as
        # NO_APPLICABLE_POLICY → DENY (the inversion that makes this gate
        # fail-closed). Admins are NOT bypassed (admin ≠ document-reader),
        # so is_admin stays False deliberately.
        resolved = resolve_user_policies(
            db, user_id=user.id, resource_type="project",
            resource_id=kb.project_id, bound_kb_ids=[kb.id],
            owner_id=None, is_admin=False,
        )
        if resolved.is_kb_fully_denied(kb.id):
            return _decision(False, DENIED, "explicit policy deny")
        if resolved.has_policies:
            return _decision(True, AUTHORIZED, "policy grant")
        return _decision(False, NO_POLICY,
                         "project-scoped document, no policy grant")
    except Exception as exc:  # noqa: BLE001 — every failure is a denial
        logger.warning("doc_access: resolution failed (%s) — denying", exc)
        return _decision(False, RESOLUTION_FAILURE, str(exc))


def assert_document_kb_access(db: Session, user, kb: KnowledgeBase) -> dict:
    """Raise PermissionError unless the principal may access the source
    document. Returns the decision (state + recorded basis) on allow."""
    out = document_access_state(db, user, kb)
    if not out["allowed"]:
        raise PermissionError(
            f"document access denied ({out['state']}): {out['basis']}"
        )
    return out


def filter_document_entries(db: Session, user,
                            provenance_json: dict | None) -> dict | None:
    """D7 per-entry runtime filter: keep only the document_provenance
    entries whose source document the principal may access. The object's
    other provenance keys pass through untouched; entries FAIL CLOSED
    (unknown KB, gate error, missing principal → hidden)."""
    base = dict(provenance_json or {})
    entries = base.get("document_provenance")
    if entries is None:
        return base

    if user is None or not (getattr(user, "id", None) or "").strip():
        base["document_provenance"] = []
        return base

    decisions: dict[str, bool] = {}
    kept = []
    for e in entries:
        try:
            kb_id = (e or {}).get("source_kb_id")
            allowed = decisions.get(kb_id)
            if allowed is None:
                kb = (
                    db.query(KnowledgeBase)
                    .filter(KnowledgeBase.id == kb_id)
                    .first()
                )
                out = document_access_state(db, user, kb)
                allowed = bool(out["allowed"])
                decisions[kb_id] = allowed
        except Exception as exc:  # noqa: BLE001 — fail CLOSED: hide the entry
            logger.warning(
                "doc_access: entry filter failed (%s) — entry hidden", exc)
            allowed = False
        if allowed:
            kept.append(e)
    base["document_provenance"] = kept
    return base
