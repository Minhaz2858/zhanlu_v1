# Phase 1 — Tracing (A1) + RBAC Authorization (B1) + SkillScan (B3)

**Date:** 2026-07-29
**Status:** Approved (design phase)
**Scope:** Three independent subsystems implemented as a single Phase 1 unit, each
in its own package with its own integration point. No cross-dependencies between
the three.

**Source of insights:** Comparative analysis of 5 harness agent projects
(CowAgent, DeerFlow 2.0, Hermes Agent, NeMo Agent Toolkit, OpenHarness) against
the Zhanlu backend. Zhanlu has already adopted the *structural* patterns
(permissions, hooks, compaction, swarm, guardrails); this spec closes the
*depth* gaps in observability, role-based authorization, and skill safety.

---

## Design decisions (locked from brainstorming)

| Decision | Choice | Rationale |
|---|---|---|
| B1 RBAC architecture | **Coexist** with existing `PermissionChecker` | RBAC adds role→resource policies as a first gate; existing tool/mode/path checks stay. Zero regression risk. |
| A1 tracing backend | **OpenTelemetry**, no-op default | Vendor-neutral, lazy SDK. LangSmith/Langfuse pluggable later via the same ABC. |
| B3 scan policy | **Warn-only** (findings logged + attached to metadata, no blocking) | User choice. `SKILL_SCAN_BLOCK_CRITICAL` flag reserved for future blocking. |
| Dependency policy | **Lazy/optional** everywhere | Matches RAM-limited dev box constraint. Defaults work with zero new pip installs. |

---

## Architecture overview

Three independent packages, each integrating at its natural hook point:

```
backend/app/services/
├── tracing/          ← A1: TracingProvider ABC + NoOp + Otel
│   ├── __init__.py   (get_tracer() singleton)
│   ├── base.py       (TracingProvider ABC, Span, TraceContext)
│   ├── noop.py       (NoOpTracer — default, zero overhead)
│   └── otel.py       (OtelTracer — lazy-imports opentelemetry)
│
├── authz/            ← B1: AuthorizationProvider ABC + RBAC
│   ├── __init__.py   (get_authorizer() singleton)
│   ├── base.py       (AuthorizationProvider ABC, AuthorizationDecision, ResourceType)
│   ├── rbac.py       (RbacAuthorizationProvider — deny-always-wins, fail-closed)
│   └── policies.py   (built-in admin/user/viewer role policies)
│
└── skill_scanner/    ← B3: deterministic safety scanner
    ├── __init__.py   (scan_skill() entry point)
    ├── base.py       (Finding, ScanResult, ScanRule dataclasses)
    ├── rules.py      (regex rule definitions)
    └── scanner.py    (SkillScanner — pattern matching)
```

No shared abstraction layer. Each subsystem is testable in isolation.

---

## A1 — Distributed Tracing

### Package: `backend/app/services/tracing/`

#### `base.py`

Defines the `TracingProvider` ABC, the `Span` context manager, and a
`TraceContext` built on `contextvars`.

```python
class TracingProvider(ABC):
    @abstractmethod
    def start_span(self, name: str, attributes: dict | None = None) -> "Span": ...

    @abstractmethod
    def record_llm_call(self, *, model: str, prompt_tokens: int,
                        completion_tokens: int, latency_ms: float,
                        error: str | None = None) -> None: ...

    @abstractmethod
    def record_tool_call(self, *, tool_name: str, args_hash: str,
                         duration_ms: float, success: bool,
                         error: str | None = None) -> None: ...

    @abstractmethod
    def record_agent_turn(self, *, turn_number: int,
                          agent_name: str) -> None: ...

    @abstractmethod
    def record_subagent_dispatch(self, *, parent_step_id: str,
                                 subagent_name: str) -> None: ...
```

`Span` is a context manager (`__enter__` returns self, `__exit__` records
duration + status). Callers use it as:

```python
with tracer.start_span("llm_call", {"model": model}) as span:
    result = call_llm(...)
    span.set_attribute("prompt_tokens", result.usage.prompt_tokens)
```

