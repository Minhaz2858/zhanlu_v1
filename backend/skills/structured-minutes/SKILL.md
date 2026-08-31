---
name: structured-minutes
description: Use when the user provides a transcript, notes, or chat log and asks to create meeting minutes, summarize a meeting, extract action items, recap a discussion, or clean up a transcript. Automatically extracts agenda topics, discussion highlights, decisions, and action items with owners and deadlines.
---

# Structured Minutes

Transform raw meeting materials (transcripts, notes, chat logs) into structured minutes by automatically extracting agenda topics, discussion highlights, decisions, and action items with owners and deadlines.

## When to use

- User provides a transcript / notes / chat log and asks for minutes
- "Summarize this meeting", "extract action items", "recap the discussion"
- "Clean up this transcript", "make minutes from this call"
- Any meeting follow-up documentation request

## Workflow

1. **Read the material** — full transcript, notes, or chat log. If it's an audio/video file, transcribe first (or use the transcript if provided).
2. **Segment by agenda topic** — identify distinct discussion threads; give each a title. When topics aren't explicit, infer them from content shifts.
3. **Extract per topic**:
   - Discussion highlights (2-5 bullets: what was said, positions, data cited)
   - Decisions (explicit: "we agreed to...", "decided that...")
   - Open questions (explicitly deferred or unresolved)
4. **Extract action items** — for each: owner (who), action (what), deadline (when, if stated), follow-up thread. If an owner is ambiguous ("we should..."), flag it as unassigned rather than guessing.
5. **Compose minutes** in the standard structure (below).
6. **Note confidence** — if the material is a partial transcript or chat log (not a full meeting), say so in the header; don't fabricate discussion that wasn't in the material.

## Minutes structure

1. **Header** — meeting title, date, attendees (as known), duration (if known), material type (full transcript / notes / chat log)
2. **Summary** — 3-5 bullets: what was accomplished, key decisions, next steps
3. **Agenda & discussion** — per topic: highlights, decisions, open questions
4. **Action items table** — Action | Owner | Deadline | Status
5. **Decisions log** — Decision | Context | Date
6. **Open questions** — unresolved items with who owns the follow-up

## Pitfalls

- Don't invent owners or deadlines — extract only what's stated; flag "unassigned" / "no deadline stated"
- Distinguish decisions from discussion — only explicit agreements become decisions
- Keep the speaker's intent: if someone dissented, record the dissent with the decision
- Chat logs: preserve the conversational thread; don't reorder by timestamp when the thread is clearer grouped by topic
