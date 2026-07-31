"""Application configuration — reads from environment variables via Pydantic Settings.

NOTE on DATABASE_URL: this project intentionally does NOT provide a silent
default for ``DATABASE_URL``. If the variable is missing, the app refuses to
start with a clear error. This prevents a misconfigured environment from
silently landing on a leftover SQLite file (which is empty and would appear
"working" while serving zero data) instead of the real Postgres the stack
depends on. See ``backend/scripts/check_db.py`` and ``GET /api/_db-info`` for
diagnostic tools that always report which database is actually in use.
"""

import os
import re
from pathlib import Path
from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Database — REQUIRED. Set this in .env (or the environment) to the SQLAlchemy
    # URL for the target database, e.g.:
    #   PostgreSQL:  postgresql+psycopg2://zhanlu:zhanlu123@postgres:5432/zhanlu
    #   SQLite:      sqlite:///./zhanlu.db
    # We intentionally do NOT provide a default value here: a silent default to
    # SQLite has caused real confusion (e.g. an empty leftover .db file looked
    # like "the database" while the real data was in Postgres).
    DATABASE_URL: str = ""

    @field_validator("DATABASE_URL")
    @classmethod
    def _require_database_url(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError(
                "DATABASE_URL is required but is not set. "
                "Set it in backend/.env or your environment, e.g.\n"
                "  DATABASE_URL=postgresql+psycopg2://zhanlu:zhanlu123@postgres:5432/zhanlu\n"
                "  DATABASE_URL=sqlite:///./zhanlu.db\n"
                "See backend/.env.example for the full list of options."
            )
        if not re.match(r"^(sqlite|postgresql|postgres|mysql)", v):
            raise ValueError(
                f"DATABASE_URL must start with a known driver prefix "
                f"(sqlite://, postgresql+psycopg2://, mysql+pymysql://, ...). Got: {v[:40]!r}"
            )
        return v

    # Auth
    JWT_SECRET: str = ""
    JWT_ALGORITHM: str = "HS256"
    JWT_EXPIRE_HOURS: int = 24

    @field_validator("JWT_SECRET")
    @classmethod
    def _require_jwt_secret(cls, v: str) -> str:
        """Reject the publicly-known default; generate a random secret if unset.

        A hardcoded default secret lets anyone forge JWTs. If JWT_SECRET is
        not set in the environment, we generate a random one for this session
        (tokens won't survive a restart, which is acceptable for dev) and log
        a loud warning. Production deployments MUST set JWT_SECRET explicitly.
        """
        v = (v or "").strip()
        if not v:
            import secrets as _secrets
            import logging as _logging
            v = _secrets.token_urlsafe(48)
            _logging.getLogger(__name__).warning(
                "JWT_SECRET is not set — generated a random secret for this "
                "session. Tokens will NOT survive a restart. Set JWT_SECRET "
                "in your environment for production use."
            )
        return v

    # Auth — token lifecycle & policy (plan 2026-07-27)
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 15
    REFRESH_TOKEN_EXPIRE_DAYS: int = 30
    PASSWORD_MIN_LENGTH: int = 10
    PASSWORD_REQUIRE_LETTER: bool = True
    PASSWORD_REQUIRE_DIGIT: bool = True

    # Rate limits (in-memory, per IP). Zero = disabled.
    RATE_LIMIT_LOGIN_PER_MIN: int = 5
    RATE_LIMIT_REGISTER_PER_10MIN: int = 3
    RATE_LIMIT_OTP_PER_10MIN: int = 3
    RATE_LIMIT_RESET_PER_10MIN: int = 3

    # App
    APP_ID: str = "local-zhanlu-app"
    FRONTEND_URL: str = "http://localhost:5157"
    BACKEND_PORT: int = 5002
    # Comma-separated list of additional allowed CORS origins (e.g. a production
    # frontend domain). Merged with FRONTEND_URL and the localhost dev origins.
    # Example: CORS_ORIGINS="https://app.zhanlu.ai,https://admin.zhanlu.ai"
    CORS_ORIGINS: str = ""

    # ── Feishu (Lark) QR-code login ────────────────────────────────────────
    # Register a self-built app (自建应用) on the Feishu Open Platform
    # (https://open.feishu.cn/ — or https://open.larksuite.com for the
    # international Lark client) to get these. Leave APP_ID empty to disable
    # the "Continue with Feishu" login button.
    #   FEISHU_APP_ID       — from the app's 凭证与基础信息 page
    #   FEISHU_APP_SECRET   — from the same page (keep it secret)
    #   FEISHU_REDIRECT_URI — the callback you must also whitelist in the
    #                         app's 安全设置 → 重定向URL. Must be the ABSOLUTE
    #                         public URL, e.g.
    #                         https://zhanlu.ai:8443/api/apps/local-zhanlu-app/auth/feishu/callback
    #   FEISHU_API_BASE     — open.feishu.cn (China) or open.larksuite.com (global)
    FEISHU_APP_ID: str = ""
    FEISHU_APP_SECRET: str = ""
    FEISHU_REDIRECT_URI: str = ""
    FEISHU_API_BASE: str = "https://open.feishu.cn"

    # ── SMTP — real email delivery (OTP codes, password resets) ────────────
    # When SMTP_HOST is empty the backend FALLS BACK to logging the OTP to the
    # server console (dev behaviour). Fill these in to actually email users.
    # Common China-friendly providers (port 465 = SSL):
    #   QQ Mail    smtp.qq.com    (enable SMTP in QQ Mail settings → 授权码)
    #   163 Mail   smtp.163.com   (enable SMTP → 授权码)
    #   Aliyun     smtp.aliyun.com
    #   Gmail      smtp.gmail.com (app password; may be blocked in China)
    # Use port 587 with SMTP_USE_SSL=false for STARTTLS providers.
    SMTP_HOST: str = ""
    SMTP_PORT: int = 465
    SMTP_USER: str = ""
    SMTP_PASSWORD: str = ""
    SMTP_FROM: str = ""  # default = SMTP_USER
    SMTP_USE_SSL: bool = True  # True = SSL (465); False = STARTTLS (587)
    SMTP_FROM_NAME: str = "Zhanlu System"

    # Public-facing URL — used to build absolute URLs for third-party
    # previewers like Microsoft Word Online.  Empty disables those tiers.
    APP_PUBLIC_URL: str = ""

    # File Uploads
    UPLOAD_DIR: str = "uploads"

    # Generated deliverables (private — served only via authenticated routes,
    # never via the public /api/uploads static mount)
    GENERATED_DIR: str = "data/generated"

    # LLM
    OPENAI_API_KEY: str = ""
    OPENAI_BASE_URL: str = "https://api.openai.com/v1"
    LLM_MODEL: str = "gpt-4o-mini"

    # LLM provider failover (Phase 3). A JSON array of fallback providers,
    # each {"name", "base_url", "api_key", "model"}. When the primary
    # provider (OPENAI_BASE_URL/OPENAI_API_KEY/LLM_MODEL) fails with an
    # HTTPStatusError or RequestError, call_llm / chat_completion_json_sync /
    # stream_chat_completion retry the next provider in order. Empty string
    # (default) = no fallbacks, behavior identical to pre-fallback.
    # Example: '[{"name":"backup","base_url":"https://api.deepseek.com/v1","api_key":"sk-...","model":"deepseek-chat"}]'
    LLM_FALLBACK_PROVIDERS: str = ""

    # Embeddings — semantic memory recall (P0). text-embedding-3-small is
    # OpenAI-compatible; providers without an /embeddings endpoint will
    # cause get_embedding() to return None and memory search transparently
    # falls back to lexical token-overlap scoring. No hard dependency.
    EMBEDDING_MODEL: str = "text-embedding-3-small"
    # When True, save_memory() computes + persists embeddings (one network
    # call per new memory). Disable to skip embedding work entirely.
    MEMORY_EMBEDDINGS_ENABLED: bool = True

    # Web Search — provider is pluggable via SEARCH_PROVIDER env var.
    # Supported: "tavily", "serper", "bocha", "duckduckgo", "bing"
    SEARCH_PROVIDER: str = "duckduckgo"
    SEARCH_API_KEY: str = ""  # Not needed for duckduckgo / bing; required for tavily / serper / bocha

    # Image Generation — provider selected via IMAGE_API_PROVIDER
    # Supported: "openai" (reuses OPENAI_API_KEY), "fal"
    IMAGE_API_PROVIDER: str = "openai"
    IMAGE_API_KEY: str = ""  # FAL.ai key (only if IMAGE_API_PROVIDER="fal")
    IMAGE_MODEL: str = "dall-e-3"

    # Code Execution Sandbox
    CODE_EXECUTION_TIMEOUT: int = 30  # seconds
    CODE_EXECUTION_MAX_MEMORY: int = 128  # MB (informational; subprocess enforcement)

    # Agent Workspace — sandboxed directory for read_file/write_file operations
    AGENT_WORKSPACE_DIR: str = "agent_workspace"

    # Delegate Task
    DELEGATE_MAX_ITERATIONS: int = 5
    DELEGATE_TIMEOUT: int = 120  # seconds per sub-conversation

    # Per-conversation total iteration budget across all turns (including
    # resumes). Bounds runaway multi-turn sessions. Per-agent overrides via
    # AgentApp.max_call_count take precedence when set.
    AGENT_MAX_ITERATIONS: int = 50

    # Tool output limits
    TOOL_MAX_OUTPUT_CHARS: int = 8000

    # Tool result persistence (P0) -- Layer 2/3 of context overflow protection
    TOOL_RESULT_STORAGE_DIR: str = "tool_results"

    # P3: Prompt caching -- apply explicit cache_control markers for providers
    # that support them (Anthropic, OpenRouter). DeepSeek caches automatically.
    PROMPT_CACHE_ENABLED: bool = False
    PROMPT_CACHE_TTL: str = "5m"  # "5m" or "1h"

    # P8: Pluggable context engine -- "default" uses the built-in 4-layer
    # compaction. Custom engines can be registered via register_engine().
    CONTEXT_ENGINE: str = "default"

    # P8: Default toolset posture -- "coding", "research", or "safe".
    # Used when an agent has no explicit tool_config.
    DEFAULT_TOOLSET_POSTURE: str = "coding"

    # Compaction settings (P0)
    COMPACTION_AUTO_THRESHOLD: float = 0.8  # context window usage threshold
    COMPACTION_MICROCOMPACT_THRESHOLD: int = 30000  # tokens to trigger microcompact
    COMPACTION_FULL_THRESHOLD: int = 50000  # tokens to trigger full compact
    COMPACTION_MAX_CONTEXT_MESSAGES: int = 100
    COMPACTION_PRESERVE_RECENT: int = 6

    # Provider Profile (P3)
    ACTIVE_PROVIDER: str = ""  # empty = use OPENAI_* env vars

    # Sandbox (P3)
    SANDBOX_BACKEND: str = ""  # auto-detect if empty
    SANDBOX_WORKSPACE: str = "agent_workspace"

    # Channels (P3)
    CHANNELS_ENABLED: bool = False  # disabled by default

    # ohmo workspace (P3)
    OHMO_WORKSPACE_DIR: str = "ohmo"

    # --- Enterprise architecture (Layer 1/2/5/6) ---
    # Redis — job queues, locks, event fanout (empty = disabled in local dev)
    REDIS_URL: str = ""

    # MinIO / S3 — object storage for large artifact blobs
    # When ARTIFACT_STORAGE_BACKEND="postgres_bytea" (default), MinIO is unused.
    MINIO_ENDPOINT: str = ""
    MINIO_ACCESS_KEY: str = ""
    MINIO_SECRET_KEY: str = ""
    MINIO_BUCKET: str = "zhanlu-artifacts"
    ARTIFACT_STORAGE_BACKEND: str = "postgres_bytea"

    # Synexia FSM feature flag (Phase 5) — when True, add_message routes
    # through the FSM cognitive core instead of the raw tool loop.
    # Default ON since the v3 SSE path now streams fsm_state events
    # (see _emit_fsm_state in app/routers/agents.py and the FSM-in-SSE
    # work in add_message_stream).
    # Rollback path: set SYNEXIA_FSM_ENABLED=false in the environment to
    # revert to the raw tool loop without rebuilding.
    SYNEXIA_FSM_ENABLED: bool = True

    # VERIFY sub-agent flag — when True, the FSM VERIFY state augments the
    # deterministic validator with an LLM rubric pass. Off by default
    # (LLM call cost + extra latency). Deterministic checks are always on.
    SYNEXIA_VERIFIER_LLM_ENABLED: bool = False

    # ── Dynamic re-planning & self-healing (Phase 2 — agent roadmap) ──────
    # ACT re-plan: when a tool/skill plan node fails during execution, the
    # capability router generates a corrective sub-plan and re-executes it.
    # SYNEXIA_ACT_REPLAN_MAX caps the recursion depth (0 = disable).
    # Only tool/skill nodes are re-planned — the data pipeline (nl2sql /
    # synthesize / sandbox) is intentionally left untouched.
    SYNEXIA_ACT_REPLAN_MAX: int = 2
    # VERIFY re-plan: when a CRITICAL deterministic check fails after
    # execution AND the failure is attributable to a tool/skill node, the
    # FSM loops back to PLAN with the verification failures as context.
    # 0 = disable (VERIFY stays non-fatal — the historical behaviour).
    SYNEXIA_VERIFY_REPLAN_MAX: int = 1
    # Tool argument self-heal: when a tool fails with a PERMANENT error
    # (not a transient infra failure), prompt the LLM to suggest corrected
    # arguments and retry the handler once. False = surface the failure to
    # the model immediately (the previous behaviour).
    TOOL_ARG_REFORMULATION_ENABLED: bool = True

    # ── Result-level tool retry (Phase B — reliability spec) ────────────
    # Handler-level retry (tool_retry.retry_with_backoff) only fires on
    # raised EXCEPTIONS. Most handlers instead RETURN failure dicts
    # ({"success": False, "error": ...}); those are retried by
    # execute_tool_with_retry using these knobs.
    # TOOL_RETRY_MAX_ATTEMPTS = number of RETRIES after the initial call
    # (total calls = 1 + TOOL_RETRY_MAX_ATTEMPTS).
    TOOL_RETRY_MAX_ATTEMPTS: int = 2
    TOOL_RETRY_BASE_DELAY: float = 0.5
    TOOL_RETRY_MAX_DELAY: float = 8.0
    # Post-exhaustion argument reformulation attempts at the result level
    # (0 = disable). One-shot by default to bound cost and latency.
    TOOL_REFORMULATE_MAX_ATTEMPTS: int = 1

    # ── Artifact quality gate (Phase B) ─────────────────────────────────
    # When the FSM ships artifacts, the deterministic confidence score must
    # meet this threshold or the artifacts are held back from the result
    # (they stay in the DB; the user is told the output was held and why).
    SYNEXIA_QUALITY_GATE_ENABLED: bool = True
    SYNEXIA_QUALITY_GATE_THRESHOLD: float = 0.4
    # Automation runs are unattended — a silently-wrong shipped report is
    # costly.  They use a stricter shipping threshold than interactive chat.
    SYNEXIA_QUALITY_GATE_THRESHOLD_AUTOMATION: float = 0.6

    # ── QUALITY_EVAL (Tier 2 — Approach C) ───────────────────────────────
    # Post-FINALIZE semantic quality layer: a combined completeness +
    # reflexion LLM critique on the generated response, with a bounded
    # corrective re-generation loop.  See
    # app.services.synexia.quality_eval.  Disable to skip the extra LLM
    # calls entirely (heuristic-only reflexion remains via _run_reflexion).
    SYNEXIA_QUALITY_EVAL_ENABLED: bool = True
    # Max corrective iterations.  Each iteration = 1 regenerate + 1 re-eval
    # LLM call.  0 extra calls on a clean accept; up to
    # 1 + max_iterations*2 calls on a bad output.
    SYNEXIA_QUALITY_EVAL_MAX_ITERATIONS: int = 2

    # Phase 1 response grounding: char cap for the findings block injected
    # into the FINALIZE response prompt (spec §3.3). Metadata is preserved
    # over data rows under the cap.
    SYNEXIA_GROUNDING_MAX_CHARS: int = 2000

    # Phase 3a: opt-in LLM-driven plan generation (default off — the curated
    # default plan is the proven path; the LLM planner is gated for staging
    # validation). Also fixes the prior un-awaited call_llm in generate_plan.
    SYNEXIA_LLM_PLANNER_ENABLED: bool = False

    # Phase 3c: adaptive mid-execution re-planning (default off). After each
    # checkpoint node (nl2sql/synthesize/sandbox), an LLM decides whether to
    # proceed / insert / modify / complete_early. Bounded by MAX_REVISIONS.
    SYNEXIA_ADAPTIVE_PLANNING_ENABLED: bool = False
    SYNEXIA_ADAPTIVE_MAX_REVISIONS: int = 2

    # Planning router mode — "heuristic" (regex, default, English-leaning),
    # "llm" (LLM classifier; heuristic stays as fallback), or "hybrid"
    # (heuristic first; LLM only when heuristic is in a gray band).
    PLANNING_ROUTER_MODE: str = "heuristic"

    # OHMO memory consolidation (P3) — when True, a post-turn hook extracts
    # durable user facts via an LLM classify-prompt and merges them into
    # the OHMO workspace's user.md (dedup + timestamp via append_user_fact).
    # Enabled by default now that semantic memory recall (embeddings) lands
    # the retrieval layer that makes those extracted facts actually
    # retrievable. Best-effort: any failure is swallowed and never breaks
    # the SSE chat path. Set OHMO_MEMORY_CONSOLIDATION_ENABLED=false to opt out.
    OHMO_MEMORY_CONSOLIDATION_ENABLED: bool = True

    # Default org/app IDs for single-tenant backward compatibility
    DEFAULT_ORG_ID: str = "default-org"
    DEFAULT_APP_ID: str = "default-app"

    # ── Forecasting feature flag (Section 6) — when True, tool registration
    # and nightly cron for the forecasting engine are active. Default OFF
    # to prevent impact on existing deployments until explicitly enabled.
    FORECASTING_ENABLED: bool = False

    # ── Sandbox (runbook §6) ────────────────────────────────────────────
    SANDBOX_ENABLED: bool = False

    # ── Automation reliability (Phase 1) ────────────────────────────────
    # Wall-clock deadline for a single automation run. Executions whose
    # timeout_at passes while still queued/running are reaped by the
    # dispatcher's janitor and marked failed (then retried if attempts
    # remain). Bounds hung LLM calls so a single stuck run can't occupy a
    # worker slot forever.
    AUTOMATION_RUN_TIMEOUT_SECONDS: int = 600
    # data_sync preflight: per-source connectivity probe budget (seconds).
    # Bounds driver defaults (pymysql etc. are 10s+) so an unreachable
    # source fails the run fast instead of hanging the worker.
    AUTOMATION_DS_PREFLIGHT_TIMEOUT_SECONDS: float = 8.0
    # How often (in dispatcher ticks) the janitor sweeps for zombie /
    # timed-out executions. TICK_INTERVAL=60s → default 5 = every 5 min.
    AUTOMATION_REAPER_INTERVAL_TICKS: int = 5
    # Misfire visibility: if a task fires more than this many seconds late,
    # the dispatcher logs a WARNING (currently silent in production).
    AUTOMATION_MISFIRE_WARN_SECONDS: int = 120

    # ── Automation cognitive core (Phase 5 — Manus parity) ──────────────
    # Force the SynexiaFSM planning pipeline (GOAL→CONTEXT→PLAN→GATE→ACT→
    # OBSERVE→VERIFY→FINALIZE) for every scheduled run, instead of relying
    # on the should_trigger_planning classifier (which can bypass planning
    # for "simple-looking" prompts). Manus always plans before acting on a
    # scheduled job. The plan nodes + FSM state transitions are surfaced
    # through the existing SSE activity feed. Disable to fall back to the
    # classifier-gated behaviour.
    AUTOMATION_FORCE_PLANNING: bool = True

    # Maximum number of automation executions allowed to run concurrently.
    # Each run is a long-lived agent turn (LLM + tool calls); without a cap
    # a burst of due tasks can OOM a RAM-limited box or rate-limit the LLM
    # provider. The dispatcher acquires an asyncio.Semaphore(N) around every
    # spawned executor (both scheduled fires and manual triggers).
    AUTOMATION_MAX_CONCURRENCY: int = 3
    SANDBOX_IMAGE_PYTHON: str = "zhanlu-sandbox-python:latest"
    SANDBOX_IMAGE_OFFICE: str = "zhanlu-sandbox-office:latest"
    SANDBOX_IMAGE_WEBAPP: str = "zhanlu-sandbox-webapp:latest"
    SANDBOX_DEFAULT_NETWORK: str = "none"
    SANDBOX_DEFAULT_TIMEOUT_SECONDS: int = 120
    SANDBOX_DEFAULT_MEMORY_MB: int = 1024
    SANDBOX_DEFAULT_CPUS: float = 1.0
    SANDBOX_DEFAULT_PIDS_LIMIT: int = 128
    SANDBOX_ALLOW_DOCKER_SOCKET_ONLY_IN_WORKER: bool = True
    SANDBOX_TMP_ROOT: str = "/tmp/zhanlu_sandbox"

    # ── B3: Skill safety scanner ───────────────────────────────────────
    SKILL_SCAN_ENABLED: bool = True            # master switch (warn-only)

    # ── A1: Distributed tracing ─────────────────────────────────────────
    TRACING_PROVIDER: str = "noop"            # "noop" | "otel"
    OTEL_EXPORTER_OTLP_ENDPOINT: str = ""      # standard OTEL env var
    OTEL_SERVICE_NAME: str = "zhanlu-backend"
    AUTHZ_PROVIDER: str = "rbac"              # "rbac" | "none"

    @property
    def upload_path(self) -> Path:
        p = Path(self.UPLOAD_DIR)
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def generated_path(self) -> Path:
        p = Path(self.GENERATED_DIR)
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def is_sqlite(self) -> bool:
        return "sqlite" in self.DATABASE_URL

    @property
    def database_url_safe(self) -> str:
        """Return ``DATABASE_URL`` with the password masked.

        Safe to log. ``postgresql+psycopg2://user:secret@host/db`` →
        ``postgresql+psycopg2://user:***@host/db``.
        """
        return re.sub(r"://([^:]+):[^@]+@", r"://\1:***@", self.DATABASE_URL)

    @property
    def database_dialect(self) -> str:
        """Return just the SQLAlchemy dialect prefix (``postgresql+psycopg2``, ``sqlite``, …)."""
        return self.DATABASE_URL.split("://", 1)[0] if "://" in self.DATABASE_URL else "unknown"

    @property
    def workspace_path(self) -> Path:
        """Agent workspace directory for file operations."""
        p = Path(self.AGENT_WORKSPACE_DIR)
        p.mkdir(parents=True, exist_ok=True)
        return p

    def search_config_ok(self) -> bool:
        """Return True if the search provider has the config it needs."""
        if self.SEARCH_PROVIDER in ("duckduckgo", "bing"):
            return True
        return bool(self.SEARCH_API_KEY)

    def image_config_ok(self) -> bool:
        """Return True if image generation is configured."""
        if self.IMAGE_API_PROVIDER == "openai":
            return bool(self.OPENAI_API_KEY)
        return bool(self.IMAGE_API_KEY)


settings = Settings()
