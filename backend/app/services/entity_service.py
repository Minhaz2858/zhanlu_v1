"""Generic entity service — CRUD operations shared by all entity routers.

This module provides functions that operate on any SQLAlchemy model class,
implementing the full Base44 entity API contract: list, filter, get, create,
update, soft-delete, delete-many, bulk-create, bulk-update, update-many, restore.
"""

from typing import Any, Optional
from datetime import datetime, date, timezone
from sqlalchemy.orm import Session

from app.config import settings
from app.utils.query_parser import parse_query
from app.utils.sort_parser import parse_sort


def _tenant_filters(model):
    """Return org_id/app_id filter conditions for multi-tenant isolation.

    Applied to every query so a tenant can never see another tenant's
    rows, even if the auth layer is bypassed. Models that lack these
    columns (pre-migration) are unaffected.
    """
    conditions = []
    if hasattr(model, "org_id"):
        conditions.append(model.org_id == settings.DEFAULT_ORG_ID)
    if hasattr(model, "app_id"):
        conditions.append(model.app_id == settings.DEFAULT_APP_ID)
    return conditions


def _apply_tenant(model, query):
    """Apply tenant isolation filters to a SQLAlchemy query."""
    for cond in _tenant_filters(model):
        query = query.filter(cond)
    return query


def _apply_owner(model, query, owner_id, include_shared=False, db=None):
    """Apply per-user ownership isolation (created_by_id filter).

    This is the "real software" data-isolation layer: a user only ever sees
    rows they created, never another user's rows. ``owner_id`` is the current
    identity's id — a real ``User.id`` when logged in, or the per-browser
    anonymous UUID for guests (so guests still get their own private scope).

    No-op when ``owner_id`` is None (no identity available) or the model has
    no ``created_by_id`` column. Routers only pass ``owner_id`` for entities
    that are genuinely user-owned (chat sessions, messages, conversations,
    automations) — shared/system entities (agents, knowledge bases,
    marketplace) deliberately pass None so they stay visible to everyone.

    When the model has an ``is_system`` column alongside ``created_by_id``,
    system-owned rows (is_system=True, created_by_id=NULL) are included
    regardless of the owner_id. This ensures system agents like
    general_assistant appear in every user's list endpoints while
    user-created rows remain properly scoped.

    Platform-shipped but user-facing rows (is_system=False, created_by_id=NULL)
    are also visible to all users. These are seeded by ``ensure_system_agents``
    with an explicit is_system=False so they appear in
    the agent picker and "My Space" — they are platform templates that every
    user can select and chat with, but are not auto-managed by the runtime.

    When ``include_shared=True`` and the model has a ``resource_type`` column
    (Project, AgentApp), rows that were explicitly shared with the current
    user via ``ResourceShare`` are also included.  This only affects the
    **read path** — write paths (update/delete/restore) keep the default
    ``include_shared=False`` so edit/delete remain owner-only.
    """
    if owner_id is None:
        return query

    # User is self-scoped by id — check BEFORE the created_by_id branch
    # because User inherits created_by_id from TimestampedBase (it is a
    # column on the table for audit, but ownership is through primary key).
    if getattr(model, "__name__", "") == "User":
        query = query.filter(model.id == owner_id)
        return query

    if hasattr(model, "created_by_id"):
        # Shared-with-me subquery — only for shareable models on the read path.
        shared_clause = None
        if include_shared and hasattr(model, "resource_type") and db is not None:
            from app.models.resource_share import ResourceShare
            model_name = getattr(model, "__name__", "")
            resource_type = "project" if model_name == "Project" else "agent"
            from sqlalchemy import select as sa_select
            shared_ids = sa_select(ResourceShare.resource_id).where(
                ResourceShare.resource_type == resource_type,
                ResourceShare.shared_with_user_id == owner_id,
                ResourceShare.is_deleted == False,
            )
            shared_clause = model.id.in_(shared_ids)

        if hasattr(model, "is_system"):
            base_conditions = [
                model.created_by_id == owner_id,
                model.is_system == True,
                model.created_by_id.is_(None) & (model.is_system == False),
            ]
            if shared_clause is not None:
                base_conditions.append(shared_clause)
            from sqlalchemy import or_
            query = query.filter(or_(*base_conditions))
        else:
            if shared_clause is not None:
                from sqlalchemy import or_
                query = query.filter(
                    or_(model.created_by_id == owner_id, shared_clause)
                )
            else:
                query = query.filter(model.created_by_id == owner_id)
    return query


