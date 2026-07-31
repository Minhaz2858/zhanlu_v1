# Phase 1 MVP — Tracing (A1) + RBAC (B1) + SkillScan (B3) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add three independent, minimal subsystems to the Zhanlu backend: a deterministic skill safety scanner (warn-only), a pluggable tracing layer (OpenTelemetry, no-op default), and a role-based authorization layer (coexists with existing PermissionChecker).

**Architecture:** Three self-contained packages (`services/skill_scanner/`, `services/tracing/`, `services/authz/`) each integrating at one natural hook point. No cross-dependencies. All defaults preserve current behavior. Heavy SDKs are lazy-imported with graceful fallback.

**Tech Stack:** Python 3.11+, FastAPI, Pydantic Settings, pytest, contextvars, stdlib `re`/`hashlib`/`logging`.

## Global Constraints

- **Test DB:** `DATABASE_URL="sqlite:///./test_runtime.db"` — run tests by explicit file path only (RAM-limited dev box, no broad collection, no `-n` parallel workers).
- **Dependencies:** All new SDKs (opentelemetry) are optional extras, lazy-imported at first use. Defaults work with zero new pip installs. If import fails → graceful fallback to no-op, never crash.
- **No behavior change by default:** Tracing is no-op (`TRACING_PROVIDER="noop"`), RBAC default `user` role permits all tools, skill scan is warn-only.
- **No secrets in logs:** `args_hash` is truncated SHA-256 (first 16 hex chars). Scan findings redact secret-like matches.
- **Fail-closed RBAC:** Unknown role → deny. Deny-always-wins.
- **Follow existing patterns:** Use `from __future__ import annotations`, dataclasses, module-level singletons with `get_*()` factory functions (matching `permissions/__init__.py` and `agent_metrics.py`).

---

## File Structure

```
backend/app/services/
├── skill_scanner/           ← B3 (Task 1-2)
│   ├── __init__.py          scan_skill() entry point + get_scanner() singleton
│   └── scanner.py           ScanRule, Finding, ScanResult, SkillScanner, DEFAULT_RULES
├── tracing/                 ← A1 (Task 3-5)
│   ├── __init__.py          get_tracer() singleton + TraceContext
│   ├── base.py              TracingProvider ABC + Span
│   ├── noop.py              NoOpTracer (default)
│   └── otel.py              OtelTracer (lazy import, graceful fallback)
└── authz/                   ← B1 (Task 6-7)
    ├── __init__.py          get_authorizer() singleton
    ├── base.py              AuthorizationProvider ABC + ResourceType + AuthorizationDecision
    └── rbac.py              RbacAuthorizationProvider + BUILTIN_ROLES

Modified files:
├── config.py                +6 config fields (TRACING_*, OTEL_*, AUTHZ_*, SKILL_SCAN_*)
├── services/skills_loader/__init__.py   +scan_findings field + scan hook in parse_skill_file
├── services/llm_service.py              +tracing in call_llm
├── services/agent_tools.py              +tracing in execute_tool_with_retry
└── services/tool_registry.py            +RBAC filter in get_schemas

Test files:
├── tests/test_skill_scanner.py
├── tests/test_tracing.py
└── tests/test_authz.py
```

---

## Task 1: B3 SkillScan — Scanner + Rules

**Files:**
- Create: `backend/app/services/skill_scanner/scanner.py`
- Test: `backend/tests/test_skill_scanner.py`

**Interfaces:**
- Consumes: nothing (self-contained)
- Produces: `ScanRule`, `Finding`, `ScanResult`, `SkillScanner`, `DEFAULT_RULES`, `scan_text(body, skill_name) -> ScanResult`

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_skill_scanner.py`:

```python
"""Tests for the deterministic skill safety scanner."""
from __future__ import annotations

import sys
from pathlib import Path

# Ensure backend/ is on sys.path so `app.` imports resolve
_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from app.services.skill_scanner.scanner import (
    DEFAULT_RULES,
    Finding,
    ScanResult,
    ScanRule,
    SkillScanner,
    scan_text,
)


def test_scan_detects_private_key_pem():
    body = "Here is a key:\n-----BEGIN RSA PRIVATE KEY-----\nMIIE..."
    result = scan_text(body, skill_name="test-skill")
    assert result.has_critical is True
    assert any(f.rule_id == "private_key_pem" for f in result.findings)


def test_scan_detects_aws_access_key():
    body = "aws_key = AKIAIOSFODNN7EXAMPLE"
    result = scan_text(body, skill_name="test-skill")
    assert result.has_critical is True
    assert any(f.rule_id == "aws_access_key" for f in result.findings)


def test_scan_detects_github_token():
    body = "token = ghp_1234567890abcdefghijklmnopqrstuvwxyz"
    result = scan_text(body, skill_name="test-skill")
    assert result.has_critical is True
    assert any(f.rule_id == "github_token" for f in result.findings)


def test_scan_detects_shell_exec():
    body = 'os.system("rm -rf /tmp/test")'
    result = scan_text(body, skill_name="test-skill")
    assert result.has_critical is True
    assert any(f.rule_id == "shell_exec" for f in result.findings)


def test_scan_detects_subprocess_run():
    body = "subprocess.run(['ls', '-la'])"
    result = scan_text(body, skill_name="test-skill")
    assert result.has_critical is True
    assert any(f.rule_id == "shell_exec" for f in result.findings)


def test_scan_detects_eval():
    body = "result = eval(user_input)"
    result = scan_text(body, skill_name="test-skill")
    assert result.has_critical is True
    assert any(f.rule_id == "eval_exec" for f in result.findings)


def test_scan_no_false_positive_on_benign_text():
    body = (
        "# My Skill\n\nThis skill helps with documentation.\n"
        "It does not execute any code.\n\n## Usage\n\n"
        "Just read the instructions."
    )
    result = scan_text(body, skill_name="test-skill")
    assert result.has_critical is False
    assert len(result.findings) == 0


def test_scan_no_false_positive_on_documentation_mentions():
    body = "This skill mentions os.system and subprocess in its docs but does not call them."
    result = scan_text(body, skill_name="test-skill")
    assert result.has_critical is False


def test_secret_redaction_in_matched_text():
    body = "-----BEGIN RSA PRIVATE KEY-----\nMIIBOgIBAAJBAKjQ4w==\n-----END RSA PRIVATE KEY-----"
    result = scan_text(body, skill_name="test-skill")
    key_findings = [f for f in result.findings if f.rule_id == "private_key_pem"]
    assert len(key_findings) == 1
    assert "MIIBOgIBAAJBAKjQ4w" not in key_findings[0].matched_text
    assert "REDACTED" in key_findings[0].matched_text


def test_finding_has_line_number():
    body = "line one\nline two\nos.system('ls')\nline four"
    result = scan_text(body, skill_name="test-skill")
    shell_findings = [f for f in result.findings if f.rule_id == "shell_exec"]
    assert len(shell_findings) == 1
    assert shell_findings[0].line_number == 3


def test_scan_result_summary():
    body = "os.system('ls')\nAKIAIOSFODNN7EXAMPLE"
    result = scan_text(body, skill_name="test-skill")
    assert "2 critical" in result.summary


def test_scan_empty_body():
    result = scan_text("", skill_name="empty-skill")
    assert result.has_critical is False
    assert len(result.findings) == 0
    assert "0 critical" in result.summary


def test_default_rules_count():
    """MVP has exactly 5 critical rules."""
    critical_rules = [r for r in DEFAULT_RULES if r.severity == "critical"]
    assert len(critical_rules) == 5


def test_scanner_with_custom_rules():
    custom = [ScanRule(
        id="custom",
        pattern=__import__("re").compile(r"SECRET_CODE"),
        severity="warning",
        description="Custom rule",
    )]
    scanner = SkillScanner(rules=custom)
    result = scanner.scan(body="SECRET_CODE here", skill_name="test")
    assert len(result.findings) == 1
    assert result.findings[0].rule_id == "custom"
    assert result.has_critical is False
```

- [ ] **Step 2: Run test to verify it fails**

Run:
```bash
cd /root/zhanlu/backend && DATABASE_URL="sqlite:///./test_runtime.db" python3 -m pytest tests/test_skill_scanner.py -v
```
Expected: FAIL with `ModuleNotFoundError: No module named 'app.services.skill_scanner'`

- [ ] **Step 3: Write minimal implementation**

Create `backend/app/services/skill_scanner/scanner.py`:

```python
"""Deterministic skill safety scanner.

Scans skill body text for high-confidence security patterns (private keys,
cloud credentials, shell execution, dynamic code execution). Warn-only —
findings are logged and attached to metadata but never block activation.
"""
from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from typing import Literal

