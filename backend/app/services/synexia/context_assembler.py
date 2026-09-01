"""Context assembler — builds the ContextManifest for the FSM.

The ContextManifest is the assembled context for an execution:
- Conversation history (recent messages)
- Agent memory (relevant memories)
- Knowledge base entries (if applicable)
- User attachments
- System context (agent config, available tools/skills)

Token budget is enforced BEFORE the LLM call, not after.
"""

import logging
from typing import Optional

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# Token budget for context (conservative default)
MAX_CONTEXT_TOKENS = 50000

# Cumulative budget for re-injecting files uploaded in EARLIER turns
# (see collect_historical_file_urls). Files stay readable for the whole
# conversation (Kimi/GPT parity), but a long file-heavy conversation
# must not blow the context window — once this budget is exhausted the
# remaining historical files are noted by name only.
MAX_HISTORICAL_EXTRACT_CHARS = 60000

# Scan bounds for historical file re-injection: only look back this many
# user messages, and cap the number of distinct file urls re-read.
_HISTORICAL_MAX_USER_MESSAGES = 8
_HISTORICAL_MAX_URLS = 12


def collect_historical_file_urls(
    conv_messages: Optional[list],
    exclude: Optional[list] = None,
) -> list[str]:
    """Collect ``file_urls`` from PAST user messages in a conversation.

    The chat frontend only sends ``file_urls`` for the CURRENT turn, so a
    follow-up request ("tell me more about that file") would otherwise
    lose access to files uploaded earlier. Those urls are persisted on the
    user message by the stream router — this helper walks ``conv.messages``
    newest-first and gathers them for re-injection.

    Rules (mirror the stream router's anti-path-traversal wall):
    - Only ``role == 'user'`` messages are scanned.
    - Only urls starting with ``/api/uploads/`` are accepted (arbitrary
      absolute paths are dropped).
    - The current turn's urls (``exclude``) are skipped.
    - Order preserved, duplicates removed, scan bounded by
      ``_HISTORICAL_MAX_USER_MESSAGES`` / ``_HISTORICAL_MAX_URLS``.

    Non-fatal: any malformed message is skipped. Returns ``[]`` when
    there is nothing to re-read.
    """
    if not conv_messages:
        return []
    exclude_set = {u for u in (exclude or []) if isinstance(u, str)}
    seen: set[str] = set(exclude_set)
    out: list[str] = []
    user_seen = 0
    for m in reversed(conv_messages):
        if not isinstance(m, dict):
            continue
        if (m.get("role") or "").lower() != "user":
            continue
        user_seen += 1
        if user_seen > _HISTORICAL_MAX_USER_MESSAGES:
            break
        urls = m.get("file_urls") or []
        if isinstance(urls, str):
            urls = [urls]
        if not isinstance(urls, list):
            continue
        for u in urls:
            if not isinstance(u, str) or not u.startswith("/api/uploads/"):
                continue
            if u in seen:
                continue
            seen.add(u)
            out.append(u)
            if len(out) >= _HISTORICAL_MAX_URLS:
                return out
    return out