def _get_valid_fields(model) -> set:
    """Return the set of valid column names for a model (excluding internal fields)."""
    return {col.name for col in model.__table__.columns} - {"is_deleted", "password_hash"}


# Server-managed fields that must NEVER be accepted from clients. These are
# set exclusively by the backend (tenant scoping, ownership stamping, PKs).
# ``password_hash``/``is_deleted`` are already excluded by ``_get_valid_fields``.
_IMMUTABLE_FIELDS = {"id", "created_by_id", "org_id", "app_id", "resource_type"}

# Per-model privileged fields clients must never write. ``role`` on User is the
# privilege-escalation vector — without this, any authenticated user could
# PUT /entities/User/{id} with {"role": "admin"} and gain admin powers.
_MODEL_IMMUTABLE_FIELDS = {
    "User": {"role"},
}

# Models whose ``created_by_id`` is treated as a client-claimable marker
# (not a security boundary). The Skills tab UI marks a tool as "added
# to My Skills" by sending ``{ created_by_id: <user.id> }`` on the
# existing tool row — see ``Toolkit.addToMySkills`` on the frontend.
#
# Tool is NOT in ``USER_SCOPED_ENTITIES`` (it is a global catalog, not
# per-user isolated data), so ``created_by_id`` on Tool is a UI marker,
# not an isolation stamp. The general entity API still strips it via
# ``_IMMUTABLE_FIELDS`` to keep every other model safe; the dedicated
# ``_apply_claim_updates`` helper below re-allows it ONLY on Tool with
# strict rules.
#
# 2026-08-28: Without this fix, clicking the ``+`` on a Skills card
# silently no-ops because the backend strips ``created_by_id``. The
# optimistic UI shows a green ✓ for the current session (held in a
# frontend ref) but on reload or when "My Skills" is opened, the
# filter ``tools.filter(x => x.created_by_id === ownerId)`` returns
# empty — exactly the "No skills yet. Click Create to add one." state.
_CLAIMABLE_MODELS = {"Tool"}
_CLAIMABLE_FIELDS = {"created_by_id"}


def _filter_data(model, data: dict) -> dict:
    """Filter incoming data to only valid, client-writable columns.

    Strips server-managed fields (``id``, ``created_by_id``, ``org_id``,
    ``app_id``) and model-specific privileged fields (``role`` on User) so the
    generic entity API cannot be abused for privilege escalation, ownership
    reassignment, or tenant tampering.
    """
    valid = _get_valid_fields(model)
    immutable = _IMMUTABLE_FIELDS | _MODEL_IMMUTABLE_FIELDS.get(
        getattr(model, "__name__", ""), set()
    )
    return {k: v for k, v in data.items() if k in valid and k not in immutable}


def _coerce_for_model(model, key: str, value):
    """Coerce string datetimes to ``datetime`` so SQLAlchemy/SQLite accept them.

    The Base44 SDK and our frontend echo the full entity back on every
    update, including read-only timestamp fields serialized as ISO-8601
    strings. Without this conversion, ``UPDATE`` raises a TypeError on
    SQLite ("DateTime type only accepts Python datetime and date objects").
    """
    if value is None or isinstance(value, (datetime, date)):
        return value
    if not isinstance(value, str):
        return value
    dt_columns = {col.name for col in model.__table__.columns
                  if col.type.__class__.__name__ in ("DateTime", "Date", "DateTimeField")}
    if key not in dt_columns:
        return value
    s = value.strip()
    if not s:
        return None
    # Python 3.11+: fromisoformat handles Z, +08:00, and fractional seconds natively
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return value