`TraceContext` uses `contextvars.ContextVar` to carry `session_id`,
`user_id`, `trace_id`, `agent_name`, and `role` through the call stack — including
across `asyncio` boundaries (subagent dispatches). Set once at request entry
(in the agent turn loop), inherited by all nested spans automatically.

#### `noop.py`

`NoOpTracer` — the default provider. Every method is a no-op. `start_span()`
returns a `NoOpSpan` whose `__enter__`/`__exit__` do nothing. This guarantees
zero overhead when tracing is disabled and means call sites never need
`if tracing_enabled:` guards.

#### `otel.py`

`OtelTracer` — lazy-imports `opentelemetry` at first use (in `__init__`).
If the import fails, logs a warning and **degrades to no-op behavior** (never
crashes the agent loop). Translates the ABC methods into OTEL spans:

- `start_span(name, attributes)` → `tracer.start_span(name)` with attributes set
- `record_llm_call(...)` → creates a span `llm_call` with attributes
  `model`, `prompt_tokens`, `completion_tokens`, `latency_ms`, `error`
- `record_tool_call(...)` → span `tool_call` with `tool_name`, `args_hash`,
  `duration_ms`, `success`, `error`
- `record_agent_turn(...)` → span `agent_turn` with `turn_number`, `agent_name`
- `record_subagent_dispatch(...)` → span `subagent_dispatch` with
  `parent_step_id`, `subagent_name`

Every span inherits `session_id`/`user_id`/`trace_id`/`agent_name`/`role` from
`TraceContext` as span attributes.

#### `__init__.py`

`get_tracer() -> TracingProvider` singleton factory. Reads
`settings.TRACING_PROVIDER`:
- `"noop"` (default) → `NoOpTracer()`
- `"otel"` → `OtelTracer()`

### Integration points

| # | File | What gets instrumented |
|---|---|---|
| 1 | `services/llm_service.py` | Wrap `call_llm`/`chat_completion`/`stream_chat` with `start_span("llm_call")` + `record_llm_call(model, prompt_tokens, completion_tokens, latency_ms, error)`. |
| 2 | `services/agent_tools.py` | Wrap `execute_tool_with_retry` with `record_tool_call(tool_name, args_hash=sha256(args)[:16], duration_ms, success, error)`. |
| 3 | `routers/agents.py` (`add_message_stream`) | `record_agent_turn(turn_number, agent_name)` at the top of each turn iteration. Set `TraceContext` values here (session/user/agent). |
| 4 | `services/swarm/orchestrator.py` | `record_subagent_dispatch(parent_step_id, subagent_name)` when dispatching to a subagent. |

### Config additions

```python
TRACING_PROVIDER: str = "noop"            # "noop" | "otel"
OTEL_EXPORTER_OTLP_ENDPOINT: str = ""      # standard OTEL env var
OTEL_SERVICE_NAME: str = "zhanlu-backend"  # service name for OTEL
```

### Error handling

- `OtelTracer` import failure → log warning, degrade to no-op. Agent loop never
  crashes due to tracing.
- All `record_*` methods swallow internal errors (tracing is best-effort; a
  tracing failure must never break an LLM call or tool execution).
- `args_hash` is a truncated SHA-256 (first 16 hex chars) — never logs raw args
  (which may contain secrets).

---

## B1 — Pluggable RBAC Authorization

### Package: `backend/app/services/authz/`

#### `base.py`

```python
class ResourceType(Enum):
    TOOL = "tool"
    MODEL = "model"
    SKILL = "skill"
    SANDBOX = "sandbox"
    MCP_SERVER = "mcp_server"
    ROUTE = "route"           # defined now, wired in future work

@dataclass
class AuthorizationDecision:
    allowed: bool
    reason: str = ""
    denied_resources: list[str] = field(default_factory=list)

class AuthorizationProvider(ABC):
    @abstractmethod
    def can_access(self, *, role: str, resource_type: ResourceType,
                   resource_id: str, action: str = "use") -> AuthorizationDecision: ...

    @abstractmethod
    def filter_resources(self, *, role: str, resource_type: ResourceType,
                         resource_ids: list[str]) -> list[str]: ...
```

`filter_resources` is the batch helper used by `tool_registry` to strip denied
tools from the model's tool schema in one call. It calls `can_access` per item
internally.