def assemble_context(
    db: Session,
    conversation_id: Optional[str],
    agent_name: str,
    user_message: str,
    task_spec: dict,
    attachments: Optional[list] = None,
) -> dict:
    """Assemble the ContextManifest for an execution.

    Args:
        attachments: optional list of ``file_url`` strings the user uploaded
            with this message. Each is resolved to a local path and its
            text extracted via ``document_ingestion.service.prepare_for_context``.
            Image files with no OCR text are flagged as multimodal inputs
            (the caller forwards them as image content blocks separately).

    Returns a dict with:
        items: list of context items (each with type, content, source)
        token_estimate: estimated token count
        agent_config: agent configuration snapshot
        available_tools: list of available tool names
        available_skills: list of available skill names
        user_attachments: list of prepared attachment dicts (for the
            response generator to cite file names / forward images)
    """
    items = []
    token_estimate = 0
    prepared_attachments: list[dict] = []

    # Chat-upload RAG (2026-08-31, FSM parity with the v3 stream): large
    # uploads are indexed into the ChromaDB session collection and answered
    # via top-k retrieval instead of the truncated text dump. Fail-open.
    _rag_on = False
    _upload_rag_mod = None
    _rag_inline_max = 6000
    try:
        from app.config import settings as _rag_settings

        _rag_inline_max = _rag_settings.RAG_UPLOADS_INLINE_MAX_CHARS
        if _rag_settings.RAG_UPLOADS_ENABLED:
            from app.services.document_ingestion import upload_rag as _upload_rag_mod

            _rag_on = _upload_rag_mod.availability()
    except Exception:
        _rag_on = False

    # 1. Agent configuration
    agent_config = _get_agent_config(db, agent_name)
    if agent_config:
        items.append({
            "type": "agent_config",
            "content": agent_config,
            "source": "agent_app",
            "priority": "high",
        })
        token_estimate += _estimate_tokens(str(agent_config))

    # 1b. User attachments — extracted text from files the user uploaded
    # this turn. Phase 1: each attachment is resolved to a local path and
    # its text extracted via prepare_for_context. Image files are flagged
    # as multimodal inputs (the chat loop forwards the bytes as image
    # content blocks separately). Non-fatal: extraction failures are
    # surfaced as "[could not read X]" markers, never break the turn.
    if attachments:
        attach_parts: list[str] = []
        for furl in attachments:
            if not isinstance(furl, str) or not furl:
                continue
            try:
                from app.services.document_ingestion.service import prepare_for_context
                prep = prepare_for_context(furl)
                prepared_attachments.append(prep)
                fname = prep.get("file_name") or furl
                if prep.get("is_image"):
                    if prep.get("text"):
                        attach_parts.append(
                            f"--- {fname} (image, OCR) ---\n{prep['text']}"
                        )
                    else:
                        attach_parts.append(
                            f"--- {fname} (image, no OCR text — see multimodal block) ---"
                        )
                elif prep.get("text"):
                    if _rag_on and len(prep["text"]) > _rag_inline_max:
                        attach_parts.append(
                            f"--- {fname} (large file — relevant passages retrieved below) ---"
                        )
                    else:
                        attach_parts.append(f"--- {fname} ---\n{prep['text']}")
                elif prep.get("error"):
                    attach_parts.append(
                        f"--- {fname} [could not read: {prep['error']}] ---"
                    )
            except Exception as e:
                logger.warning(
                    "assemble_context: attachment prep failed for %s: %s",
                    furl, e,
                )
                attach_parts.append(f"--- {furl} [extraction failed] ---")
        if attach_parts:
            attach_block = (
                "The user uploaded the file(s) below. Answer the user's "
                "question using their content. Quote the file name when "
                "citing a passage.\n\n"
                + "\n\n".join(attach_parts)
            )
            items.append({
                "type": "user_attachments",
                "content": attach_block,
                "source": "user_upload",
                "priority": "high",
            })
            token_estimate += _estimate_tokens(attach_block)

    # 1c. Historical attachments — files the user uploaded in EARLIER
    # turns of this conversation. The frontend only sends file_urls for
    # the CURRENT turn, so a follow-up request ("tell me more about that
    # file") would otherwise find the file gone and the agent would
    # apologise it "can't re-read" the upload — the Kimi/GPT parity gap.
    # The stream router persists file_urls on each user message; re-scan
    # conv.messages and re-extract the text so uploads stay readable for
    # the whole conversation. Deduplicated against this turn's
    # attachments. Bounded by MAX_HISTORICAL_EXTRACT_CHARS so a long
    # file-heavy conversation can't blow the context window.
    historical_urls = collect_historical_file_urls(
        _get_all_messages(db, conversation_id),
        exclude=attachments,
    )
    if historical_urls:
        hist_parts: list[str] = []
        _hist_budget = MAX_HISTORICAL_EXTRACT_CHARS
        for furl in historical_urls:
            try:
                from app.services.document_ingestion.service import prepare_for_context
                prep = prepare_for_context(furl)
                prepared_attachments.append(prep)
                fname = prep.get("file_name") or furl
                if prep.get("is_image"):
                    if prep.get("text"):
                        hist_parts.append(
                            f"--- {fname} (image, OCR, uploaded earlier) ---\n{prep['text']}"
                        )
                    else:
                        hist_parts.append(
                            f"--- {fname} (image uploaded earlier; no OCR text) ---"
                        )
                elif prep.get("text"):
                    if _rag_on and len(prep["text"]) > _rag_inline_max:
                        hist_parts.append(
                            f"--- {fname} (uploaded earlier, large file — "
                            "relevant passages retrieved below) ---"
                        )
                    else:
                        hist_parts.append(f"--- {fname} (uploaded earlier) ---\n{prep['text']}")
                elif prep.get("error"):
                    hist_parts.append(
                        f"--- {fname} [could not read: {prep['error']}] ---"
                    )
                _hist_budget -= len(prep.get("text") or "")
                if _hist_budget <= 0:
                    hist_parts.append(
                        "(further earlier files omitted — the conversation "
                        "already has a lot of file content; ask to focus on "
                        "a specific file if needed)"
                    )
                    break
            except Exception as e:
                logger.warning(
                    "assemble_context: historical attachment prep failed for %s: %s",
                    furl, e,
                )
                hist_parts.append(f"--- {furl} [extraction failed] ---")
        if hist_parts:
            hist_block = (
                "The user uploaded the file(s) below in EARLIER messages of this "
                "conversation. They remain available for follow-up questions. "
                "Quote the file name when citing a passage.\n\n"
                + "\n\n".join(hist_parts)
            )
            items.append({
                "type": "user_attachments",
                "content": hist_block,
                "source": "user_upload",
                "priority": "high",
            })
            token_estimate += _estimate_tokens(hist_block)

    # 1d. Chat-upload RAG retrieval — when any attachment was too large to
    # inline, index it (once, idempotent) and retrieve the top-k chunks
    # most relevant to THIS turn's question. Mirrors the v3 stream path.
    if _rag_on and (attachments or historical_urls):
        try:
            _rag_session = conversation_id or "default"
            _rag_org = "default"
            _rag_agent = agent_name or ""
            _rag_pid = ""
            try:
                from app.models.agent_conversation import AgentConversation

                _conv_row = (
                    db.query(AgentConversation)
                    .filter(AgentConversation.id == conversation_id)
                    .first()
                )
                if _conv_row and getattr(_conv_row, "project_id", None):
                    _rag_pid = _conv_row.project_id or ""
            except Exception:
                pass
            _big = False
            for _furl in dict.fromkeys(
                [*(attachments or []), *historical_urls]
            ):
                if not isinstance(_furl, str) or not _furl:
                    continue
                try:
                    from app.services.document_ingestion.service import (
                        prepare_for_context,
                    )

                    _p = prepare_for_context(_furl)
                    _t = _p.get("text") or ""
                    if len(_t) > _rag_inline_max:
                        if _p.get("truncated"):
                            _fp = prepare_for_context(_furl, max_chars=5_000_000)
                            _t = _fp.get("text") or _t
                        _n = _upload_rag_mod.index_upload_text(
                            _furl, _rag_session, _rag_org, _t,
                            file_name=_p.get("file_name") or _furl,
                            agent=_rag_agent,
                            project_id=_rag_pid,
                        )
                        _big = _big or bool(_n and _n > 0)
                except Exception as _rag_idx_err:
                    logger.debug(
                        "assemble_context: upload index failed (non-fatal): %s",
                        _rag_idx_err,
                    )
            if _big:
                _chunks = _upload_rag_mod.retrieve_upload_chunks(
                    _rag_session, _rag_org, user_message,
                    agent=_rag_agent,
                    project_id=_rag_pid,
                )
                _blk = _upload_rag_mod.build_retrieval_block(
                    user_message, _chunks
                )
                if _blk:
                    items.append({
                        "type": "user_attachments",
                        "content": _blk,
                        "source": "upload_rag",
                        "priority": "high",
                    })
                    token_estimate += _estimate_tokens(_blk)
        except Exception as _rag_err:
            logger.debug(
                "assemble_context: upload retrieval failed (non-fatal): %s",
                _rag_err,
            )

    # 2. Conversation history
    history = _get_conversation_history(db, conversation_id)
    if history:
        items.append({
            "type": "conversation_history",
            "content": history,
            "source": "chat_messages",
            "priority": "high",
        })
        token_estimate += _estimate_tokens(str(history))

    # 2b. Conversation context (compact transcript + recent artifacts +
    #     prior entities) — the follow-up-aware block consumed by the
    #     planner and response generator so refinement turns ("make it
    #     better", "dark theme") resolve against prior turns instead of
    #     being treated as brand-new, context-free requests.
    conv_ctx = build_conversation_context(db, conversation_id, agent_name)
    if conv_ctx:
        items.append({
            "type": "conversation_context",
            "content": conv_ctx,
            "source": "context_assembler",
            "priority": "high",
        })
        token_estimate += _estimate_tokens(str(conv_ctx))

    # 3. Agent memory
    memories = _get_agent_memories(db, agent_name, user_message)
    if memories:
        items.append({
            "type": "memory",
            "content": memories,
            "source": "agent_memory",
            "priority": "medium",
        })
        token_estimate += _estimate_tokens(str(memories))

    # 4. Available tools and skills
    available_tools = _get_available_tools(db, agent_name)
    available_skills = _get_available_skills(db, agent_name)

    items.append({
        "type": "available_tools",
        "content": available_tools,
        "source": "tool_registry",
        "priority": "medium",
    })
    token_estimate += _estimate_tokens(str(available_tools))

    # 5. Task spec
    items.append({
        "type": "task_spec",
        "content": task_spec,
        "source": "task_spec_parser",
        "priority": "high",
    })

    # Enforce token budget
    if token_estimate > MAX_CONTEXT_TOKENS:
        logger.warning("Context exceeds token budget: %d > %d — truncating", token_estimate, MAX_CONTEXT_TOKENS)
        # Drop low-priority items first
        items = [i for i in items if i["priority"] == "high"]
        token_estimate = sum(_estimate_tokens(str(i["content"])) for i in items)

    return {
        "items": items,
        "token_estimate": token_estimate,
        "agent_config": agent_config,
        "available_tools": available_tools,
        "available_skills": available_skills,
        "user_attachments": prepared_attachments,
    }


