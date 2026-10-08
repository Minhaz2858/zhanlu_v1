"""Group chat — AutoGen/Semantic-Kernel-style shared-thread multi-agent rounds.

IBM framework-audit gap #1 (2026-10-08): the swarm toolbox had task hand-off
(mailbox) and parallel fan-out (FanOutPool), but not the *conversational*
pattern — multiple agents replying in one shared thread, taking turns, each
seeing what the others said, until a completion condition is met.

Design (built on existing machinery — no new protocol):
- The shared thread IS the team mailbox (DB-persisted, readable by every
  member and by the lead via swarm_get_messages).
- Speaker selection: "round_robin" (deterministic) or "llm" (a cheap LLM
  call picks the next most useful speaker from the roster).
- Termination: ``max_rounds`` (hard bound) or any agent emitting the
  ``[COMPLETE]`` marker in its reply.
- Each agent runs through SwarmRuntime (own prompt + tool whitelist) via
  the SwarmCoordinator — same governance as every other swarm path.
"""
from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

COMPLETE_MARKER = "[COMPLETE]"


def _thread_transcript(registry, team_id: str, last_n: int = 12) -> str:
    messages = registry.get_messages(team_id, "moderator", limit=last_n) or []
    lines = []
    for m in messages:
        if isinstance(m, dict):
            sender = m.get("sender", "?")
            content = m.get("content", "")
        else:
            sender = getattr(m, "sender", "?")
            content = getattr(m, "content", "")
        lines.append(f"{sender}: {str(content)[:400]}")
    return "\n".join(lines)


async def _select_speaker_llm(
    roster: list[str], transcript: str, topic: str,
) -> str:
    """LLM speaker-selection: which agent should speak next, and why."""
    from app.services.llm_service import call_llm

    try:
        res = await call_llm(
            prompt=(
                "You are the moderator of a multi-agent group chat working on "
                f"this topic: {topic!r}\n\nRoster: {roster}\n\nTranscript so "
                "far:\n" + (transcript or "(empty)") +
                "\n\nWhich agent should speak NEXT to make the most progress? "
                "Reply with ONLY the agent's name."
            ),
            messages=[],
            temperature=0.0,
            task_type="simple_chat",
        )
        raw = (res.get("response") or res.get("content") or "").strip()
        for name in roster:
            if name in raw:
                return name
    except Exception as e:  # noqa: BLE001 — fall back to round-robin
        logger.debug("group_chat: speaker selection failed: %s", e)
    return roster[0]


class GroupChatResult(dict):
    """Dict alias for typing ergonomics."""


async def run_group_chat(
    coordinator,
    registry,
    team_id: str,
    topic: str,
    agent_names: list[str],
    max_rounds: int = 6,
    speaker_policy: str = "round_robin",
    db=None,
    user_id: str | None = None,
) -> dict[str, Any]:
    """Run a shared-thread group chat; returns the full transcript summary."""
    roster = [a for a in agent_names if a]
    if len(roster) < 2:
        return {"success": False,
                "error": "group chat needs at least 2 agent names"}

    registry.send_message(
        team_id, sender="moderator", recipient="all",
        content=f"GROUP CHAT START — topic: {topic}",
        summary="group chat opened",
    )

    transcript: list[dict[str, Any]] = []
    idx = 0
    completed_by = None
    for round_no in range(1, max_rounds + 1):
        if speaker_policy == "llm" and transcript:
            speaker = await _select_speaker_llm(
                roster, _thread_transcript(registry, team_id), topic)
        else:
            speaker = roster[idx % len(roster)]
            idx += 1

        prior = _thread_transcript(registry, team_id)
        task = (
            f"Group chat on: {topic}\n\nTranscript so far:\n{prior}\n\n"
            "It is your turn. Concretely advance the group's work: add new "
            "information, critique or build on the previous message, or — if "
            "the group's goal is fully achieved — start your reply with "
            f"{COMPLETE_MARKER} and summarize the outcome."
        )
        result = await coordinator.run_task(
            team_id, speaker, task, member_name=speaker,
            db=db, user_id=user_id,
        )
        reply = (result.final_response or result.error or "").strip()
        registry.send_message(
            team_id, sender=speaker, recipient="all",
            content=reply[:4000],
            summary=f"round {round_no}: {reply[:80]}",
        )
        transcript.append({
            "round": round_no, "speaker": speaker, "reply": reply[:2000],
            "success": result.success,
        })
        if COMPLETE_MARKER in reply[:200]:
            completed_by = speaker
            logger.info("group_chat: %s declared completion (round %d)",
                        speaker, round_no)
            break

    return {
        "success": True,
        "team_id": team_id,
        "topic": topic,
        "rounds_used": len(transcript),
        "completed": completed_by is not None,
        "completed_by": completed_by,
        "transcript": transcript,
    }


__all__ = ["run_group_chat", "COMPLETE_MARKER"]