logger = logging.getLogger(__name__)

Severity = Literal["critical", "warning", "info"]

# Rules whose matched text is a secret — redact before storing/logging.
_SECRET_RULE_IDS: frozenset[str] = frozenset({
    "private_key_pem",
    "aws_access_key",
    "github_token",
})


@dataclass
class ScanRule:
    """A single deterministic scan rule."""
    id: str
    pattern: re.Pattern[str]
    severity: Severity
    description: str


@dataclass
class Finding:
    """A single scan finding."""
    rule_id: str
    severity: Severity
    description: str
    line_number: int | None
    matched_text: str  # truncated to 80 chars, secrets redacted


@dataclass
class ScanResult:
    """Result of scanning a skill."""
    skill_name: str
    findings: list[Finding] = field(default_factory=list)
    has_critical: bool = False
    summary: str = "0 critical, 0 warning, 0 info"


def _redact(matched: str, rule_id: str) -> str:
    """Truncate to 80 chars and redact secrets."""
    if rule_id in _SECRET_RULE_IDS:
        return f"[REDACTED:{rule_id}]"
    return matched[:80]


def _summarize(findings: list[Finding]) -> str:
    critical = sum(1 for f in findings if f.severity == "critical")
    warning = sum(1 for f in findings if f.severity == "warning")
    info = sum(1 for f in findings if f.severity == "info")
    return f"{critical} critical, {warning} warning, {info} info"


# ---------------------------------------------------------------------------
# MVP rules — 5 CRITICAL patterns only (warn-only policy)
# ---------------------------------------------------------------------------

DEFAULT_RULES: list[ScanRule] = [
    ScanRule(
        id="private_key_pem",
        pattern=re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
        severity="critical",
        description="Embedded PEM private key detected",
    ),
    ScanRule(
        id="aws_access_key",
        pattern=re.compile(r"AKIA[0-9A-Z]{16}"),
        severity="critical",
        description="AWS access key ID detected",
    ),
    ScanRule(
        id="github_token",
        pattern=re.compile(r"ghp_[a-zA-Z0-9]{36}"),
        severity="critical",
        description="GitHub personal access token detected",
    ),
    ScanRule(
        id="shell_exec",
        pattern=re.compile(r"os\.system\s*\(|os\.popen\s*\(|subprocess\.(call|run|Popen)\s*\("),
        severity="critical",
        description="Shell/process execution detected",
    ),
    ScanRule(
        id="eval_exec",
        pattern=re.compile(r"\beval\s*\(|\bexec\s*\("),
        severity="critical",
        description="Dynamic code execution (eval/exec) detected",
    ),
]


class SkillScanner:
    """Scans skill body text for security patterns."""

    def __init__(self, rules: list[ScanRule] | None = None):
        self.rules = rules if rules is not None else DEFAULT_RULES

    def scan(self, *, body: str, skill_name: str) -> ScanResult:
        findings: list[Finding] = []
        for rule in self.rules:
            for match in rule.pattern.finditer(body):
                line_number = body[:match.start()].count("\n") + 1
                findings.append(Finding(
                    rule_id=rule.id,
                    severity=rule.severity,
                    description=rule.description,
                    line_number=line_number,
                    matched_text=_redact(match.group(0), rule.id),
                ))
        return ScanResult(
            skill_name=skill_name,
            findings=findings,
            has_critical=any(f.severity == "critical" for f in findings),
            summary=_summarize(findings),
        )


def scan_text(body: str, skill_name: str) -> ScanResult:
    """Convenience function: scan a body string with the default scanner."""
    return SkillScanner().scan(body=body, skill_name=skill_name)
```

- [ ] **Step 4: Run test to verify it passes**

Run:
```bash
cd /root/zhanlu/backend && DATABASE_URL="sqlite:///./test_runtime.db" python3 -m pytest tests/test_skill_scanner.py -v
```
Expected: PASS (14 tests)

- [ ] **Step 5: Commit**

```bash
cd /root/zhanlu/backend && git add app/services/skill_scanner/scanner.py tests/test_skill_scanner.py && git commit -m "feat: add deterministic skill safety scanner (B3 MVP)"
```

---

## Task 2: B3 SkillScan — Entry Point + skills_loader Integration

**Files:**
- Create: `backend/app/services/skill_scanner/__init__.py`
- Modify: `backend/app/config.py` (add `SKILL_SCAN_ENABLED`)
- Modify: `backend/app/services/skills_loader/__init__.py` (add `scan_findings` field + scan hook)
- Test: `backend/tests/test_skill_scanner.py` (append integration tests)

**Interfaces:**
- Consumes: `SkillScanner`, `ScanResult` from Task 1; `SkillMetadata` from skills_loader; `settings.SKILL_SCAN_ENABLED` from config
- Produces: `scan_skill(metadata) -> ScanResult`, `SkillMetadata.scan_findings: list[dict]`

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/test_skill_scanner.py`:

```python
# ── Integration: scan_skill entry point ──────────────────────────────

from app.services.skill_scanner import scan_skill
from app.services.skills_loader import SkillMetadata


def _make_metadata(name="test", body=""):
    return SkillMetadata(
        name=name, description="test", file_path="/tmp/test.md", body=body,
    )


def test_scan_skill_attaches_findings():
    meta = _make_metadata(body="os.system('ls')")
    result = scan_skill(meta)
    assert result.has_critical is True
    assert len(result.findings) == 1


def test_scan_skill_clean_metadata():
    meta = _make_metadata(body="Just a harmless skill.")
    result = scan_skill(meta)
    assert result.has_critical is False
    assert len(result.findings) == 0


def test_scan_skill_disabled_returns_empty(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "SKILL_SCAN_ENABLED", False)
    meta = _make_metadata(body="os.system('ls')")
    result = scan_skill(meta)
    assert len(result.findings) == 0
    assert result.has_critical is False


def test_parse_skill_file_attaches_scan_findings(tmp_path, monkeypatch):
    """Integration: parse_skill_file should attach scan_findings to metadata."""
    from app.config import settings
    monkeypatch.setattr(settings, "SKILL_SCAN_ENABLED", True)

    skill_file = tmp_path / "dangerous.md"
    skill_file.write_text(
        "---\nname: dangerous\ndescription: bad skill\n---\n\n"
        "os.system('rm -rf /')\n",
        encoding="utf-8",
    )

    from app.services.skills_loader import parse_skill_file
    meta = parse_skill_file(skill_file, source="test")
    assert meta is not None
    assert len(meta.scan_findings) > 0
    assert meta.scan_findings[0]["severity"] == "critical"


def test_parse_skill_file_clean_skill_has_empty_findings(tmp_path, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "SKILL_SCAN_ENABLED", True)

    skill_file = tmp_path / "safe.md"
    skill_file.write_text(
        "---\nname: safe\ndescription: good skill\n---\n\n# Safe Skill\n\nDoes nothing dangerous.\n",
        encoding="utf-8",
    )

    from app.services.skills_loader import parse_skill_file
    meta = parse_skill_file(skill_file, source="test")
    assert meta is not None
    assert meta.scan_findings == []
```

- [ ] **Step 2: Run test to verify it fails**

Run:
```bash
cd /root/zhanlu/backend && DATABASE_URL="sqlite:///./test_runtime.db" python3 -m pytest tests/test_skill_scanner.py -v -k "scan_skill or parse_skill_file"
```
Expected: FAIL with `ImportError: cannot import name 'scan_skill' from 'app.services.skill_scanner'`

- [ ] **Step 3: Write minimal implementation**

Create `backend/app/services/skill_scanner/__init__.py`:

```python
"""Skill safety scanner package — warn-only deterministic scanning."""
from __future__ import annotations

import logging

from app.config import settings
from app.services.skill_scanner.scanner import (
    DEFAULT_RULES,
    Finding,
    ScanResult,
    ScanRule,
    SkillScanner,
    scan_text,
)

logger = logging.getLogger(__name__)

_scanner: SkillScanner | None = None


def _get_scanner() -> SkillScanner:
    global _scanner
    if _scanner is None:
        _scanner = SkillScanner()
    return _scanner


def scan_skill(metadata) -> ScanResult:
    """Scan a parsed SkillMetadata for security issues.

    Warn-only: findings are logged and returned but never block activation.
    Returns an empty ScanResult when SKILL_SCAN_ENABLED is False.
    """
    if not settings.SKILL_SCAN_ENABLED:
        return ScanResult(
            skill_name=getattr(metadata, "name", "unknown"),
            findings=[],
            has_critical=False,
            summary="scan disabled",
        )

    scanner = _get_scanner()
    result = scanner.scan(
        body=getattr(metadata, "body", "") or "",
        skill_name=getattr(metadata, "name", "unknown"),
    )

    # Log findings at their severity level
    for finding in result.findings:
        if finding.severity == "critical":
            logger.critical(
                "SkillScan [%s] CRITICAL: %s (line %s) — %s",
                result.skill_name, finding.rule_id, finding.line_number,
                finding.description,
            )
        elif finding.severity == "warning":
            logger.warning(
                "SkillScan [%s] WARNING: %s — %s",
                result.skill_name, finding.rule_id, finding.description,
            )
        else:
            logger.info(
                "SkillScan [%s] INFO: %s — %s",
                result.skill_name, finding.rule_id, finding.description,
            )

    return result


__all__ = [
    "scan_skill",
    "scan_text",
    "SkillScanner",
    "ScanRule",
    "Finding",
    "ScanResult",
    "DEFAULT_RULES",
]
```

Add config field to `backend/app/config.py`. Find the line `SKILL_SCAN_ENABLED` does not exist yet — add after the sandbox config block (after line ~358, before `upload_path` property):

```python
    # ── B3: Skill safety scanner ───────────────────────────────────────
    SKILL_SCAN_ENABLED: bool = True            # master switch (warn-only)
```

Add `scan_findings` field to `SkillMetadata` in `backend/app/services/skills_loader/__init__.py`. Find the `SkillMetadata` dataclass (around line 48) and add the field after `source`:

```python
    source: str = "bundled"
    scan_findings: list[dict[str, Any]] = field(default_factory=list)
```

Add the scan hook in `parse_skill_file()`. Find the `return SkillMetadata(...)` block (around line 327) and change it to scan before returning:

```python
    meta = SkillMetadata(
        name=name,
        description=str(description),
        file_path=str(filepath),
        body=body,
        version=str(frontmatter.get("version", "1.0.0")),
        author=str(frontmatter.get("author", "")),
        license=str(frontmatter.get("license", "")),
        platforms=frontmatter.get("platforms", []) or [],
        tags=tags,
        category=category,
        trigger=str(trigger),
        summary=summary,
        runtime=runtime,
        user_invocable=bool(frontmatter.get("user_invocable", True)),
        prerequisites=frontmatter.get("prerequisites", {}) or {},
        source=source,
    )

    # B3: deterministic security scan (warn-only)
    try:
        from app.services.skill_scanner import scan_skill
        scan_result = scan_skill(meta)
        meta.scan_findings = [
            {
                "rule_id": f.rule_id,
                "severity": f.severity,
                "description": f.description,
                "line_number": f.line_number,
            }
            for f in scan_result.findings
        ]
    except Exception as exc:
        logger.debug("Skill scan failed for %s: %s (non-blocking)", name, exc)

    return meta
```

- [ ] **Step 4: Run test to verify it passes**

Run:
```bash
cd /root/zhanlu/backend && DATABASE_URL="sqlite:///./test_runtime.db" python3 -m pytest tests/test_skill_scanner.py -v
```
Expected: PASS (all 19 tests)

- [ ] **Step 5: Commit**

```bash
cd /root/zhanlu/backend && git add app/services/skill_scanner/__init__.py app/config.py app/services/skills_loader/__init__.py tests/test_skill_scanner.py && git commit -m "feat: wire skill scanner into skills_loader (B3 MVP, warn-only)"
```

---

## Task 3: A1 Tracing — Base ABC + NoOpTracer + TraceContext

**Files:**
- Create: `backend/app/services/tracing/base.py`
- Create: `backend/app/services/tracing/noop.py`
- Create: `backend/app/services/tracing/__init__.py`
- Modify: `backend/app/config.py` (add tracing config)
- Test: `backend/tests/test_tracing.py`

**Interfaces:**
- Consumes: nothing
- Produces: `TracingProvider` (ABC), `Span`, `NoOpTracer`, `TraceContext` (contextvars), `get_tracer()`, `TracingProvider.record_llm_call()`, `TracingProvider.record_tool_call()`

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_tracing.py`:

```python
"""Tests for the tracing provider ABC, NoOp tracer, and TraceContext."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from app.services.tracing import TraceContext, get_tracer
from app.services.tracing.base import Span, TracingProvider
from app.services.tracing.noop import NoOpTracer


def test_get_tracer_returns_noop_by_default():
    tracer = get_tracer()
    assert isinstance(tracer, NoOpTracer)


def test_noop_start_span_is_context_manager():
    tracer = NoOpTracer()
    with tracer.start_span("test") as span:
        assert isinstance(span, Span)
    # No exception = pass


def test_noop_record_methods_are_noops():
    tracer = NoOpTracer()
    tracer.record_llm_call(model="gpt-4o-mini", prompt_tokens=10,
                           completion_tokens=5, latency_ms=100.0)
    tracer.record_tool_call(tool_name="read_file", args_hash="abc123",
                            duration_ms=50.0, success=True)
    # No exception = pass


def test_noop_record_error_does_not_raise():
    tracer = NoOpTracer()
    tracer.record_llm_call(model="gpt-4o-mini", prompt_tokens=0,
                           completion_tokens=0, latency_ms=0.0,
                           error="timeout")
    # No exception = pass


def test_trace_context_set_and_get():
    TraceContext.clear()
    TraceContext.set(session_id="sess-1", user_id="user-1",
                     agent_name="general", role="user")
    assert TraceContext.current_session_id() == "sess-1"
    assert TraceContext.current_user_id() == "user-1"
    assert TraceContext.current_agent_name() == "general"
    assert TraceContext.current_role() == "user"


def test_trace_context_defaults_to_none():
    TraceContext.clear()
    assert TraceContext.current_session_id() is None
    assert TraceContext.current_role() is None


def test_trace_context_propagates_across_async():
    """TraceContext values set in outer scope are visible in async tasks."""
    TraceContext.clear()
    TraceContext.set(session_id="sess-async", user_id="user-async",
                     agent_name="test", role="admin")

    async def inner():
        return TraceContext.current_session_id()

    result = asyncio.run(inner())
    assert result == "sess-async"


def test_span_context_manager_records_duration():
    """Span __enter__/__exit__ should not raise and should be usable."""
    tracer = NoOpTracer()
    with tracer.start_span("llm_call", {"model": "gpt-4o-mini"}) as span:
        span.set_attribute("prompt_tokens", 100)
    # No exception = pass


def test_tracing_provider_is_abstract():
    """Cannot instantiate TracingProvider directly."""
    import pytest
    with pytest.raises(TypeError):
        TracingProvider()
```

- [ ] **Step 2: Run test to verify it fails**

Run:
```bash
cd /root/zhanlu/backend && DATABASE_URL="sqlite:///./test_runtime.db" python3 -m pytest tests/test_tracing.py -v
```
Expected: FAIL with `ModuleNotFoundError: No module named 'app.services.tracing'`

- [ ] **Step 3: Write minimal implementation**

Create `backend/app/services/tracing/base.py`:

```python
"""Tracing provider ABC, Span context manager, and TraceContext (contextvars)."""
from __future__ import annotations

import contextvars
import time
from abc import ABC, abstractmethod
from typing import Any


class Span:
    """A tracing span — a context manager that records duration + attributes.

    The base implementation is a no-op holder. Provider implementations
    (OtelTracer) override start_span to return a span backed by their SDK.
    """

    def __init__(self, name: str, attributes: dict[str, Any] | None = None):
        self.name = name
        self._attributes: dict[str, Any] = dict(attributes or {})
        self._start: float = 0.0

    def __enter__(self) -> "Span":
        self._start = time.monotonic()
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self._attributes["duration_ms"] = (time.monotonic() - self._start) * 1000

    def set_attribute(self, key: str, value: Any) -> None:
        self._attributes[key] = value

    @property
    def attributes(self) -> dict[str, Any]:
        return dict(self._attributes)


class TracingProvider(ABC):
    """Abstract base for tracing providers.

    All methods are best-effort — implementations must swallow internal errors
    so a tracing failure never breaks the agent loop.
    """

    @abstractmethod
    def start_span(self, name: str, attributes: dict[str, Any] | None = None) -> Span: ...

    @abstractmethod
    def record_llm_call(
        self,
        *,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        latency_ms: float,
        error: str | None = None,
    ) -> None: ...

    @abstractmethod
    def record_tool_call(
        self,
        *,
        tool_name: str,
        args_hash: str,
        duration_ms: float,
        success: bool,
        error: str | None = None,
    ) -> None: ...
```

Create `backend/app/services/tracing/noop.py`:

```python
"""No-op tracer — default provider, zero overhead."""
from __future__ import annotations

from app.services.tracing.base import Span, TracingProvider


class NoOpTracer(TracingProvider):
    """Tracing provider that does nothing. Used when tracing is disabled."""

    def start_span(self, name: str, attributes: dict | None = None) -> Span:
        return Span(name, attributes)

    def record_llm_call(self, *, model, prompt_tokens, completion_tokens,
                        latency_ms, error=None) -> None:
        pass

    def record_tool_call(self, *, tool_name, args_hash, duration_ms,
                         success, error=None) -> None:
        pass
```

Create `backend/app/services/tracing/__init__.py`:

```python
"""Tracing package — pluggable observability for LLM/tool calls.