def _get_agent_config(db: Session, agent_name: str) -> Optional[dict]:
    """Get agent configuration from the database."""
    try:
        from app.models.agent_app import AgentApp
        agent = db.query(AgentApp).filter(AgentApp.name == agent_name, AgentApp.is_deleted == False).first()
        if agent:
            return {
                "name": agent.name,
                "description": agent.description,
                "model": agent.model,
                "agent_type": agent.agent_type,
                "skills": agent.skills,
                "topology": agent.topology,
            }
    except Exception as e:
        logger.debug("Failed to get agent config: %s", e)
    return None


def _get_conversation_history(db: Session, conversation_id: Optional[str]) -> list:
    """Get recent conversation messages."""
    if not conversation_id:
        return []
    try:
        from app.models.agent_conversation import AgentConversation
        conv = db.query(AgentConversation).filter(AgentConversation.id == conversation_id).first()
        if conv and conv.messages:
            # Return last 10 messages
            return conv.messages[-10:]
    except Exception as e:
        logger.debug("Failed to get conversation history: %s", e)
    return []


def _get_all_messages(db: Session, conversation_id: Optional[str]) -> list:
    """Get the FULL conversation message list (for historical re-reads).

    Unlike ``_get_conversation_history`` (last 10 only), this returns every
    message so ``collect_historical_file_urls`` can find files uploaded
    many turns back. The collector bounds the scan, so the full list is
    cheap to load. Non-fatal: returns ``[]`` on any error.
    """
    if not conversation_id:
        return []
    try:
        from app.models.agent_conversation import AgentConversation
        conv = db.query(AgentConversation).filter(AgentConversation.id == conversation_id).first()
        if conv and conv.messages:
            return list(conv.messages)
    except Exception as e:
        logger.debug("Failed to get full conversation messages: %s", e)
    return []