#### `rbac.py` — `RbacAuthorizationProvider`

**Construction-time policy compilation:**
- Accepts a `dict[str, RolePolicy]` mapping role name → policy.
- Validates each policy: unknown keys → `ValueError` (no silent mis-grants).
- Compiles into fast lookup structures: `_denied: dict[(role, resource_type), set[str]]`
  and `_allowed: dict[(role, resource_type), set[str] | None]` (`None` = all allowed).

**Semantics:**
- **Deny-always-wins**: if any applicable deny rule matches the resource, the
  decision is `allowed=False`. Deny is checked before allow.
- **Fail-closed**: unknown role (not in policy map) → `allowed=False` with
  reason `"unknown role"`. Safer than fail-open.
- **Explicit allow**: if no deny matches and an allow list exists, the resource
  must be in the allow list. If allow is `None` (wildcard), all are allowed
  (subject to no deny match).
- **No deny + no allow list** → `allowed=True` (permissive for resources not
  mentioned in any policy — matches existing behavior where everything is
  allowed by default).

#### `policies.py` — Built-in role policies

Three built-in roles, defined as plain dicts that `RbacAuthorizationProvider`
compiles:

| Role | Tools | Models | Skills | Sandbox | Notes |
|---|---|---|---|---|---|
| `admin` | All allowed (`None`) | All allowed | All allowed | All allowed | Superuser; no denies |
| `user` | All allowed except explicitly denied (configurable; default: none denied) | All allowed | All allowed | Allowed | Standard user; RBAC is permissive by default, restricts only when policies are customized |
| `viewer` | Allow only `read_file`, `web_search`, `web_extract`, `list_tools`, `list_knowledge_bases`, `list_market_agents` | All allowed (read-only model use is fine) | All allowed | Denied | Read-only role |

The built-in `user` role is intentionally permissive (matches current behavior —
all tools allowed). The value of RBAC is that an operator can customize the
policies via `AUTHZ_POLICIES_JSON` to deny specific tools/models/skills per role
without touching code. The `viewer` role demonstrates the deny/allow mechanism.

#### `__init__.py`

`get_authorizer() -> AuthorizationProvider` singleton factory:
- Reads `settings.AUTHZ_PROVIDER`:
  - `"rbac"` (default) → `RbacAuthorizationProvider` with built-in policies,
    optionally overridden by `AUTHZ_POLICIES_JSON` (inline JSON).
  - `"none"` → `PermissiveAuthorizer` (allows everything — for testing/legacy).

### Coexistence: two-gate design

RBAC runs as the **first** gate. If it denies → stop. If it allows → the
existing `PermissionChecker` runs unchanged (mode/path/command checks).

```
Tool call request
    │
    ▼
┌──────────────────────────────┐
│ GATE 1: RBAC authorizer      │  ← NEW (role → resource)
│ can_access(role, TOOL, name) │
└──────────┬───────────────────┘
           │ allowed?
     ┌─────┴─────┐
    YES          NO → denied (RBAC: role policy)
     │
     ▼
┌──────────────────────────────┐
│ GATE 2: PermissionChecker    │  ← EXISTING (mode/path/cmd)
│ check_tool(name, args, ...)  │
└──────────┬───────────────────┘
           │ allowed?
     ┌─────┴─────┐
    YES          NO → denied (mode/path/sensitive)
     │
     ▼
  execute tool
```

RBAC can only *add* restrictions, never *remove* existing ones. This guarantees
zero regression: every tool that was allowed before is still allowed (the
default `user` role permits all tools), and RBAC cannot weaken the existing
mode/path protections.

### Integration points

| # | File | What changes |
|---|---|---|
| 1 | `services/tool_registry.py` `get_definitions()` | Before returning tool schemas to the LLM, call `authorizer.filter_resources(role=role, resource_type=ResourceType.TOOL, resource_ids=tool_names)`. Denied tools are stripped from the schema — the model never sees them. |
| 2 | `services/agent_tools.py` `execute_tool_with_retry` | Before dispatching to the handler, call `authorizer.can_access(role=role, resource_type=ResourceType.TOOL, resource_id=tool_name)`. Defense-in-depth: if the model hallucinates a denied tool name, this blocks it. |