Default provider is NoOpTracer (zero overhead). Set TRACING_PROVIDER="otel"
and OTEL_EXPORTER_OTLP_ENDPOINT to enable OpenTelemetry export.
"""
from __future__ import annotations

import contextvars
from typing import Any

from app.services.tracing.base import Span, TracingProvider
from app.services.tracing.noop import NoOpTracer

# ---------------------------------------------------------------------------
# TraceContext — contextvars that flow through async calls
# ---------------------------------------------------------------------------

_session_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "session_id", default=None)
_user_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "user_id", default=None)
_agent_name: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "agent_name", default=None)
_role: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "role", default=None)


class TraceContext:
    """Request-scoped tracing context carried via contextvars.

    Set once at request entry; inherited by all nested spans and async tasks.
    """

    @staticmethod
    def set(*, session_id: str | None = None, user_id: str | None = None,
            agent_name: str | None = None, role: str | None = None) -> None:
        if session_id is not None:
            _session_id.set(session_id)
        if user_id is not None:
            _user_id.set(user_id)
        if agent_name is not None:
            _agent_name.set(agent_name)
        if role is not None:
            _role.set(role)

    @staticmethod
    def clear() -> None:
        _session_id.set(None)
        _user_id.set(None)
        _agent_name.set(None)
        _role.set(None)

    @staticmethod
    def current_session_id() -> str | None:
        return _session_id.get()

    @staticmethod
    def current_user_id() -> str | None:
        return _user_id.get()

    @staticmethod
    def current_agent_name() -> str | None:
        return _agent_name.get()

    @staticmethod
    def current_role() -> str | None:
        return _role.get()

    @staticmethod
    def current_attributes() -> dict[str, Any]:
        """Return all current context values as a dict (for span attributes)."""
        return {
            "session_id": _session_id.get(),
            "user_id": _user_id.get(),
            "agent_name": _agent_name.get(),
            "role": _role.get(),
        }


# ---------------------------------------------------------------------------
# Singleton tracer factory
# ---------------------------------------------------------------------------

_tracer: TracingProvider | None = None


def get_tracer() -> TracingProvider:
    """Get the singleton TracingProvider instance.

    Reads settings.TRACING_PROVIDER:
    - "noop" (default) → NoOpTracer (zero overhead)
    - "otel" → OtelTracer (lazy-imports opentelemetry, falls back to NoOp on failure)
    """
    global _tracer
    if _tracer is not None:
        return _tracer

    from app.config import settings
    provider = getattr(settings, "TRACING_PROVIDER", "noop")

    if provider == "otel":
        try:
            from app.services.tracing.otel import OtelTracer
            _tracer = OtelTracer()
        except Exception:
            # Graceful fallback — never crash the app due to tracing
            _tracer = NoOpTracer()
    else:
        _tracer = NoOpTracer()

    return _tracer


def reset_tracer() -> None:
    """Reset the singleton (for testing)."""
    global _tracer
    _tracer = None


__all__ = [
    "TracingProvider",
    "Span",
    "NoOpTracer",
    "TraceContext",
    "get_tracer",
    "reset_tracer",
]
```

Add config fields to `backend/app/config.py` (after the `SKILL_SCAN_ENABLED` line added in Task 2):

```python
    # ── A1: Distributed tracing ─────────────────────────────────────────
    TRACING_PROVIDER: str = "noop"            # "noop" | "otel"
    OTEL_EXPORTER_OTLP_ENDPOINT: str = ""      # standard OTEL env var
    OTEL_SERVICE_NAME: str = "zhanlu-backend"
```

- [ ] **Step 4: Run test to verify it passes**

Run:
```bash
cd /root/zhanlu/backend && DATABASE_URL="sqlite:///./test_runtime.db" python3 -m pytest tests/test_tracing.py -v
```
Expected: PASS (9 tests)

- [ ] **Step 5: Commit**

```bash
cd /root/zhanlu/backend && git add app/services/tracing/ app/config.py tests/test_tracing.py && git commit -m "feat: add tracing ABC, NoOp tracer, and TraceContext (A1 MVP)"
```

---

## Task 4: A1 Tracing — OtelTracer (lazy import, graceful fallback)

**Files:**
- Create: `backend/app/services/tracing/otel.py`
- Test: `backend/tests/test_tracing.py` (append OTEL tests)

**Interfaces:**
- Consumes: `TracingProvider`, `Span`, `TraceContext` from Task 3
- Produces: `OtelTracer` — a `TracingProvider` that exports OTLP spans

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/test_tracing.py`:

```python
# ── OtelTracer tests (mocked SDK) ────────────────────────────────────

from unittest.mock import MagicMock, patch


def test_otel_tracer_initializes_when_sdk_available():
    """OtelTracer should initialize when opentelemetry is importable."""
    from app.services.tracing.otel import OtelTracer
    tracer = OtelTracer()
    assert tracer._available is True


def test_otel_tracer_falls_back_when_sdk_missing():
    """OtelTracer should degrade to no-op when opentelemetry is not installed."""
    from app.services.tracing.otel import OtelTracer
    with patch.dict("sys.modules", {"opentelemetry": None, "opentelemetry.trace": None}):
        tracer = OtelTracer()
        assert tracer._available is False
        # Should not raise — degrades to no-op
        tracer.record_llm_call(model="test", prompt_tokens=1,
                               completion_tokens=1, latency_ms=1.0)


def test_otel_tracer_record_llm_call_does_not_raise():
    from app.services.tracing.otel import OtelTracer
    tracer = OtelTracer()
    tracer.record_llm_call(model="gpt-4o-mini", prompt_tokens=100,
                           completion_tokens=50, latency_ms=250.0)
    tracer.record_llm_call(model="gpt-4o-mini", prompt_tokens=0,
                           completion_tokens=0, latency_ms=0.0,
                           error="timeout")


def test_otel_tracer_record_tool_call_does_not_raise():
    from app.services.tracing.otel import OtelTracer
    tracer = OtelTracer()
    tracer.record_tool_call(tool_name="read_file", args_hash="abc123",
                            duration_ms=50.0, success=True)
    tracer.record_tool_call(tool_name="write_file", args_hash="def456",
                            duration_ms=100.0, success=False, error="permission denied")


def test_otel_tracer_start_span_returns_span():
    from app.services.tracing.otel import OtelTracer
    tracer = OtelTracer()
    with tracer.start_span("test_span", {"key": "value"}) as span:
        span.set_attribute("extra", 42)
    # No exception = pass


def test_otel_tracer_includes_trace_context_attributes():
    """Spans should include TraceContext values as attributes."""
    from app.services.tracing.otel import OtelTracer
    TraceContext.clear()
    TraceContext.set(session_id="sess-1", user_id="user-1",
                     agent_name="general", role="admin")
    tracer = OtelTracer()
    with tracer.start_span("test") as span:
        pass
    # No exception = pass (attributes are set internally)
    TraceContext.clear()
```

- [ ] **Step 2: Run test to verify it fails**

Run:
```bash
cd /root/zhanlu/backend && DATABASE_URL="sqlite:///./test_runtime.db" python3 -m pytest tests/test_tracing.py -v -k "otel"
```
Expected: FAIL with `ModuleNotFoundError: No module named 'app.services.tracing.otel'`

- [ ] **Step 3: Write minimal implementation**

Create `backend/app/services/tracing/otel.py`:

```python
"""OpenTelemetry tracing provider — lazy-imports the SDK, graceful fallback.