# Per-message content cap for the transcript (keeps the block bounded).
_TRANSCRIPT_MSG_CHAR_CAP = 600
_TRANSCRIPT_MAX_MESSAGES = 10


def build_conversation_context(
    db: Session,
    conversation_id: Optional[str],
    agent_name: str,
) -> dict:
    """Build a compact, follow-up-aware context block for the FSM.

    Returns a dict with:
        transcript: str — recent turns rendered as ``"User: …\\nAssistant: …"``,
            capped per-message and in total so it never crowds out the plan
            prompt.
        recent_artifacts: list[dict] — up to 5 most recent artifacts for the
            conversation, each ``{id, title, artifact_type, created_date}``.
        prior_entities: dict — entities from the most recent *completed*
            turn's TaskSpec (date_range, metric, …), carried forward so
            refinement requests inherit context.

    The entire function is **non-fatal**: any error returns ``{}`` so the
    FSM degrades to its previous (context-blind) behavior rather than
    failing the turn.
    """
    if not conversation_id:
        return {}
    try:
        transcript = _build_transcript(db, conversation_id)
        recent_artifacts = _get_recent_artifacts(db, conversation_id)
        prior_entities = _get_prior_entities(db, conversation_id)
        prior_datasets = _get_prior_datasets(db, conversation_id)

        dashboard_id = _get_conversation_dashboard_id(db, conversation_id)

        if not transcript and not recent_artifacts and not prior_entities and not dashboard_id and not prior_datasets:
            return {}
        result: dict = {
            "transcript": transcript,
            "recent_artifacts": recent_artifacts,
            "prior_entities": prior_entities,
        }
        if prior_datasets:
            result["prior_datasets"] = prior_datasets
        if dashboard_id:
            result["dashboard_id"] = dashboard_id
        # Phase 4: carry forward artifact type of the most recent artifact
        if recent_artifacts:
            result["previous_artifact_type"] = recent_artifacts[0].get("artifact_type", "")
        return result
    except Exception as e:
        logger.debug("build_conversation_context failed (non-fatal): %s", e)
        return {}