**Role source & propagation:** `User.role` (already exists on the User model,
already stripped from client updates per the 2026-07-28 security hardening).
The role is propagated via `TraceContext` (the `contextvars`-based context
defined in A1's `tracing/base.py`), extended to carry a `role` field alongside
`session_id`/`user_id`/`trace_id`/`agent_name`.

At request entry (the agent turn loop in `routers/agents.py`), the authenticated
user's role is set into `TraceContext`:

```python
TraceContext.set(session_id=..., user_id=..., role=user.role, agent_name=...)
```

The authorizer integration in `tool_registry.get_definitions()` and
`agent_tools.execute_tool_with_retry()` reads the role from
`TraceContext.current_role()` — a contextvar accessor that returns `None` when
no context is set (e.g. in background jobs). When the role is `None`, the
authorizer treats it as the `"user"` role (the permissive default), so
background tasks and unauthenticated code paths keep working. This avoids
changing the signatures of `get_definitions()` or `execute_tool_with_retry()`.

### Config additions

```python
AUTHZ_PROVIDER: str = "rbac"           # "rbac" | "none"
AUTHZ_POLICIES_JSON: str = ""           # inline JSON overriding built-in policies; "" = built-ins
```

`AUTHZ_POLICIES_JSON` format (optional):
```json
{
  "user": {
    "tools": {"deny": ["execute_code"]},
    "models": {"allow": ["gpt-4o-mini", "deepseek-chat"]}
  }
}
```

---

## B3 — SkillScan Deterministic Safety Scanner

### Package: `backend/app/services/skill_scanner/`

#### `base.py`

```python
@dataclass
class Finding:
    rule_id: str
    severity: Literal["critical", "warning", "info"]
    description: str
    line_number: int | None
    matched_text: str          # truncated to 80 chars, secrets redacted

@dataclass
class ScanResult:
    skill_name: str
    findings: list[Finding]
    has_critical: bool          # any critical finding?
    summary: str                # one-line human-readable, e.g. "2 critical, 1 warning"

@dataclass
class ScanRule:
    id: str
    pattern: re.Pattern
    severity: Literal["critical", "warning", "info"]
    description: str
```

#### `rules.py` — Scan rule definitions

All rules are deterministic regex patterns. **No inference from bare module
names** — only concrete syntax matches. This avoids false positives on skills
that merely *mention* `subprocess` in documentation.

| Severity | Rule ID | Pattern (concept) | What it catches |
|---|---|---|---|
| CRITICAL | `private_key_pem` | `-----BEGIN [A-Z ]*PRIVATE KEY-----` | Embedded PEM private keys |
| CRITICAL | `aws_access_key` | `AKIA[0-9A-Z]{16}` | AWS access key IDs |
| CRITICAL | `github_token` | `ghp_[a-zA-Z0-9]{36}` | GitHub personal access tokens |
| CRITICAL | `shell_exec` | `os\.system\|os\.popen\|subprocess\.(call|run|Popen)\b` | Shell/process execution |
| CRITICAL | `eval_exec` | `\beval\s*\(\|\bexec\s*\(` | Dynamic code execution |
| WARNING | `external_post` | `requests\.(post|put)\|urllib\.request\.urlopen` | Outbound network writes |
| WARNING | `sensitive_path_ref` | `\.ssh\b\|\.aws/credentials\|\.env\b` | References to sensitive paths |
| INFO | `dynamic_import` | `importlib\.import_module\|__import__\s*\(` | Dynamic module loading |

**Redaction:** `matched_text` is truncated to 80 characters and, for secret-like
rules (`private_key_pem`, `aws_access_key`, `github_token`), the matched text is
replaced with `[REDACTED:rule_id]` so secrets are never logged or stored in
findings.

#### `scanner.py` — `SkillScanner`

```python
class SkillScanner:
    def __init__(self, rules: list[ScanRule] | None = None):
        self.rules = rules or DEFAULT_RULES

    def scan(self, *, skill_name: str, body: str,
             prerequisites: dict | None = None) -> ScanResult:
        findings = []
        for rule in self.rules:
            for match in rule.pattern.finditer(body):
                findings.append(Finding(
                    rule_id=rule.id,
                    severity=rule.severity,
                    description=rule.description,
                    line_number=body[:match.start()].count("\n") + 1,
                    matched_text=self._redact(match.group(0), rule),
                ))
        # Also scan prerequisites values if present (string values only)
        ...
        return ScanResult(
            skill_name=skill_name,
            findings=findings,
            has_critical=any(f.severity == "critical" for f in findings),
            summary=self._summarize(findings),
        )
```

#### `__init__.py`

```python
def scan_skill(metadata: SkillMetadata) -> ScanResult:
    """Scan a parsed skill for security issues. Warn-only — never blocks."""
    if not settings.SKILL_SCAN_ENABLED:
        return ScanResult(skill_name=metadata.name, findings=[], has_critical=False, summary="scan disabled")
    scanner = _get_scanner()
    result = scanner.scan(skill_name=metadata.name, body=metadata.body,
                          prerequisites=metadata.prerequisites)
    _log_findings(result)
    return result
```

### Policy: warn-only

Per the user's design choice:
- All findings logged at their severity level:
  - CRITICAL → `logger.critical`
  - WARNING → `logger.warning`
  - INFO → `logger.info`
- Findings attached to `SkillMetadata.scan_findings: list[dict]` (new field on
  the dataclass, default `[]`).
- Skill activates normally — **no blocking**.
- Reserved for future: `SKILL_SCAN_BLOCK_CRITICAL: bool = False`. When flipped
  to `True`, `skills_loader` will refuse to register skills with `has_critical`.
  The scanner already computes `has_critical`, so this is a 3-line gate.

### Integration point

| # | File | What changes |
|---|---|---|
| 1 | `services/skills_loader/__init__.py` | After parsing `SkillMetadata` (from frontmatter or manifest), before registering into `ToolRegistry`, call `scan_skill(metadata)`. Attach `result.findings` (as list of dicts) to `metadata.scan_findings`. Log findings. Continue activation (warn-only). |

`SkillMetadata` gets a new field:
```python
@dataclass
class SkillMetadata:
    ...
    scan_findings: list[dict[str, Any]] = field(default_factory=list)
```

### Config additions

```python
SKILL_SCAN_ENABLED: bool = True            # master switch
SKILL_SCAN_BLOCK_CRITICAL: bool = False    # reserved — default warn-only
```

---

## Config summary (all additions to `config.py`)

```python
# ── A1: Distributed tracing ──────────────────────────────────────────
TRACING_PROVIDER: str = "noop"            # "noop" | "otel"
OTEL_EXPORTER_OTLP_ENDPOINT: str = ""      # standard OTEL env var
OTEL_SERVICE_NAME: str = "zhanlu-backend"

# ── B1: RBAC authorization ───────────────────────────────────────────
AUTHZ_PROVIDER: str = "rbac"              # "rbac" | "none"
AUTHZ_POLICIES_JSON: str = ""              # inline JSON overriding built-in policies

# ── B3: Skill safety scanner ─────────────────────────────────────────
SKILL_SCAN_ENABLED: bool = True            # master switch
SKILL_SCAN_BLOCK_CRITICAL: bool = False    # reserved — default warn-only
```

All defaults preserve current behavior:
- Tracing is no-op (zero overhead unless explicitly enabled).
- RBAC is on but the default `user` role permits all tools (matches current
  permissive behavior; value is in customizability).
- Skill scan is warn-only (visibility without disruption).

---

## Testing strategy

All tests run with `DATABASE_URL="sqlite:///./test_runtime.db"` on explicit file
paths (per the RAM-limited dev box constraint — no broad pytest collection, no
parallel workers).

### A1 Tracing tests

**Unit (`tests/test_tracing.py`):**
- `NoOpTracer`: all `record_*` methods are no-ops; `start_span()` returns a span
  whose `__enter__`/`__exit__` don't raise.
- `OtelTracer` (mocked SDK): verifies span names, attributes (model,
  prompt_tokens, etc.), and that `TraceContext` values propagate as span
  attributes.
- Graceful fallback: when `opentelemetry` import fails, `OtelTracer` degrades to
  no-op without raising.
- `TraceContext`: values set in an outer scope are visible in nested spans
  (including across an `asyncio` boundary).

**Integration (`tests/test_tracing_integration.py`):**
- `llm_service.call_llm` records a span with the model + token usage.
- `agent_tools.execute_tool_with_retry` records a tool span on success and on
  failure.
- Tracing errors are swallowed — a tracing failure never breaks an LLM call.

### B1 RBAC tests

**Unit (`tests/test_authz.py`):**
- `RbacAuthorizationProvider`:
  - Deny-always-wins: a resource in both allow and deny → denied.
  - Fail-closed: unknown role → `allowed=False`.
  - `filter_resources`: removes denied items, keeps allowed items.
  - Policy compilation rejects unknown keys → `ValueError`.
  - Built-in roles: `admin` allows all, `viewer` allows only read tools,
    `user` allows all (default permissive).
  - Custom policies via `AUTHZ_POLICIES_JSON` override built-ins.

**Integration (`tests/test_authz_integration.py`):**
- `tool_registry.get_definitions()` filters denied tools from the schema (model
  never sees them).
- `agent_tools.execute_tool_with_retry` blocks a denied tool (defense-in-depth).
- Two-gate coexistence: RBAC deny stops before `PermissionChecker` runs; RBAC
  allow passes through to `PermissionChecker` which may still deny on mode/path.

### B3 SkillScan tests

**Unit (`tests/test_skill_scanner.py`):**
- Each rule pattern matches its target string (e.g. `shell_exec` matches
  `os.system("ls")`).
- Each rule does NOT match benign text (e.g. `shell_exec` does not match
  "This skill uses the system to...").
- `ScanResult.has_critical` is `True` when any critical finding exists.
- Secret redaction: `private_key_pem` finding's `matched_text` is
  `[REDACTED:private_key_pem]`, not the actual key.
- `scan_skill()` with `SKILL_SCAN_ENABLED=False` returns empty result.

**Integration (`tests/test_skill_scanner_integration.py`):**
- `skills_loader` attaches findings to `SkillMetadata.scan_findings`.
- Warn-only: a skill with critical findings still activates (registered into
  `ToolRegistry`).
- Findings are logged at the correct severity level.

---

## Out of scope (explicitly deferred)

**Deferred from MVP (Phase 1a → Phase 1b):**

| Item | Reason |
|---|---|
| `record_agent_turn` + `record_subagent_dispatch` instrumentation | Lower value than LLM/tool call tracing; add once base tracing is proven. |
| `AUTHZ_POLICIES_JSON` custom role policies | Built-in 3 roles cover MVP; custom policies add config parsing + validation complexity. |
| Defense-in-depth RBAC re-check in `agent_tools` | Tool-schema filtering (GATE 1) already blocks denied tools from the model; re-check is belt-and-suspenders. |
| B3 WARNING/INFO rules + `SKILL_SCAN_BLOCK_CRITICAL` flag | 5 CRITICAL rules cover the highest-risk patterns; blocking can come when rules are battle-tested. |
| Model/skill/sandbox RBAC filtering | Tool filtering is the highest-value RBAC gate; other resource types add scope without proportional value yet. |

**Deferred to separate specs (later phases):**

| Item | Reason | Future spec |
|---|---|---|
| Route-level RBAC enforcement (`ROUTE` resource type wired into routers) | Needs careful per-route design. | Separate spec |
| LangSmith / Langfuse tracing providers | ABC supports them; implementation deferred until OTEL proves out. | Separate spec |
| LLM-based contextual skill review | Warn-only policy makes LLM review lower priority. | Separate spec |
| Token-level profiler (A2) | Different scope (flame-graph profiling, not span export). | Separate spec |
| Subagent token attribution (D3) | Depends on tracing infra but is a distinct feature. | Separate spec |
| AgentMiddleware chain (B2) | Pluggable middleware is a separate extensibility layer. | Separate spec |

---

## Implementation order (MVP)

1. **B3 SkillScan** — most self-contained (1 file, 5 rules, 1 integration point).
   Build + test first.
2. **A1 Tracing** — 2 instrumentation points, purely additive. Build + test second.
3. **B1 RBAC** — 1 integration point, built-in roles only. Build + test last.

Each step is small and independently testable. Total: 3 packages, 9 files.
