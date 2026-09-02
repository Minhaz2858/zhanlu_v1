# Agent Runtime Gap Fix — Vision Model Routing + Voice-Out (P0 Pair)

> **Date:** 2026-09-02
> **Status:** Draft (pending user review — NO code changed yet)
> **Source:** platform-gap-audit (2026-08-31) + re-verification against live code today
> **Scope:** L1 vision-model routing + P0-2 voice-out. L2 (notification channels) and L4 (agent-builder eval gate) are separate specs.
> **Design rule:** flag-OFF-by-default; nothing changes for existing flows when OFF. Backend tests run only their own new test file; frontend only the colocated suite (RAM-limited box).

## 1. Problem

Two runtime-level gaps that every agent (existing Synexia agents AND every
future Harness agent created in Agent Studio) inherits:

1. **Vision is broken with the default model.** `vision_tool.py:71-98` posts
   `model=settings.LLM_MODEL` (= `deepseek-chat`, text-only) to
   `{settings.OPENAI_BASE_URL}/chat/completions` with an `image_url` content
   block. The model cannot see images, so image description/OCR returns
   garbage or empty. Chat image attachments hit the same wall:
   `routers/agents.py` Phase-2 multimodal pass-through (~line 12392) forwards
   `image_url` blocks into the same text-only model. There is no
   `VISION_MODEL`/`VISION_BASE_URL` anywhere in `config.py`. Container reality:
   no tesseract, no whisper (verified `docker exec`).
   - Downstream blocker: the computer-use "Kimi-style" screenshot->act loop
     (roadmap item 5 Phase 2) cannot work without a vision-capable model.

2. **Voice-out is dead code.** `frontend/src/hooks/useTTS.js` — a complete,
   correct browser `SpeechSynthesis` wrapper (`speak/pause/resume/cancel`,
   zh-CN mapping) — has **zero importers** (verified grep). Mic-in IS wired
   (`ChatInput.jsx:440` → `VoiceInput`). `tts_tool.py` is registered but has
   no usable provider (DeepSeek base URL exposes no `/audio/speech`).

## 2. Design

### 2a. Vision model routing (backend)

Add three settings to `backend/app/config.py` (near the LLM section, ~line 195):

```python
VISION_MODEL: str = ""            # empty = fall back to LLM_MODEL (current behavior)
VISION_BASE_URL: str = ""         # empty = settings.OPENAI_BASE_URL
VISION_API_KEY: str = ""          # empty = settings.OPENAI_API_KEY
```

Semantics: all three empty ⇒ bit-for-bit current behavior (flag-OFF).
`.env.example` documents the local-vision option:
`VISION_MODEL=qwen-vl-max` (cloud) or a local qwen-vl vLLM endpoint per the
customer local-LLM rule; `VISION_BASE_URL` points at it; key may be a dummy
for local vLLM.

Change `backend/app/services/tool_handlers/vision_tool.py` (`_vision`, ~line 60-100)
to resolve:

```python
base = (settings.VISION_BASE_URL or settings.OPENAI_BASE_URL).rstrip("/")
model = settings.VISION_MODEL or settings.LLM_MODEL
key = settings.VISION_API_KEY or settings.OPENAI_API_KEY
```

and use those in the URL/headers/payload. Response `model` field reflects the
resolved model. No other call sites change.

Graceful degradation when the resolved model is text-only is a **documented
limitation, not a code fix here**: model capability metadata (vision-capable
flag) belongs to the L1 catalog work (config already seeds LLM catalogs,
`config.py:374`); this spec keeps the change minimal. The vision tool result
already returns `success: False` with the provider error when the model
rejects image input — acceptable signal.

Chat image attachments (`agents.py` multimodal pass-through) are **out of
scope for the backend change** — routing whole chat turns to a different
provider/model is an agent-policy decision (per-agent model config), tracked
with the L1 catalog work. This spec only fixes the explicit `vision` tool.

### 2b. Voice-out (frontend only — zero backend, zero keys)

- `frontend/src/components/chat/MessageBubble.jsx`: for assistant text
  messages, render a small speaker button (lucide `Volume2`/`VolumeX` toggle)
  that calls `useTTS().speak(text, lang)` with the UI language (zh → zh-CN,
  else en-US). Pause/resume/cancel on second click; auto-cancel on new
  message send (or component unmount). Render nothing if
  `!window.speechSynthesis` (privacy/unsupported) or when the message has no
  plain text.
- `frontend/src/hooks/useTTS.js` is already complete — reuse, do not rewrite.
- Existing `VoiceInput` (mic-in) untouched.
- No new props plumbed through Chat.jsx: the button lives inside
  MessageBubble and reads language from the existing locale context the
  bubble already has.

### 2c. Config & docs

- `backend/.env.example` (and root `.env.example` mirror if it lists these):
  `VISION_MODEL=`, `VISION_BASE_URL=`, `VISION_API_KEY=` with the local
  qwen-vl comment.
- README "Known follow-ups" is not required (this closes the vision gap for
  the tool surface; chat-turn vision routing stays a follow-up noted in §5).

## 3. Tests

- `backend/tests/test_vision_tool_model_override.py` (new, one file):
  monkeypatch `settings` (VISION_MODEL/BASE/KEY empty → asserts payload model
  == LLM_MODEL and URL == OPENAI_BASE_URL; set → asserts override values; key
  fallback chain). Mock `httpx.AsyncClient.post` — no network.
- `frontend/src/components/chat/MessageBubble.tts.test.jsx` (new, colocated):
  mock `speechSynthesis`; speaker button appears on assistant text, calls
  speak with zh-CN for zh locale, toggles to cancel, and renders nothing when
  `speechSynthesis` is undefined.
- Run only those two files (RAM-tight rule).

## 4. Rollout

Per project phase-gate sequence — no skipping:
shadow dispatch (flag ON in dev, monitor vision tool + no regressions with
flags OFF via the override test) → golden eval (vision describe/ocr on a fixed
image set; TTS manual golden list) → limited canary (one internal agent) →
controlled automation → rollback rehearsal (flags OFF revert is the rehearsal)
→ freeze. VISION_* empty + no UI button = complete rollback, zero code paths
changed.

Deploys: restart BOTH `zhanlu-backend` and `zhanlu-sandbox-worker` after the
backend change; frontend is a `npm run build` only (dist :ro bind-mount).

## 5. Out of scope (next specs)

- L1 full model-routing layer: vision-capable model catalog metadata,
  per-agent model policy, chat-turn vision routing, OCR ingestion (local
  tesseract).
- Computer-use vision loop enablement (depends on this spec's model fix).
- L2 notification channels (WeCom/Feishu/Slack/Teams) + completion pings.
- L4 agent-builder eval-gate (shadow→golden eval before agent publish).
- SAML, A2A `a2a_delegate`, realtime voice WS — tracked in the existing
  roadmap doc.

## 6. Verification checklist

- [ ] `VISION_MODEL` empty ⇒ vision tool payload identical to today (byte-for-byte).
- [ ] `VISION_MODEL=qwen-vl-*` + `VISION_BASE_URL` ⇒ payload model/base/key override.
- [ ] Vision tool returns `model: <resolved>`.
- [ ] Speaker button: appears on assistant text only; speaks zh-CN/en-US; toggles cancel; hidden without speechSynthesis.
- [ ] `vite build` + eslint clean; the two new test files pass; existing suites untouched and green (targeted runs only).