def _apply_updates(record, model, updates: dict) -> None:
    """Apply filtered, coerced updates to a SQLAlchemy record."""
    for key, value in updates.items():
        setattr(record, key, _coerce_for_model(model, key, value))


def _apply_claim_updates(record, model, data: dict, claim_user_id: str | None) -> None:
    """Apply ownership-claim semantics for ``Tool`` rows.

    The Skills tab UI marks a tool as "added to My Skills" by sending
    ``{ created_by_id: <user.id> }`` on the existing tool row. The generic
    entity API strips ``created_by_id`` via ``_IMMUTABLE_FIELDS`` because
    for ChatSession, Project, KnowledgeBase, etc. that field IS a security
    boundary (it is the user-isolation stamp that the row-level scoping
    filter in ``_apply_owner`` relies on).

    On ``Tool`` it is NOT a security boundary — ``Tool`` is not in
    ``USER_SCOPED_ENTITIES``, it is a global catalog, and the field is
    re-used purely as a UI marker for "this user added this skill to
    their My Skills view".  The frontend renders the My Skills modal
    with ``tools.filter(x => x.created_by_id === ownerId)`` so the claim
    MUST be persistable, otherwise the click is a no-op.

    Rules enforced here:
      * Only ``Tool`` accepts a ``created_by_id`` write (other entities
        stay strictly immutable).
      * The new value MUST equal the requesting user's id. No
        cross-user reassignment, no nulling.
      * Re-claim is allowed: any user can claim a Tool the previous
        user already claimed. This matches the intent — "whoever
        clicked it last owns it now" — and is safe because Tool is
        not used as an isolation boundary.
      * ``claim_user_id`` is required (must be an authenticated user).
        This is independent of ``owner_id`` because ``Tool`` is not in
        ``USER_SCOPED_ENTITIES`` — the router passes ``owner_id=None``
        for non-scoped entities (so they stay visible to everyone for
        reads), but the claim still needs to know "who clicked the
        button" — that's ``claim_user_id``. See ``update_record``.
    """
    if claim_user_id is None:
        return
    if getattr(model, "__name__", "") not in _CLAIMABLE_MODELS:
        return
    for field in _CLAIMABLE_FIELDS:
        if field not in data:
            continue
        new_value = data[field]
        if str(new_value) != str(claim_user_id):
            # Reject cross-user reassignment — silently ignore.  We don't
            # 400 here because the frontend may send a normal Tool.update
            # payload that happens to include a server-managed field; the
            # correct behavior is to drop the disallowed sub-update.
            continue
        setattr(record, field, new_value)


_SHAREABLE_MODELS = {"Project", "AgentApp"}


def _annotate_access(model, records: list, owner_id: str | None, db,
                      is_admin: bool = False) -> list:
    """Annotate serialized records with ``can_edit`` and ``is_shared_with_me``.

    Only called for shareable models (Project, AgentApp) on the read path so
    the frontend can decide whether to show edit/share/delete buttons.  The
    annotation is appended to the dict returned by ``to_dict()``; it does not
    exist on the ORM model itself.

    ``can_edit`` → True when the caller owns the record (created_by_id match)
    **or** when the caller is an org admin (``is_admin=True``).
    ``is_shared_with_me`` → True when the caller received a ResourceShare grant
    but does NOT own the record (i.e. it was shared *to* them).
    """
    if not records or owner_id is None:
        for r in records:
            r["can_edit"] = False
            r["is_shared_with_me"] = False
        return records

    # ── Admin short-circuit: full access to everything ──
    if is_admin:
        for r in records:
            r["can_edit"] = True
            r["is_shared_with_me"] = False
        return records

    model_name = getattr(model, "__name__", "")
    if model_name not in _SHAREABLE_MODELS or not hasattr(model, "resource_type"):
        return records

    record_ids = [r["id"] for r in records]
    resource_type = "project" if model_name == "Project" else "agent"

    from app.models.resource_share import ResourceShare
    shared_ids: set[str] = {
        row[0] for row in db.query(ResourceShare.resource_id).filter(
            ResourceShare.resource_type == resource_type,
            ResourceShare.resource_id.in_(record_ids),
            ResourceShare.shared_with_user_id == owner_id,
            ResourceShare.is_deleted == False,
        ).all()
    }

    for r in records:
        r["can_edit"] = r.get("created_by_id") == owner_id
        r["is_shared_with_me"] = (
            r["id"] in shared_ids
            and r.get("created_by_id") != owner_id
        )

    return records