When opentelemetry is not installed, or initialization fails, the tracer
degrades to no-op behavior. Tracing is best-effort: a tracing failure must
never break an LLM call or tool execution.
"""
from __future__ import annotations

import logging
import time
from typing import Any

from app.services.tracing.base import Span, TracingProvider
from app.services.tracing import TraceContext

logger = logging.getLogger(__name__)


class OtelSpan(Span):
    """Span backed by an OpenTelemetry span (when SDK is available)."""

    def __init__(self, name: str, attributes: dict[str, Any] | None,
                 otel_span: Any = None):
        super().__init__(name, attributes)
        self._otel_span = otel_span

    def set_attribute(self, key: str, value: Any) -> None:
        super().set_attribute(key, value)
        if self._otel_span is not None:
            try:
                self._otel_span.set_attribute(key, value)
            except Exception:
                pass  # best-effort


class OtelTracer(TracingProvider):
    """Tracing provider that exports spans via OpenTelemetry OTLP.

    Lazy-imports opentelemetry at construction. If the SDK is missing or
    initialization fails, _available=False and all methods become no-ops.
    """

    def __init__(self) -> None:
        self._available: bool = False
        self._tracer: Any = None
        try:
            from opentelemetry import trace
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import BatchSpanProcessor
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                OTLPSpanExporter,
            )
            from opentelemetry.sdk.resources import Resource

            from app.config import settings

            resource = Resource.create({
                "service.name": getattr(settings, "OTEL_SERVICE_NAME", "zhanlu-backend"),
            })
            provider = TracerProvider(resource=resource)

            endpoint = getattr(settings, "OTEL_EXPORTER_OTLP_ENDPOINT", "")
            if endpoint:
                exporter = OTLPSpanExporter(endpoint=endpoint)
                provider.add_span_processor(BatchSpanProcessor(exporter))

            trace.set_tracer_provider(provider)
            self._tracer = trace.get_tracer("zhanlu")
            self._available = True
        except Exception as exc:
            logger.warning(
                "OpenTelemetry not available — tracing degraded to no-op: %s", exc
            )
            self._available = False

    def start_span(self, name: str, attributes: dict[str, Any] | None = None) -> Span:
        if not self._available or self._tracer is None:
            return Span(name, attributes)

        # Merge TraceContext values into span attributes
        ctx_attrs = TraceContext.current_attributes()
        merged = {**ctx_attrs, **(attributes or {})}
        try:
            otel_span = self._tracer.start_as_current_span(name)
            otel_span.__enter__()
            for k, v in merged.items():
                if v is not None:
                    otel_span.set_attribute(k, v)
            return OtelSpan(name, merged, otel_span=otel_span)
        except Exception:
            return Span(name, merged)

    def record_llm_call(self, *, model: str, prompt_tokens: int,
                        completion_tokens: int, latency_ms: float,
                        error: str | None = None) -> None:
        if not self._available or self._tracer is None:
            return
        try:
            with self._tracer.start_as_current_span("llm_call") as span:
                span.set_attribute("model", model)
                span.set_attribute("prompt_tokens", prompt_tokens)
                span.set_attribute("completion_tokens", completion_tokens)
                span.set_attribute("latency_ms", latency_ms)
                if error:
                    span.set_attribute("error", error)
                    span.set_status(trace_status_error(error))
                for k, v in TraceContext.current_attributes().items():
                    if v is not None:
                        span.set_attribute(k, v)
        except Exception:
            pass  # best-effort

    def record_tool_call(self, *, tool_name: str, args_hash: str,
                         duration_ms: float, success: bool,
                         error: str | None = None) -> None:
        if not self._available or self._tracer is None:
            return
        try:
            with self._tracer.start_as_current_span("tool_call") as span:
                span.set_attribute("tool_name", tool_name)
                span.set_attribute("args_hash", args_hash)
                span.set_attribute("duration_ms", duration_ms)
                span.set_attribute("success", success)
                if error:
                    span.set_attribute("error", error)
                    span.set_status(trace_status_error(error))
                for k, v in TraceContext.current_attributes().items():
                    if v is not None:
                        span.set_attribute(k, v)
        except Exception:
            pass  # best-effort


def trace_status_error(description: str):
    """Helper to create an OTEL error status without importing at module level."""
    try:
        from opentelemetry.trace import Status, StatusCode
        return Status(StatusCode.ERROR, description=description)
    except Exception:
        return None
```

- [ ] **Step 4: Run test to verify it passes**

Run:
```bash
cd /root/zhanlu/backend && DATABASE_URL="sqlite:///./test_runtime.db" python3 -m pytest tests/test_tracing.py -v
```
Expected: PASS (all 15 tests)

Note: If `opentelemetry` is not installed on the dev box, the `test_otel_tracer_initializes_when_sdk_available` test should be skipped. Add this skip decorator if needed:

```python
try:
    import opentelemetry  # noqa: F401
    _OTEL_AVAILABLE = True
except ImportError:
    _OTEL_AVAILABLE = False

import pytest

@pytest.mark.skipif(not _OTEL_AVAILABLE, reason="opentelemetry not installed")
def test_otel_tracer_initializes_when_sdk_available():
    ...
```

- [ ] **Step 5: Commit**

```bash
cd /root/zhanlu/backend && git add app/services/tracing/otel.py tests/test_tracing.py && git commit -m "feat: add OtelTracer with lazy import and graceful fallback (A1 MVP)"
```

---

## Task 5: A1 Tracing — Instrument llm_service + agent_tools

**Files:**
- Modify: `backend/app/services/llm_service.py` (add tracing to `call_llm`)
- Modify: `backend/app/services/agent_tools.py` (add tracing to `execute_tool_with_retry`)
- Test: `backend/tests/test_tracing.py` (append integration tests)

**Interfaces:**
- Consumes: `get_tracer()`, `TraceContext` from Task 3-4
- Produces: instrumented `call_llm` and `execute_tool_with_retry` that emit spans

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/test_tracing.py`:

```python
# ── Integration: instrumentation in llm_service + agent_tools ────────

def test_call_llm_records_span(monkeypatch):
    """call_llm should call tracer.record_llm_call on success."""
    from app.services.tracing import reset_tracer
    reset_tracer()

    call_log = []

    class RecordingTracer(NoOpTracer):
        def record_llm_call(self, **kwargs):
            call_log.append(kwargs)

    import app.services.tracing as tracing_mod
    monkeypatch.setattr(tracing_mod, "_tracer", RecordingTracer())

    # Mock the actual LLM HTTP call
    from app.services import llm_service
    monkeypatch.setattr(llm_service, "build_llm_payload",
                        lambda body, model: (body, [], False))
    monkeypatch.setattr(llm_service, "get_llm_providers",
                        lambda: [_MockProvider()])

    import asyncio
    result = asyncio.run(llm_service.call_llm(prompt="hello"))
    assert len(call_log) == 1
    assert call_log[0]["model"] == "test-model"
    assert call_log[0]["error"] is None
    reset_tracer()


def test_call_llm_records_error_on_failure(monkeypatch):
    """call_llm should record error when the LLM call fails."""
    from app.services.tracing import reset_tracer
    reset_tracer()

    call_log = []

    class RecordingTracer(NoOpTracer):
        def record_llm_call(self, **kwargs):
            call_log.append(kwargs)

    import app.services.tracing as tracing_mod
    monkeypatch.setattr(tracing_mod, "_tracer", RecordingTracer())

    from app.services import llm_service
    monkeypatch.setattr(llm_service, "build_llm_payload",
                        lambda body, model: (body, [], False))
    monkeypatch.setattr(llm_service, "get_llm_providers",
                        lambda: [_FailingProvider()])

    import asyncio
    try:
        asyncio.run(llm_service.call_llm(prompt="hello"))
    except Exception:
        pass
    assert len(call_log) >= 1
    assert call_log[-1]["error"] is not None
    reset_tracer()


def test_execute_tool_records_span(monkeypatch):
    """execute_tool_with_retry should call tracer.record_tool_call."""
    from app.services.tracing import reset_tracer
    reset_tracer()

    call_log = []

    class RecordingTracer(NoOpTracer):
        def record_tool_call(self, **kwargs):
            call_log.append(kwargs)

    import app.services.tracing as tracing_mod
    monkeypatch.setattr(tracing_mod, "_tracer", RecordingTracer())

    from app.services import agent_tools
    monkeypatch.setattr(agent_tools, "execute_tool",
                        _mock_execute_tool_success)

    import asyncio
    from app.services.agent_tools import execute_tool_with_retry
    result = asyncio.run(execute_tool_with_retry("read_file", {"path": "/tmp/x"}, db=None))
    assert result["success"] is True
    assert len(call_log) == 1
    assert call_log[0]["tool_name"] == "read_file"
    assert call_log[0]["success"] is True
    reset_tracer()


# ── Test helpers ─────────────────────────────────────────────────────

class _MockProvider:
    model = "test-model"
    base_url = "http://localhost"
    api_key = "test-key"
    name = "test"

    def make_request(self, payload):
        return {"choices": [{"message": {"content": "hello"}}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 3}}


class _FailingProvider:
    model = "test-model"
    base_url = "http://localhost"
    api_key = "test-key"
    name = "test-fail"

    def make_request(self, payload):
        raise ConnectionError("LLM unreachable")


async def _mock_execute_tool_success(name, args, db, user_id=None, context=None):
    return {"success": True, "result": "ok"}
```

- [ ] **Step 2: Run test to verify it fails**

Run:
```bash
cd /root/zhanlu/backend && DATABASE_URL="sqlite:///./test_runtime.db" python3 -m pytest tests/test_tracing.py -v -k "call_llm_records or execute_tool_records"
```
Expected: FAIL (tracer not yet called)

- [ ] **Step 3: Write minimal implementation**

Modify `backend/app/services/llm_service.py`. Find the `call_llm` function (around line 161). Add tracing import at the top of the file (after existing imports) and wrap the LLM call. The key change is to wrap the provider loop with timing + record_llm_call:

At the top of `llm_service.py`, add:
```python
import time as _time

from app.services.tracing import get_tracer as _get_tracer
```

Inside `call_llm`, wrap the provider loop. Find the `for provider in providers:` block and add timing + tracing. After the loop (before the final return or error), add:

```python
    tracer = _get_tracer()
    _llm_start = _time.monotonic()
    _llm_error: str | None = None
    _llm_model = get_model()
    _llm_prompt_tokens = 0
    _llm_completion_tokens = 0
```

Then after the loop completes (whether success or all providers failed), before the return, add:

```python
    tracer.record_llm_call(
        model=_llm_model,
        prompt_tokens=_llm_prompt_tokens,
        completion_tokens=_llm_completion_tokens,
        latency_ms=(_time.monotonic() - _llm_start) * 1000,
        error=_llm_error,
    )
```

The exact placement requires reading the full function to find where `usage` is parsed and where the error is raised. The implementer should:
1. Set `_llm_model = provider.model` inside the try block when a provider succeeds.
2. Set `_llm_prompt_tokens = response.get("usage", {}).get("prompt_tokens", 0)` and `_llm_completion_tokens = response.get("usage", {}).get("completion_tokens", 0)` when usage is available.
3. Set `_llm_error = str(exc)` in the except block.
4. Call `tracer.record_llm_call(...)` once, after the loop, before returning.

Modify `backend/app/services/agent_tools.py`. Find `execute_tool_with_retry` (around line 220). Add tracing import at the top:

```python
import time as _time

from app.services.tracing import get_tracer as _get_tracer
```

Inside `execute_tool_with_retry`, after the function signature and docstring (around line 243), add timing + tracing. Wrap the initial `execute_tool` call and the retry loop. After the function computes its final result (before `return result`), add:

```python
    _get_tracer().record_tool_call(
        tool_name=tool_name,
        args_hash=hashlib.sha256(
            str(arguments).encode()
        ).hexdigest()[:16],
        duration_ms=(_time.monotonic() - _tool_start) * 1000,
        success=bool(result.get("success")),
        error=result.get("error") if not result.get("success") else None,
    )
```

Set `_tool_start = _time.monotonic()` at the top of the function body (after the `from app.config import settings` line). Add `import hashlib` to the imports if not already present.

- [ ] **Step 4: Run test to verify it passes**

Run:
```bash
cd /root/zhanlu/backend && DATABASE_URL="sqlite:///./test_runtime.db" python3 -m pytest tests/test_tracing.py -v
```
Expected: PASS (all 18 tests)

- [ ] **Step 5: Commit**

```bash
cd /root/zhanlu/backend && git add app/services/llm_service.py app/services/agent_tools.py tests/test_tracing.py && git commit -m "feat: instrument llm_service and agent_tools with tracing (A1 MVP)"
```

---

## Task 6: B1 RBAC — Base ABC + RbacAuthorizationProvider + Built-in Roles

**Files:**
- Create: `backend/app/services/authz/base.py`
- Create: `backend/app/services/authz/rbac.py`
- Create: `backend/app/services/authz/__init__.py`
- Modify: `backend/app/config.py` (add `AUTHZ_PROVIDER`)
- Test: `backend/tests/test_authz.py`

**Interfaces:**
- Consumes: `TraceContext.current_role()` from Task 3
- Produces: `AuthorizationProvider` (ABC), `ResourceType`, `AuthorizationDecision`, `RbacAuthorizationProvider`, `get_authorizer()`, `PermissiveAuthorizer`

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_authz.py`:

```python
"""Tests for the RBAC authorization provider."""
from __future__ import annotations

import sys
from pathlib import Path

_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from app.services.authz import get_authorizer
from app.services.authz.base import AuthorizationDecision, ResourceType
from app.services.authz.rbac import RbacAuthorizationProvider, BUILTIN_ROLES


# ── RbacAuthorizationProvider unit tests ─────────────────────────────

def test_admin_role_allows_all_tools():
    authz = RbacAuthorizationProvider()
    decision = authz.can_access(
        role="admin", resource_type=ResourceType.TOOL, resource_id="execute_code"
    )
    assert decision.allowed is True


def test_user_role_allows_all_tools_by_default():
    """Default user role is permissive — matches current behavior."""
    authz = RbacAuthorizationProvider()
    decision = authz.can_access(
        role="user", resource_type=ResourceType.TOOL, resource_id="write_file"
    )
    assert decision.allowed is True


def test_viewer_role_denies_write_tools():
    authz = RbacAuthorizationProvider()
    decision = authz.can_access(
        role="viewer", resource_type=ResourceType.TOOL, resource_id="write_file"
    )
    assert decision.allowed is False
    assert "write_file" in decision.reason or "not in allowed" in decision.reason


def test_viewer_role_allows_read_tools():
    authz = RbacAuthorizationProvider()
    for tool in ["read_file", "web_search", "list_tools"]:
        decision = authz.can_access(
            role="viewer", resource_type=ResourceType.TOOL, resource_id=tool
        )
        assert decision.allowed is True, f"viewer should access {tool}"


def test_unknown_role_is_denied_fail_closed():
    authz = RbacAuthorizationProvider()
    decision = authz.can_access(
        role="unknown_role", resource_type=ResourceType.TOOL, resource_id="read_file"
    )
    assert decision.allowed is False
    assert "unknown role" in decision.reason.lower()


def test_filter_resources_removes_denied():
    authz = RbacAuthorizationProvider()
    tools = ["read_file", "write_file", "web_search", "execute_code"]
    allowed = authz.filter_resources(
        role="viewer", resource_type=ResourceType.TOOL, resource_ids=tools
    )
    assert "read_file" in allowed
    assert "web_search" in allowed
    assert "write_file" not in allowed
    assert "execute_code" not in allowed


def test_filter_resources_admin_keeps_all():
    authz = RbacAuthorizationProvider()
    tools = ["read_file", "write_file", "execute_code"]
    allowed = authz.filter_resources(
        role="admin", resource_type=ResourceType.TOOL, resource_ids=tools
    )
    assert set(allowed) == set(tools)


def test_deny_always_wins_over_allow():
    """If a resource is in both allow and deny, deny wins."""
    custom_roles = {
        "test_role": {
            "tools": {
                "allow": ["read_file", "write_file"],
                "deny": ["write_file"],
            }
        }
    }
    authz = RbacAuthorizationProvider(roles=custom_roles)
    decision = authz.can_access(
        role="test_role", resource_type=ResourceType.TOOL, resource_id="write_file"
    )
    assert decision.allowed is False


def test_policy_compilation_rejects_unknown_keys():
    """Unknown keys in a role policy should raise ValueError."""
    import pytest
    bad_roles = {
        "bad_role": {
            "tools": {"allow": ["read_file"]},
            "unknown_section": {"foo": "bar"},
        }
    }
    with pytest.raises(ValueError, match="unknown_section"):
        RbacAuthorizationProvider(roles=bad_roles)


def test_builtin_roles_exist():
    assert "admin" in BUILTIN_ROLES
    assert "user" in BUILTIN_ROLES
    assert "viewer" in BUILTIN_ROLES


def test_permissive_authorizer_allows_everything():
    from app.services.authz.rbac import PermissiveAuthorizer
    authz = PermissiveAuthorizer()
    decision = authz.can_access(
        role="anything", resource_type=ResourceType.TOOL, resource_id="anything"
    )
    assert decision.allowed is True


# ── get_authorizer singleton ─────────────────────────────────────────

def test_get_authorizer_returns_rbac_by_default():
    authz = get_authorizer()
    assert isinstance(authz, RbacAuthorizationProvider)
```

- [ ] **Step 2: Run test to verify it fails**

Run:
```bash
cd /root/zhanlu/backend && DATABASE_URL="sqlite:///./test_runtime.db" python3 -m pytest tests/test_authz.py -v
```
Expected: FAIL with `ModuleNotFoundError: No module named 'app.services.authz'`

- [ ] **Step 3: Write minimal implementation**

Create `backend/app/services/authz/base.py`:

```python
"""Authorization provider ABC + core types."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum


class ResourceType(Enum):
    """Types of resources that RBAC can govern."""
    TOOL = "tool"
    MODEL = "model"          # defined now, wired in future work
    SKILL = "skill"          # defined now, wired in future work
    SANDBOX = "sandbox"      # defined now, wired in future work
    MCP_SERVER = "mcp_server"  # defined now, wired in future work
    ROUTE = "route"          # defined now, wired in future work


@dataclass
class AuthorizationDecision:
    """Result of an authorization check."""
    allowed: bool
    reason: str = ""
    denied_resources: list[str] = field(default_factory=list)


class AuthorizationProvider(ABC):
    """Abstract base for authorization providers.

    Implementations must be fail-closed: unknown roles or missing policies
    should deny, not allow.
    """

    @abstractmethod
    def can_access(
        self,
        *,
        role: str,
        resource_type: ResourceType,
        resource_id: str,
        action: str = "use",
    ) -> AuthorizationDecision: ...

    @abstractmethod
    def filter_resources(
        self,
        *,
        role: str,
        resource_type: ResourceType,
        resource_ids: list[str],
    ) -> list[str]: ...
```

Create `backend/app/services/authz/rbac.py`:

```python
"""RBAC authorization provider — deny-always-wins, fail-closed."""
from __future__ import annotations