def _build_transcript(db: Session, conversation_id: str) -> str:
    """Render recent conv.messages as a compact plain-text transcript."""
    try:
        from app.models.agent_conversation import AgentConversation
        conv = db.query(AgentConversation).filter(
            AgentConversation.id == conversation_id,
        ).first()
        if not conv or not conv.messages:
            return ""
        msgs = conv.messages[-_TRANSCRIPT_MAX_MESSAGES:]
        lines = []
        for m in msgs:
            role = (m.get("role") or "?").capitalize()
            content = str(m.get("content") or "").strip()
            if not content:
                # Assistant messages may carry tool_calls/artifacts but no
                # text — surface a compact summary so the transcript shows
                # what was produced.
                tcs = m.get("tool_calls") or []
                aids = m.get("artifact_ids") or []
                if tcs:
                    names = ", ".join(
                        str(t.get("name") or "?") for t in tcs[:4]
                    )
                    content = f"[produced via: {names}]"
                elif aids:
                    content = f"[produced artifact(s): {', '.join(str(a) for a in aids[:3])}]"
                else:
                    continue
            if len(content) > _TRANSCRIPT_MSG_CHAR_CAP:
                content = content[:_TRANSCRIPT_MSG_CHAR_CAP] + "…"
            lines.append(f"{role}: {content}")
        return "\n".join(lines)
    except Exception as e:
        logger.debug("_build_transcript failed (non-fatal): %s", e)
        return ""