def list_records(
    model, db: Session, sort: str | None = None,
    limit: int | None = None, skip: int | None = None,
    owner_id: str | None = None,
    include_shared: bool = False,
    is_admin: bool = False,
) -> list:
    """List non-deleted records with optional sorting and pagination.

    ``owner_id`` (optional) scopes the result to rows the identity created —
    used for user-owned entities so each user sees only their own data.

    ``include_shared`` (optional) adds rows shared with the owner via
    ResourceShare — only for shareable models (Project, AgentApp) on reads.
    """
    query = db.query(model).filter(model.is_deleted == False)
    query = _apply_tenant(model, query)
    query = _apply_owner(model, query, owner_id,
                         include_shared=include_shared, db=db)
    query = query.order_by(*parse_sort(model, sort))
    if skip:
        query = query.offset(skip)
    if limit:
        query = query.limit(limit)
    records = [r.to_dict() for r in query.all()]
    return _annotate_access(model, records, owner_id, db, is_admin=is_admin)


def filter_records(
    model, db: Session, q: str | None = None,
    sort: str | None = None, limit: int | None = None,
    skip: int | None = None, owner_id: str | None = None,
    include_shared: bool = False,
    is_admin: bool = False,
) -> list:
    """Filter non-deleted records by MongoDB-style query with sorting and pagination.

    ``owner_id`` (optional) additionally scopes to the identity's own rows.

    ``include_shared`` (optional) adds rows shared with the owner via
    ResourceShare — only for shareable models (Project, AgentApp) on reads.
    """
    query = db.query(model).filter(model.is_deleted == False)
    query = _apply_tenant(model, query)
    query = _apply_owner(model, query, owner_id,
                         include_shared=include_shared, db=db)
    conditions = parse_query(model, q)
    for cond in conditions:
        query = query.filter(cond)
    query = query.order_by(*parse_sort(model, sort))
    if skip:
        query = query.offset(skip)
    if limit:
        query = query.limit(limit)
    records = [r.to_dict() for r in query.all()]
    return _annotate_access(model, records, owner_id, db, is_admin=is_admin)


def get_record(model, record_id: str, db: Session, owner_id: str | None = None,
                include_shared: bool = False, is_admin: bool = False) -> dict | None:
    """Get a single non-deleted record by ID.

    ``owner_id`` (optional) scopes the lookup to the identity's own rows —
    a non-owner gets None (record hidden), which the router surfaces as 404.

    ``include_shared`` (optional) also matches records shared with the
    owner via ResourceShare — only for shareable models (Project, AgentApp).
    """
    record = _apply_owner(model, _apply_tenant(model, db.query(model).filter(
        model.id == record_id, model.is_deleted == False
    )), owner_id, include_shared=include_shared, db=db).first()
    if not record:
        return None
    result = record.to_dict()
    _annotate_access(model, [result], owner_id, db, is_admin=is_admin)
    return result


def create_record(model, data: dict, db: Session, created_by_id: str | None = None,
                  extra_fields: dict | None = None) -> dict:
    """Create a new record, filtering to valid columns only.

    ``extra_fields`` (optional) are injected server-side AFTER filtering
    (they bypass _filter_data).  Used to stamp ``resource_type`` on Project
    and AgentApp from the creator's role without the client needing to know
    about the column.
    """
    filtered = _filter_data(model, data)
    if created_by_id and "created_by_id" in _get_valid_fields(model):
        filtered["created_by_id"] = created_by_id
    if extra_fields:
        filtered.update(extra_fields)

    record = model(**filtered)
    db.add(record)
    db.commit()
    db.refresh(record)
    return record.to_dict()


