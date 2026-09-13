# Computer Use (Managed Browser) — Design

> **Date:** 2026-09-02
> **Status:** Approved (user: "follow your recommendation" — Option 1, primitive facade)
> **Scope:** `computer_use` tool finished as a primitive facade over the existing `agent-browser`/CDP path. No OS-level desktop capture.

## Decisions

- **Primitive facade (not an autonomous loop).** The Synexia FSM agent loop drives `computer_use` one action per call, like Claude's computer-use tool and modern browser MCP tools. A Kimi-style `run` loop (screenshot → vision → act) is a later flag-gated follow-up.
- **A11y-tree-first perception.** `get_state` returns the accessibility snapshot (compact `@eN` refs); `screenshot` returns a PNG file path. No pixel-coordinate clicks, no `eval` — never exposes the host shell.
- **Shared session manager.** One browser session per conversation, shared by `agent_browser` and `computer_use` (one Chromium per chat). TTL + per-conversation action cap.
- **Allowlist.** `COMPUTER_USE_ALLOWED_URLS` env JSON (empty = SSRF guard only). Per-user/workspace URL permissions (DB) deferred to Phase 2.

## Tool surface (`computer_use`)

| action      | CLI mapping                          | notes                                  |
|-------------|--------------------------------------|----------------------------------------|
| `open`      | `agent-browser open <url> --session` | SSRF + allowlist checked               |
| `screenshot`| `agent-browser screenshot --out <png>`| returns `file_path`, `file_size`      |
| `get_state` | `agent-browser snapshot`             | a11y tree JSON (`@eN` refs)            |
| `click`     | `agent-browser click --session <ref>`| `element` = `@eN` or CSS selector      |
| `type`      | `agent-browser type --session <ref> <text>` |                              |
| `scroll`    | `agent-browser scroll --session`     |                                        |
| `close`     | cleanup session + `agent-browser close` | resets step budget                   |

## Config (all OFF / empty by default)

- `COMPUTER_USE_ENABLED=false` — off ⇒ legacy "not implemented" stub response (behavior parity).
- `COMPUTER_USE_MAX_STEPS=20` — per-conversation session action cap; `close` resets it.
- `COMPUTER_USE_VIEWPORT="1280,800"` — reserved (agent-browser CLI has no `--viewport`; future engine launch config).
- `COMPUTER_USE_SESSION_TTL_S=1800` — idle session expiry.
- `COMPUTER_USE_ALLOWED_URLS=[]` — URL allowlist (host or URL-prefix patterns).

## Security

- `is_safe_url()` (SSRF, fails closed) on every `open` — unchanged.
- New `tool_security.is_url_in_allowlist(url, allowlist)`: empty list ⇒ allow all (SSRF still applies); host match incl. subdomains, or URL-prefix match when the pattern has a scheme.
- No arbitrary shell: only fixed CLI verbs + validated URL/ref/text args; `eval` intentionally excluded.

## Files

- **New:** `backend/app/services/computer_use/__init__.py`, `session.py` (session store, TTL, step cap, `BROWSER_DIR`), `manager.py` (`run_cli`, `is_available`, action dispatch), `backend/tests/test_computer_use_agent_browser.py` (mock CLI).
- **Modified:** `backend/app/services/tool_handlers/computer_use_tool.py` (real handler + new schema), `backend/app/services/tool_handlers/agent_browser_tool.py` (delegates session/CLI to shared modules; behavior + existing tests unchanged), `backend/app/services/tool_security.py` (+allowlist helper), `backend/app/config.py` (5 flags).

## Acceptance

`computer_use` takes a screenshot, reads state, clicks a `@eN` element, types text, and closes; unauthorized URLs are blocked; disabled flag returns the legacy stub response; `agent_browser` tests still pass after the shared-session refactor.