def _get_conversation_dashboard_id(db: Session, conversation_id: str) -> str | None:
    """Return the live dashboard bound to this conversation, if any."""
    try:
        from app.models.agent_conversation import AgentConversation

        row = db.query(AgentConversation.dashboard_id).filter(
            AgentConversation.id == conversation_id,
            AgentConversation.is_deleted == False,  # noqa: E712
        ).first()
        return row[0] if row and row[0] else None
    except Exception as e:
        logger.debug("_get_conversation_dashboard_id failed (non-fatal): %s", e)
        return None


def _get_recent_artifacts(db: Session, conversation_id: str) -> list:
    """Return up to 5 most recent artifacts for the conversation."""
    try:
        from app.models.artifact import Artifact
        rows = db.query(Artifact).filter(
            Artifact.conversation_id == conversation_id,
            Artifact.is_deleted == False,
        ).order_by(Artifact.created_date.desc()).limit(5).all()
        return [
            {
                "id": str(a.id),
                "title": a.title or "",
                "artifact_type": a.artifact_type or "",
                "created_date": a.created_date.isoformat() if a.created_date else None,
            }
            for a in rows
        ]
    except Exception as e:
        logger.debug("_get_recent_artifacts failed (non-fatal): %s", e)
        return []


def _get_prior_entities(db: Session, conversation_id: str) -> dict:
    """Extract entities from the most recent *completed* turn's TaskSpec.

    Filters by ``current_state IN ('done', 'fail')`` so the in-flight
    execution (still in GOAL/CONTEXT/PLAN) is naturally excluded.
    """
    try:
        from app.models.execution import Execution
        prev = db.query(Execution).filter(
            Execution.conversation_id == conversation_id,
            Execution.current_state.in_(["done", "fail"]),
            Execution.task_spec.isnot(None),
        ).order_by(Execution.created_date.desc()).first()
        if prev and prev.task_spec:
            return prev.task_spec.get("entities", {}) or {}
    except Exception as e:
        logger.debug("_get_prior_entities failed (non-fatal): %s", e)
    return {}


def _get_prior_datasets(db: Session, conversation_id: str) -> list[dict] | None:
    """Extract answer datasets from the most recent *completed* turn.

    Returns a compact list of ``{rows, sql, source_id, source_name}`` dicts
    from prior observations tagged with ``purpose='answer'``. This allows
    follow-up requests ("a summary", "break it down") to synthesize from
    existing data without re-querying (~5s instead of ~35s).

    Only returns datasets when they contain non-trivial data (not effectively
    empty per ``is_effective_empty``). Returns None when no usable prior
    data exists.
    """
    try:
        from app.models.execution import Execution
        from app.services.goal_contract import is_effective_empty

        prev = db.query(Execution).filter(
            Execution.conversation_id == conversation_id,
            Execution.current_state.in_(["done", "fail"]),
        ).order_by(Execution.created_date.desc()).first()
        if not prev or not prev.observations:
            return None

        datasets = []
        for obs in prev.observations:
            if not obs.success:
                continue
            rd = obs.result_data
            if not isinstance(rd, dict):
                continue
            rows = rd.get("rows")
            if not isinstance(rows, list) or len(rows) == 0:
                continue
            if is_effective_empty(rows):
                continue
            datasets.append({
                "rows": rows[:200],  # cap to avoid bloat
                "sql": rd.get("sql"),
                "source_id": rd.get("source_id"),
                "source_name": rd.get("source_name"),
            })

        return datasets if datasets else None
    except Exception as e:
        logger.debug("_get_prior_datasets failed (non-fatal): %s", e)
        return None