def update_record(model, record_id: str, data: dict, db: Session, owner_id: str | None = None,
                  claim_user_id: str | None = None) -> dict | None:
    """Update a non-deleted record by ID.

    ``owner_id`` (optional) scopes the update to the identity's own rows —
    a non-owner gets None (record hidden → router returns 404).

    ``claim_user_id`` (optional, default None) is the authenticated user's
    id used by ``_apply_claim_updates`` to enforce claim semantics on
    ``Tool`` rows.  Routers should pass ``getattr(user, "id", None)``
    whenever the entity is in ``_CLAIMABLE_MODELS``. This is independent
    of ``owner_id`` because ``Tool`` is not user-scoped (the router passes
    ``owner_id=None`` for non-scoped entities so reads stay global) — but
    the claim still needs to know who clicked the button.
    """
    record = _apply_owner(model, _apply_tenant(model, db.query(model).filter(
        model.id == record_id, model.is_deleted == False
    )), owner_id).first()
    if not record:
        return None

    filtered = _filter_data(model, data)
    # Apply ownership-claim updates AFTER the general filter — the filter
    # strips ``created_by_id`` because for most entities it IS a
    # security boundary, but for ``Tool`` we re-allow it under the
    # strict claim rules in ``_apply_claim_updates``.  See the helper
    # docstring for the rationale (2026-08-28 Skills-tab bug fix).
    _apply_claim_updates(record, model, data, claim_user_id)
    _apply_updates(record, model, filtered)

    record.updated_date = datetime.now(timezone.utc)
    db.commit()
    db.refresh(record)
    return record.to_dict()


def soft_delete_record(model, record_id: str, db: Session, owner_id: str | None = None) -> bool:
    """Soft-delete a record by ID (set is_deleted=True).

    ``owner_id`` (optional) scopes the delete to the identity's own rows —
    a non-owner gets False (record hidden → router returns 404).

    If the record has ``is_system=True`` on a model that carries the
    column, the delete is refused and ``False`` is returned. System
    agents are platform-critical runtime components that the chat
    auto-select path depends on; deleting them would break silent
    auto-selection. The router
    treats False as 404, so callers cannot distinguish a hidden
    system agent from a missing one — which is the intended behavior.
    """
    record = _apply_owner(model, _apply_tenant(model, db.query(model).filter(
        model.id == record_id, model.is_deleted == False
    )), owner_id).first()
    if not record:
        return False
    if getattr(record, "is_system", False) is True:
        return False
    record.is_deleted = True
    record.updated_date = datetime.now(timezone.utc)
    db.commit()
    return True


def delete_many_records(model, query_data: dict, db: Session, owner_id: str | None = None) -> int:
    """Soft-delete multiple records matching the query. Returns count deleted.

    ``owner_id`` (optional) scopes the delete to the identity's own rows —
    a non-owner's matching rows are invisible (never deleted). This closes
    the mass-IDOR gap on user-scoped entities.

    Records with ``is_system=True`` are silently excluded from the bulk
    delete — they are platform-managed and the count returned does not
    include them. (See soft_delete_record for the full rationale.)
    """
    query = db.query(model).filter(model.is_deleted == False)
    query = _apply_tenant(model, query)
    query = _apply_owner(model, query, owner_id)

    if query_data and isinstance(query_data, dict):
        import json
        q_str = json.dumps(query_data)
        conditions = parse_query(model, q_str)
        for cond in conditions:
            query = query.filter(cond)

    records = query.all()
    count = 0
    for record in records:
        if getattr(record, "is_system", False) is True:
            continue
        record.is_deleted = True
        record.updated_date = datetime.now(timezone.utc)
        count += 1

    db.commit()
    return count