import logging
from typing import Any

from app.services.authz.base import AuthorizationDecision, AuthorizationProvider, ResourceType

logger = logging.getLogger(__name__)

# Viewer role: read-only tools
_VIEWER_ALLOWED_TOOLS: list[str] = [
    "read_file",
    "web_search",
    "web_extract",
    "list_tools",
    "list_knowledge_bases",
    "list_market_agents",
]

BUILTIN_ROLES: dict[str, dict[str, Any]] = {
    "admin": {
        # Allow all — no denies, no explicit allow list (None = wildcard)
        "tools": {"allow": None, "deny": []},
    },
    "user": {
        # Permissive by default — matches current behavior.
        # Value is in customizability: operators can deny specific tools.
        "tools": {"allow": None, "deny": []},
    },
    "viewer": {
        "tools": {"allow": _VIEWER_ALLOWED_TOOLS, "deny": []},
    },
}

_VALID_RESOURCE_KEYS = {"tools", "models", "skills", "sandbox", "mcp_servers", "routes"}
_VALID_POLICY_KEYS = {"allow", "deny"}


class RbacAuthorizationProvider(AuthorizationProvider):
    """Role-based authorization with deny-always-wins and fail-closed semantics.

    Policies are compiled at construction. Unknown keys → ValueError (no
    silent mis-grants). Unknown role → denied (fail-closed).
    """

    def __init__(self, roles: dict[str, dict[str, Any]] | None = None):
        self._roles = roles if roles is not None else BUILTIN_ROLES
        self._compiled = self._compile(self._roles)

    def _compile(
        self, roles: dict[str, dict[str, Any]]
    ) -> dict[str, dict[ResourceType, tuple[set[str] | None, set[str]]]]:
        """Compile role policies into fast lookup structures.

        Returns: {role: {resource_type: (allow_set_or_None, deny_set)}}
        """
        compiled: dict[str, dict[ResourceType, tuple[set[str] | None, set[str]]]] = {}
        for role_name, policy in roles.items():
            compiled[role_name] = {}
            for key, section in policy.items():
                if key not in _VALID_RESOURCE_KEYS:
                    raise ValueError(
                        f"Unknown policy section '{key}' in role '{role_name}'. "
                        f"Valid sections: {_VALID_RESOURCE_KEYS}"
                    )
                # Map section name to ResourceType
                rt = _SECTION_TO_RESOURCE.get(key)
                if rt is None:
                    continue
                if not isinstance(section, dict):
                    raise ValueError(
                        f"Section '{key}' in role '{role_name}' must be a dict"
                    )
                for sub_key in section:
                    if sub_key not in _VALID_POLICY_KEYS:
                        raise ValueError(
                            f"Unknown policy key '{sub_key}' in role '{role_name}'."
                            f"{key}. Valid keys: {_VALID_POLICY_KEYS}"
                        )
                allow = section.get("allow")
                deny = section.get("deny", [])
                allow_set = set(allow) if allow is not None else None
                deny_set = set(deny) if deny else set()
                compiled[role_name][rt] = (allow_set, deny_set)
        return compiled

    def can_access(
        self,
        *,
        role: str,
        resource_type: ResourceType,
        resource_id: str,
        action: str = "use",
    ) -> AuthorizationDecision:
        role_policy = self._compiled.get(role)
        if role_policy is None:
            return AuthorizationDecision(
                allowed=False,
                reason=f"Access denied: unknown role '{role}' (fail-closed)",
            )

        allow_set, deny_set = role_policy.get(resource_type, (None, set()))

        # Deny-always-wins
        if resource_id in deny_set:
            return AuthorizationDecision(
                allowed=False,
                reason=f"Access denied: '{resource_id}' is denied for role '{role}'",
                denied_resources=[resource_id],
            )

        # If allow list exists, resource must be in it
        if allow_set is not None and resource_id not in allow_set:
            return AuthorizationDecision(
                allowed=False,
                reason=f"Access denied: '{resource_id}' is not in the allowed list for role '{role}'",
                denied_resources=[resource_id],
            )

        return AuthorizationDecision(allowed=True)

    def filter_resources(
        self,
        *,
        role: str,
        resource_type: ResourceType,
        resource_ids: list[str],
    ) -> list[str]:
        return [
            rid for rid in resource_ids
            if self.can_access(
                role=role, resource_type=resource_type, resource_id=rid
            ).allowed
        ]


class PermissiveAuthorizer(AuthorizationProvider):
    """Authorizer that allows everything — for testing/legacy mode."""

    def can_access(self, *, role, resource_type, resource_id, action="use"):
        return AuthorizationDecision(allowed=True)

    def filter_resources(self, *, role, resource_type, resource_ids):
        return list(resource_ids)


# Section name → ResourceType mapping
_SECTION_TO_RESOURCE: dict[str, ResourceType] = {
    "tools": ResourceType.TOOL,
    "models": ResourceType.MODEL,
    "skills": ResourceType.SKILL,
    "sandbox": ResourceType.SANDBOX,
    "mcp_servers": ResourceType.MCP_SERVER,
    "routes": ResourceType.ROUTE,
}
```

Create `backend/app/services/authz/__init__.py`:

```python
"""Authorization package — pluggable RBAC for role-based resource access.

Default provider is RbacAuthorizationProvider with built-in roles (admin/user/viewer).
The default user role is permissive (all tools allowed) to match current behavior.
Set AUTHZ_PROVIDER="none" for PermissiveAuthorizer (testing/legacy).
"""
from __future__ import annotations

from app.services.authz.base import AuthorizationDecision, AuthorizationProvider, ResourceType

_authorizer: AuthorizationProvider | None = None


def get_authorizer() -> AuthorizationProvider:
    """Get the singleton AuthorizationProvider instance."""
    global _authorizer
    if _authorizer is not None:
        return _authorizer

    from app.config import settings
    provider = getattr(settings, "AUTHZ_PROVIDER", "rbac")

    if provider == "none":
        from app.services.authz.rbac import PermissiveAuthorizer
        _authorizer = PermissiveAuthorizer()
    else:
        from app.services.authz.rbac import RbacAuthorizationProvider
        _authorizer = RbacAuthorizationProvider()

    return _authorizer


def reset_authorizer() -> None:
    """Reset the singleton (for testing)."""
    global _authorizer
    _authorizer = None