def _get_agent_memories(db: Session, agent_name: str, user_message: str) -> list:
    """Get query-relevant agent memories, scoped to the agent.

    Previously this fetched the global top-5 memories by importance —
    ignoring both the user's message and the agent scope, so every agent
    saw every other agent's notes. Now it resolves the agent's app id
    (matching the convention in routers/agents.py: ``agent_app.id if
    agent_app else agent_name``) and delegates to memory_advanced's
    semantic/lexical search with the user message as the query.
    """
    try:
        from app.models.agent_app import AgentApp
        from app.services.memory_advanced import search_memories

        agent_app = db.query(AgentApp).filter(
            AgentApp.name == agent_name,
            AgentApp.is_deleted == False,
        ).first()
        agent_app_id = agent_app.id if agent_app else agent_name

        results = search_memories(
            db,
            agent_app_id=agent_app_id,
            query=user_message,
            limit=5,
            min_score=0.05,  # lenient — prefer recall; the FSM budget enforces size
        )
        return [
            {
                "content": r.memory.content,
                "importance": getattr(r.memory, "importance", 0),
                "score": round(r.score, 4),
            }
            for r in results
        ]
    except Exception as e:
        logger.debug("Failed to get memories: %s", e)
    return []


def _get_available_tools(db: Session, agent_name: str) -> list:
    """Get available tool names for the agent."""
    try:
        from app.services.agent_tools import tool_registry
        return list(tool_registry.keys()) if hasattr(tool_registry, 'keys') else []
    except Exception:
        return []


def _get_available_skills(db: Session, agent_name: str) -> list:
    """Get available skill names."""
    try:
        from app.services.skills_loader import get_skills_registry
        registry = get_skills_registry()
        return list(registry.list_names()) if hasattr(registry, 'list_names') else []
    except Exception:
        return []


def _estimate_tokens(text: str) -> int:
    """Rough token estimate (4 chars ≈ 1 token)."""
    return len(text) // 4


def format_followup_context_block(conv_ctx: Optional[dict]) -> str:
    """Render a conversation-context block for the legacy ReAct system prompt.

    This is the defense-in-depth counterpart to the FSM's
    ``_build_response_prompt`` transcript injection: when a follow-up turn
    still reaches the legacy tool loop (e.g. FSM disabled, or the override
    didn't fire), this block gives the LLM the prior transcript, the
    refinable artifact ids, and an explicit "this is a follow-up — resolve
    'it' against the prior turn, don't invent a new topic" directive.

    Returns ``""`` when there is nothing useful (no transcript and no
    artifacts), so callers can unconditionally append it without producing
    empty/noisy prompts. Non-fatal: any error returns ``""``.
    """
    try:
        if not conv_ctx:
            return ""
        transcript = (conv_ctx.get("transcript") or "").strip()
        artifacts = conv_ctx.get("recent_artifacts") or []
        if not transcript and not artifacts:
            return ""

        parts = ["\n\n=== Conversation so far ==="]
        if transcript:
            parts.append(transcript)
        else:
            parts.append("(no prior transcript available)")

        if artifacts:
            parts.append("\n=== Recent artifacts (refinable) ===")
            for a in artifacts[:5]:
                aid = a.get("id") or "?"
                title = a.get("title") or "(untitled)"
                atype = a.get("artifact_type") or ""
                parts.append(f"- {aid}: {title}" + (f" ({atype})" if atype else ""))

        parts.append(
            "\nThis may be a follow-up refinement of a prior turn. Resolve "
            "references like \"it\", \"this\", \"that\" against the most "
            "recent artifact/result above and act on the refinement (e.g. "
            "\"make it dark theme\" → restyle the most recent artifact). Do "
            "NOT treat a refinement as a brand-new topic, and do NOT ask "
            "the user to restate information already present in the "
            "conversation. If the user granted open latitude, proceed with "
            "sensible defaults instead of asking."
        )
        return "\n".join(parts)
    except Exception as e:
        logger.debug("format_followup_context_block failed (non-fatal): %s", e)
        return ""