def bulk_create_records(model, data_list: list, db: Session, created_by_id: str | None = None) -> list:
    """Create multiple records at once."""
    results = []
    for data in data_list:
        filtered = _filter_data(model, data)
        if created_by_id and "created_by_id" in _get_valid_fields(model):
            filtered["created_by_id"] = created_by_id
        record = model(**filtered)
        db.add(record)
        results.append(record)

    db.commit()
    for record in results:
        db.refresh(record)

    return [r.to_dict() for r in results]


def bulk_update_records(model, data_list: list, db: Session, owner_id: str | None = None) -> list:
    """Update multiple records by ID. Each item must include 'id'.

    ``owner_id`` (optional) scopes each update to the identity's own rows —
    a non-owner's record is skipped (invisible). Closes the mass-IDOR gap.
    """
    results = []
    for data in data_list:
        record_id = data.get("id")
        if not record_id:
            continue
        record = _apply_owner(model, _apply_tenant(model, db.query(model).filter(
            model.id == record_id, model.is_deleted == False
        )), owner_id).first()
        if not record:
            continue

        filtered = _filter_data(model, data)
        filtered.pop("id", None)
        _apply_updates(record, model, filtered)

        record.updated_date = datetime.now(timezone.utc)
        results.append(record)

    db.commit()
    for record in results:
        db.refresh(record)

    return [r.to_dict() for r in results]


def update_many_records(
    model, query_data: dict, update_data: dict, db: Session,
    owner_id: str | None = None,
) -> int:
    """Update multiple records matching query. Supports $set, $inc, $push, $pull.

    ``owner_id`` scopes writes to the caller's own rows (prevents mass-IDOR).
    Returns count of updated records.
    """
    query = db.query(model).filter(model.is_deleted == False)
    query = _apply_tenant(model, query)
    query = _apply_owner(model, query, owner_id)
    import json
    q_str = json.dumps(query_data) if isinstance(query_data, dict) else query_data
    conditions = parse_query(model, q_str)
    for cond in conditions:
        query = query.filter(cond)

    records = query.all()
    count = 0
    for record in records:
        # Apply MongoDB-style update operators — route through _filter_data
        # to prevent privilege escalation (e.g. $set: {"role": "admin"}).
        if "$set" in update_data:
            for key, value in _filter_data(model, update_data["$set"]).items():
                col = getattr(model, key, None)
                if col is not None:
                    setattr(record, key, _coerce_for_model(model, key, value))
        if "$inc" in update_data:
            for key, value in _filter_data(model, update_data["$inc"]).items():
                current = getattr(record, key, 0) or 0
                col = getattr(model, key, None)
                if col is not None:
                    setattr(record, key, current + value)
        # $push and $pull operate on JSON array fields.
        # Build NEW list objects so SQLAlchemy flags the JSON column as dirty.
        if "$push" in update_data:
            for key, value in _filter_data(model, update_data["$push"]).items():
                current = list(getattr(record, key, None) or [])
                if isinstance(value, list):
                    current.extend(value)
                else:
                    current.append(value)
                setattr(record, key, current)
        if "$pull" in update_data:
            for key, value in _filter_data(model, update_data["$pull"]).items():
                current = list(getattr(record, key, None) or [])
                current = [x for x in current if x != value]
                setattr(record, key, current)

        record.updated_date = datetime.now(timezone.utc)
        count += 1

    db.commit()
    return count


def restore_record(model, record_id: str, db: Session, owner_id: str | None = None) -> dict | None:
    """Restore a soft-deleted record by ID (set is_deleted=False).

    ``owner_id`` (optional) scopes the restore to the identity's own rows —
    a non-owner gets None (record hidden → router returns 404).
    """
    record = _apply_owner(model, _apply_tenant(model, db.query(model).filter(
        model.id == record_id, model.is_deleted == True
    )), owner_id).first()
    if not record:
        return None
    record.is_deleted = False
    record.updated_date = datetime.now(timezone.utc)
    db.commit()
    db.refresh(record)
    return record.to_dict()