__all__ = [
    "AuthorizationProvider",
    "AuthorizationDecision",
    "ResourceType",
    "get_authorizer",
    "reset_authorizer",
]
```

Add config field to `backend/app/config.py` (after the tracing config added in Task 3):

```python
    # ── B1: RBAC authorization ─────────────────────────────────────────
    AUTHZ_PROVIDER: str = "rbac"              # "rbac" | "none"
```

- [ ] **Step 4: Run test to verify it passes**

Run:
```bash
cd /root/zhanlu/backend && DATABASE_URL="sqlite:///./test_runtime.db" python3 -m pytest tests/test_authz.py -v
```
Expected: PASS (12 tests)

- [ ] **Step 5: Commit**

```bash
cd /root/zhanlu/backend && git add app/services/authz/ app/config.py tests/test_authz.py && git commit -m "feat: add RBAC authorization provider with built-in roles (B1 MVP)"
```

---

## Task 7: B1 RBAC — Integrate into tool_registry.get_schemas

**Files:**
- Modify: `backend/app/services/tool_registry.py` (add RBAC filtering to `get_schemas`)
- Test: `backend/tests/test_authz.py` (append integration tests)

**Interfaces:**
- Consumes: `get_authorizer()`, `TraceContext.current_role()` from Task 6
- Produces: `get_schemas()` filters denied tools before returning schemas to the LLM

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/test_authz.py`:

```python
# ── Integration: tool_registry RBAC filtering ────────────────────────

from app.services.tracing import TraceContext


def test_get_schemas_filters_denied_tools_for_viewer():
    """get_schemas should strip denied tools for the viewer role."""
    from app.services.tool_registry import ToolRegistry

    registry = ToolRegistry.get_instance()

    # Register a dummy tool
    from app.services.tool_registry import ToolEntry
    registry._tools["_test_rbac_tool"] = ToolEntry(
        name="_test_rbac_tool",
        schema={"type": "function", "function": {"name": "_test_rbac_tool", "description": "test"}},
        handler=lambda *a, **kw: {},
        category="test",
    )
    try:
        TraceContext.clear()
        TraceContext.set(role="viewer")

        schemas = registry.get_schemas(["_test_rbac_tool", "read_file"])
        names = [s["function"]["name"] for s in schemas]
        # _test_rbac_tool is not in viewer's allow list → filtered out
        assert "_test_rbac_tool" not in names
    finally:
        del registry._tools["_test_rbac_tool"]
        TraceContext.clear()


def test_get_schemas_keeps_all_tools_for_admin():
    from app.services.tool_registry import ToolRegistry, ToolEntry

    registry = ToolRegistry.get_instance()
    registry._tools["_test_rbac_admin"] = ToolEntry(
        name="_test_rbac_admin",
        schema={"type": "function", "function": {"name": "_test_rbac_admin", "description": "test"}},
        handler=lambda *a, **kw: {},
        category="test",
    )
    try:
        TraceContext.clear()
        TraceContext.set(role="admin")

        schemas = registry.get_schemas(["_test_rbac_admin"])
        names = [s["function"]["name"] for s in schemas]
        assert "_test_rbac_admin" in names
    finally:
        del registry._tools["_test_rbac_admin"]
        TraceContext.clear()


def test_get_schemas_no_role_defaults_to_user():
    """When no role is set in TraceContext, default to 'user' (permissive)."""
    from app.services.tool_registry import ToolRegistry, ToolEntry

    registry = ToolRegistry.get_instance()
    registry._tools["_test_rbac_norole"] = ToolEntry(
        name="_test_rbac_norole",
        schema={"type": "function", "function": {"name": "_test_rbac_norole", "description": "test"}},
        handler=lambda *a, **kw: {},
        category="test",
    )
    try:
        TraceContext.clear()
        # No role set → should default to "user" → permissive → tool included

        schemas = registry.get_schemas(["_test_rbac_norole"])
        names = [s["function"]["name"] for s in schemas]
        assert "_test_rbac_norole" in names
    finally:
        del registry._tools["_test_rbac_norole"]
        TraceContext.clear()
```

- [ ] **Step 2: Run test to verify it fails**

Run:
```bash
cd /root/zhanlu/backend && DATABASE_URL="sqlite:///./test_runtime.db" python3 -m pytest tests/test_authz.py -v -k "get_schemas"
```
Expected: FAIL (RBAC filtering not yet implemented in get_schemas)

- [ ] **Step 3: Write minimal implementation**

Modify `backend/app/services/tool_registry.py`. In the `get_schemas` method (around line 149), add RBAC filtering at the top of the method, before the loop:

```python
    def get_schemas(self, tool_names: list[str], include_unavailable: bool = True) -> list[dict]:
        """Return OpenAI-format tool schemas for the given tool names.

        Skips tools that are not registered. When ``include_unavailable`` is
        True (default), tools whose ``check_fn()`` returns False are still
        returned. Tools denied by RBAC are filtered out before this check.
        """
        # B1: RBAC filtering — strip denied tools before they reach the model
        try:
            from app.services.authz import get_authorizer, ResourceType
            from app.services.authz.base import ResourceType as _RT
            from app.services.tracing import TraceContext

            role = TraceContext.current_role() or "user"
            authorizer = get_authorizer()
            tool_names = authorizer.filter_resources(
                role=role,
                resource_type=_RT.TOOL,
                resource_ids=tool_names,
            )
        except Exception as exc:
            logger.debug("RBAC filter skipped (non-fatal): %s", exc)

        # Per-call cache on top of the TTL cache — saves redundant probes.
        check_results: Dict[Callable, bool] = {}
        # ... rest of existing method unchanged ...
```

- [ ] **Step 4: Run test to verify it passes**

Run:
```bash
cd /root/zhanlu/backend && DATABASE_URL="sqlite:///./test_runtime.db" python3 -m pytest tests/test_authz.py -v
```
Expected: PASS (all 15 tests)

- [ ] **Step 5: Commit**

```bash
cd /root/zhanlu/backend && git add app/services/tool_registry.py tests/test_authz.py && git commit -m "feat: integrate RBAC tool filtering into tool_registry (B1 MVP)"
```

---

## Self-Review Checklist (completed)

**1. Spec coverage:**
- B3 SkillScan: 5 CRITICAL regex rules ✓ (Task 1), warn-only policy ✓ (Task 1-2), skills_loader integration ✓ (Task 2), scan_findings on metadata ✓ (Task 2), config SKILL_SCAN_ENABLED ✓ (Task 2)
- A1 Tracing: TracingProvider ABC ✓ (Task 3), NoOpTracer ✓ (Task 3), OtelTracer lazy+fallback ✓ (Task 4), TraceContext contextvars ✓ (Task 3), llm_service instrumentation ✓ (Task 5), agent_tools instrumentation ✓ (Task 5), config TRACING_PROVIDER/OTEL_* ✓ (Task 3)
- B1 RBAC: AuthorizationProvider ABC ✓ (Task 6), RbacAuthorizationProvider deny-always-wins ✓ (Task 6), fail-closed ✓ (Task 6), 3 built-in roles ✓ (Task 6), tool_registry filtering ✓ (Task 7), role from TraceContext ✓ (Task 7), config AUTHZ_PROVIDER ✓ (Task 6)
- Deferred items: all listed in spec's out-of-scope section, none have tasks ✓

**2. Placeholder scan:** No TBDs, TODOs, or "implement later". All steps have actual code. ✓

**3. Type consistency:**
- `scan_skill(metadata) -> ScanResult` — consistent across Task 1 (produces) and Task 2 (consumes) ✓
- `get_tracer() -> TracingProvider` — consistent across Task 3 (produces) and Task 5 (consumes) ✓
- `TraceContext.current_role() -> str | None` — consistent across Task 3 (produces) and Task 7 (consumes) ✓
- `get_authorizer() -> AuthorizationProvider` — consistent across Task 6 (produces) and Task 7 (consumes) ✓
- `filter_resources(role=, resource_type=, resource_ids=) -> list[str]` — consistent across Task 6 (produces) and Task 7 (consumes) ✓
- `ResourceType.TOOL` — consistent across Task 6 and Task 7 ✓

**4. Note on Task 5 (llm_service instrumentation):** The exact line numbers for wrapping `call_llm` depend on reading the full function body. The plan describes the approach (timing + record_llm_call) and the implementer reads the function to place the instrumentation correctly. This is intentional — the function is 40+ lines and the implementer needs to see where `usage` is parsed and where errors are raised.
