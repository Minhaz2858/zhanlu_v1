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

    # ── External MySQL warehouse — optional read-only mirror (empty = off).
    # Empty = dashboard routes return 503 (the rest of zhanlu is unaffected).
    # Example: mysql+pymysql://user:password@10.10.10.49:3306/demo_warehouse_prod
    MYSQL_URL: str = ""

    @property
    def has_external_mysql(self) -> bool:
        """True when MYSQL_URL is set (the dashboard will at least try)."""
        return bool(self.MYSQL_URL)

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
    # When False, open self-registration (/auth/register) is disabled and
    # accounts can ONLY be created by an admin via /api/admin/users. A fresh
    # install with zero users can still bootstrap the first admin (see
    # routers/auth.py). Enterprise / SaaS provisioning model.
    ALLOW_PUBLIC_REGISTRATION: bool = False

    # Multi-tenant SaaS (2026-10-13): when True, POST .../auth/signup-org
    # lets a new customer self-serve create an Organization + its first
    # admin user. Default False keeps the enterprise behavior (accounts
    # provisioned by an admin only).
    ALLOW_ORG_SIGNUP: bool = False

    # ── Option C Phase 1: LLM turn router (2026-10-13) ─────────────────
    # off  = heuristic routing only (zero behavior change)
    # dark = LLM decision logged beside the heuristic one (heuristic wins)
    # on   = a confident LLM decision overrides the routing regexes
    #        (automation-forced planning is never overridden)
    V3_LLM_ROUTER_MODE: str = "off"

    # ── Option C Phase 3: one narrator (2026-10-14) ─────────────────────
    # fsm (default) = data turns route to the SynexiaFSM brain as before
    # react         = data turns route to the ReAct orchestrator, which
    #                 calls the boxed `query_database` tool (the FSM
    #                 pipeline runs INSIDE the tool; the orchestrator
    #                 narrates from grounded rows). Automation-forced
    #                 planning still runs the FSM top-level (deterministic
    #                 scheduled jobs must not depend on orchestrator
    #                 tool-choice).
    V3_DATA_ROUTE_MODE: str = "fsm"

    # ── ROI service (IBM Gap 1, 2026-10-13) ────────────────────────────
    # Assumptions behind the estimated hours-saved metric. These are the
    # customer-adjustable knobs returned alongside the estimate so the
    # number is always honest about what it assumes.
    ROI_MINUTES_PER_TURN: float = 10.0
    ROI_MINUTES_PER_ARTIFACT: float = 30.0
    ROI_HOURLY_RATE: float = 40.0

    # Cost ledger wiring (IBM Gap 1 completion): when True, every successful
    # LLM completion books its token usage into cost_ledger (fire-and-forget,
    # off the event loop). Disable to stop cost bookkeeping.
    COST_LEDGER_ENABLED: bool = True

    # ── Row-Level Security rollout (tenancy D3/D4, 2026-10-13) ─────────
    # When True, sessions whose request carried a verified org claim pin
    # `app.current_org` at transaction start (database.py after_begin) and
    # Postgres RLS policies confine tenant tables to that org. Tables NOT
    # in the RLS batch are unaffected; unset context (background jobs)
    # bypasses via the policies' NULL clause. Postgres-only.
    RLS_ENABLED: bool = True




    # Rate limits (in-memory, per IP). Zero = disabled.
    RATE_LIMIT_LOGIN_PER_MIN: int = 5
    RATE_LIMIT_REGISTER_PER_10MIN: int = 3
    RATE_LIMIT_OTP_PER_10MIN: int = 3
    RATE_LIMIT_RESET_PER_10MIN: int = 3

    # ── SSO / OIDC (roadmap item 7) ────────────────────────────────────
    # OFF by default — local email/password login is unaffected. Enabling
    # SSO does NOT disable local login (SSO is additive account linking).
    SSO_ENABLED: bool = False
    # JSON array of provider configs:
    #   [{"id": "google", "name": "Google", "client_id": "...",
    #     "client_secret": "...", "issuer": "https://accounts.google.com",
    #     "discovery_url": "", "scopes": "openid email profile"}]
    # discovery_url optional — defaults to {issuer}/.well-known/openid-configuration.
    SSO_PROVIDERS: list[dict] = []
    # Optional email-domain allowlist (host or subdomain match; empty = allow all).
    SSO_ALLOWED_EMAIL_DOMAINS: list[str] = []
    # OIDC authorize-state TTL (seconds). In-memory store — single-process.
    SSO_STATE_TTL_S: int = 600

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

    # ── Email Notification Gateway (automation run results) ───────────────
    # Master switch. When False, no automation run ever sends email, even if
    # a task has recipients configured. Enable first, then add per-task emails.
    NOTIFICATION_GATEWAY_ENABLED: bool = False
    # Max attachment size in bytes before the gateway falls back to a signed
    # download link (8 MB default, matching the plan's "file > 8 MB → link").
    EMAIL_ATTACH_MAX_BYTES: int = 8_388_608
    # Lifetime of the HMAC-signed download link, in days.
    EMAIL_DOWNLOAD_LINK_TTL_DAYS: int = 7
    # Retry policy for SMTP transport errors. EMAIL_MAX_RETRIES attempts with
    # exponential backoff (1s/2s/4s by default). Permanent errors (bad address)
    # fail fast and do not retry.
    EMAIL_MAX_RETRIES: int = 3

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

    # Per-call LLM timeouts (IBM fault-tolerance: explicit per-call timeout).
    # LLM_TIMEOUT_S covers async chat/stream calls; LLM_SYNC_TIMEOUT_S covers
    # sync JSON + embedding calls.
    # 2026-10-10: the single 120s timeout applied to STREAMING calls too — a
    # large local-model generation died mid-stream. Split into connect/read:
    # connect stays tight (unreachable endpoint fails fast), read allows a
    # full long-form generation (LLM_READ_TIMEOUT_S).
    LLM_TIMEOUT_S: float = 300.0
    LLM_CONNECT_TIMEOUT_S: float = 10.0
    LLM_READ_TIMEOUT_S: float = 300.0
    LLM_SYNC_TIMEOUT_S: float = 30.0

    # Models that ignore or reject a caller-supplied ``temperature`` value.
    # Reasoning models like Moonshot kimi-k2.6 only accept temperature=1, and
    # OpenAI o1/o3 reject the field outright. For these, the temperature key is
    # omitted from chat/completions requests so the provider's own default is
    # used (otherwise a valid key surfaces as a misleading HTTP 400). Comma-
    # separated substrings matched case-insensitively against the model id.
    LLM_FIXED_TEMPERATURE_MODELS: str = "kimi-k2,o1,o3,qwen3,deepseek-v4"

    # Hard cap on the ``max_tokens`` sent to any LLM endpoint. Self-hosted
    # vLLM deployments often run with a smaller ``max_model_len`` than the
    # registered model card claims (e.g. card says 250000 but the server is
    # actually capped at 16384). A request with ``max_tokens`` greater than
    # the server's ``max_model_len`` is rejected with HTTP 400
    # ("max_tokens=... cannot be greater than max_model_len=..."), which
    # surfaces as a generic "Sorry, I hit an error" in chat. This cap is
    # applied to the user's saved max_tokens (UserSetting) before any LLM
    # request.
    #
    # NOTE: this is a SAFETY NET only. Per-model context-aware clamping
    # (``_clamp_max_tokens_for_context`` in agents.py, driven by
    # ``LlmModel.context_window`` / ``max_output_tokens``) now takes over as the
    # primary limiter, so this global value can stay generous without
    # regressing small vLLM servers (they are clamped per-model instead).
    # 2026-10-10: 4096 cut long reports mid-sentence on capable models —
    # raised to 16384; the per-model clamp remains the primary limiter.
    LLM_MAX_TOKENS_HARD_CAP: int = 16384

    # Synthesis (report-building) LLM output-token budget.
    # 1536 is far too small for a full multi-section report; 6144 gives
    # enough headroom for executive summary + markdown tables + analysis.
    LLM_SYNTH_MAX_TOKENS: int = 6144

    # ── Turn / synthesis / planner budgets (2026-10-10 de-hardcoding) ──────
    # These were PLAIN LITERALS in routers/services; a single slow LLM call
    # (allowed 120-300s) was killed by a tighter wall clock that nobody could
    # tune. All env-overridable now.
    # General-turn wall clock: the main v3 loop cap for turns that are not
    # dashboard/fusion/report builds. Was a 60s literal — LESS than one
    # LLM_TIMEOUT_S call — the #1 turn-killer.
    TURN_WALL_CLOCK_CAP_S: float = 600.0
    # Report/data/document turns run multi-query pipelines; was 180s literal.
    REPORT_TURN_WALL_CLOCK_CAP_S: float = 900.0
    # Forced-synthesis LLM call (empty/weak answer rescue). Was 60s literal.
    SYNTHESIS_TIMEOUT_S: float = 180.0
    # NL2SQL planner LLM call: SQL JSON for complex questions was truncated
    # at max_tokens=700 and aborted at a 45s literal timeout.
    NL2SQL_PLANNER_TIMEOUT_S: float = 120.0
    NL2SQL_PLANNER_MAX_TOKENS: int = 2000
    # Transient LLM error retry budget (429/overload). Was hard-capped at 3
    # attempts with ≤30s total backoff — sustained provider strain failed
    # the turn in seconds.
    LLM_RETRY_MAX_ATTEMPTS: int = 5
    # Sub-agent iteration clamps (delegate_task / ask_data_agent). Were
    # literal ≤10 clamps that truncated multi-step subtasks.
    DELEGATE_ITERATIONS_HARD_CLAMP: int = 25
    # Image generation API timeout. 60s was tight for DALL·E/FAL.
    IMAGE_GEN_TIMEOUT_S: int = 180
    # Rows of a large result the report synthesizer sees. Was 300 — the
    # synthesis narrated only a prefix of big datasets.
    REPORT_SYNTHESIS_MAX_ROWS: int = 1000
    # Mixed-mode planning (OCI agentic mode): definitional questions
    # ("what is net revenue" / 发泡剂是什么) are answered from the VERIFIED
    # glossary without any SQL — semantic lookup instead of execution.
    NL2SQL_DEFINITIONAL_LOOKUP_ENABLED: bool = True

    # ── Document ingestion — audio / image / video ────────────────────────
    # Audio: transcribe uploads via an OpenAI-compatible Whisper endpoint
    # (OPENAI_BASE_URL/audio/transcriptions) using the existing OPENAI_API_KEY.
    # No new API keys. Video transcription is deferred (needs ffmpeg).
    AUDIO_TRANSCRIBE_ENABLED: bool = False
    WHISPER_MODEL: str = "whisper-1"
    WHISPER_TIMEOUT_S: float = 60.0
    # Image: when True, run Tesseract OCR on uploaded images. Regardless of
    # this flag, the image extractor ALWAYS emits an "[Image attached: name]"
    # marker so the LLM knows the file exists (and the multimodal path can
    # forward the raw bytes to a vision-capable model).
    IMAGE_OCR_ENABLED: bool = False

    # ── Vision bridge (2026-10-13) ──────────────────────────────────────
    # Lets the chat agent answer questions about PASTED/UPLOADED images even
    # when the main model is text-only (live: deepseek-chat cannot see the
    # image content blocks, so "look at this image" answers "the image
    # didn't come through"). When a message carries images, each one is run
    # through this OpenAI-compatible vision endpoint and the analysis is
    # injected into the system prompt. VISION_* default to OPENAI_* so a
    # single-provider deployment works by just naming a vision model.
    # VISION_BRIDGE_MODE: auto = bridge only when the resolved main model
    # does not look vision-capable (native image blocks already work there);
    # always = bridge regardless; off = never (vision-model-only setups).
    VISION_MODEL: str = ""
    VISION_BASE_URL: str = ""
    VISION_API_KEY: str = ""
    VISION_BRIDGE_MODE: str = "auto"

    # ── Institutional-grade PPT/research-analyst pipeline (2026-08-25) ──
    # When True, the universal _RESEARCH_ANALYST_DIRECTIVE is appended to
    # the system prompt of every DB-bound agent (data_agent,
    # general_assistant, power_user, automation_agent,
    # plus any user agent with bound KBs or the "Database Query" skill).
    # The directive tells the LLM to use comprehensive_data(query, profile=
    # "market") for multi-dimensional data coverage on PPT / chat / brief
    # requests, and forces the synthesis-floor fallback when the main
    # LLM call drops into tool_calls with empty content (the common
    # failure mode with deepseek-chat on long analytical queries).
    COMPREHENSIVE_DATA_MARKET_PROFILE_ENABLED: bool = False
    # Pre-create_artifact coverage gate. When True, a PPT render is
    # rejected unless the payload's coverage_dimensions list has at
    # least COMPREHENSIVE_DATA_MIN_DIMENSIONS entries (default 3).
    COMPREHENSIVE_DATA_GATE_ENABLED: bool = False
    # Soft-block mode: log + carry on (instead of hard-fail).
    COMPREHENSIVE_DATA_GATE_SOFT_BLOCK: bool = False
    # Operator-only bypass: gate fully disabled (DR tests only).
    COMPREHENSIVE_DATA_GATE_BYPASS: bool = False
    # Minimum dimensions required to pass the coverage gate.
    COMPREHENSIVE_DATA_MIN_DIMENSIONS: int = 3

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
    # Offline deployments (no internet egress / no SEARCH_API_KEY): when
    # False, web_search / web_extract schemas are removed from every agent
    # toolset BEFORE the planner and the agent loop see them — no phantom
    # "Search the web" plan step, no turn budget burned on unreachable
    # providers (each httpx attempt can hang up to the provider timeout),
    # and no failed-lookup row that poisons the run verdict. The agent then
    # answers purely from bound data sources. Default True — flag-off only
    # where the deployment genuinely has no usable web access.
    WEB_TOOLS_ENABLED: bool = True

    # Image Generation — provider selected via IMAGE_API_PROVIDER
    # Supported: "openai" (reuses OPENAI_API_KEY), "fal"
    IMAGE_API_PROVIDER: str = "openai"
    IMAGE_API_KEY: str = ""  # FAL.ai key (only if IMAGE_API_PROVIDER="fal")
    IMAGE_MODEL: str = "dall-e-3"

    # Code Execution Sandbox
    # 2026-10-10: 30s killed any pandas-heavy script; real data-munging needs
    # minutes. 120s default, env-overridable.
    CODE_EXECUTION_TIMEOUT: int = 120  # seconds
    CODE_EXECUTION_MAX_MEMORY: int = 128  # MB (informational; subprocess enforcement)

    # Programmatic tool calling (PTC). When execute_code runs inside an agent
    # turn it may call the agent's own tools from inside the code block; those
    # blocks legitimately run longer than a plain computation because each
    # sub-call can be a delegated data query.
    PTC_ENABLED: bool = True
    PTC_EXECUTION_TIMEOUT: int = 600  # seconds for a PTC code block
    PTC_SUB_CALL_TIMEOUT: int = 300  # seconds per sub-call
    PTC_SUB_CALL_LIMIT: int = 25  # sub-calls allowed in one code block

    # Agent Workspace — sandboxed directory for read_file/write_file operations
    AGENT_WORKSPACE_DIR: str = "agent_workspace"

    # Delegate Task
    DELEGATE_MAX_ITERATIONS: int = 5
    # 2026-10-10: DELEGATE_TIMEOUT (120) was DEAD CONFIG — defined but never
    # referenced by any enforcement path. Removed to stop it misleading
    # operators into thinking a per-sub-conversation timeout exists.

    # Per-conversation total iteration budget across all turns (including
    # resumes). Bounds runaway multi-turn sessions. Per-agent overrides via
    # AgentApp.max_call_count take precedence when set.
    AGENT_MAX_ITERATIONS: int = 100

    # Tool output limits
    # 2026-10-10: 8000 silently truncated virtually every substantial tool
    # result (big query results, file reads) BEFORE the persistence layer
    # could spill with a read pointer — the model then "hallucinated" the
    # missing content. 50000 chars; Layer-2 persistence (spill + pointer)
    # remains the real context guard.
    TOOL_MAX_OUTPUT_CHARS: int = 50000

    # Tool result persistence (P0) -- Layer 2/3 of context overflow protection
    TOOL_RESULT_STORAGE_DIR: str = "tool_results"

    # NL2SQL: bounded repair of a query the DATABASE rejected (unknown
    # column/table, syntax). The driver error + the real column list go back to
    # the model, which re-generates once per attempt. 0 disables the loop.
    NL2SQL_DB_ERROR_REPAIR_MAX: int = 2
    # NL2SQL_PLAN_MEMORY_ENABLED: reuse the plan (tables + SQL shape) that
    # verified-answered this same question before, so one business question
    # cannot answer with a different column on each run. Rows are always read
    # live; relative periods are re-resolved against today. Store:
    # app/models/query_plan_memory.py, logic: app/services/db/plan_memory.py.
    NL2SQL_PLAN_MEMORY_ENABLED: bool = True

    # ── Answer figure provenance (routers/agents.py guardrail) ───────────
    # Every SQL-side guard stops at the rows. This checks the PROSE: each
    # data figure in the final answer must exist in the turn's tool results
    # (after scale/rounding normalisation, sums, and ratio derivation — see
    # services/db/answer_grounding.py). A figure that survives one retry is
    # logged, never silently accepted.
    ANSWER_GROUNDING_ENABLED: bool = True
    # Relative tolerance for a match (0.005 = 0.5%), which absorbs rounding
    # ("≈3.46亿" for 345,678,912) without letting a wrong count through.
    ANSWER_GROUNDING_TOLERANCE: float = 0.005

    # ── Hard execution caps (non-bypassable) ─────────────────────────────
    # The model supplies max_rows/timeout_s on the tool args; these clamp what
    # the executor will honour. Applied in QueryService.execute so EVERY caller
    # inherits them (tool args, NLAnswerService, FSM nodes, automations).
    MAX_QUERY_ROWS_CAP: int = 5000
    MAX_QUERY_TIMEOUT_S: int = 30
    # ── Timeout policy + collation self-healing (2026-10-10) ──
    # QUERY_TIMEOUT_DISABLED=True removes the per-statement timeout entirely
    # (MAX_EXECUTION_TIME hints, SET statement_timeout, SQLite progress
    # handler, MSSQL LOCK_TIMEOUT and the tool-level clamp) so very large
    # warehouses can run unbounded aggregations. Trade-off: a runaway query
    # holds its connection until the database/DBAPI gives up — only enable
    # on warehouses where the statements are trusted and resource-bounded
    # upstream. Connect-level connect_timeout (handshake) still applies.
    QUERY_TIMEOUT_DISABLED: bool = True
    # Collation-mismatch self-healing: on ER 1267 / collation errors the
    # statement's cross-table join predicates are rewritten with
    # CONVERT(col USING <charset>) and retried once (MySQL family).
    COLLATION_HEAL_ENABLED: bool = True
    COLLATION_TARGET_CHARSET: str = "utf8mb4"

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

    # ── multi-tenant RBAC (Phase 1, plan 2026-08-03) ────────────────────
    # When False, POST /apps/{app_id}/auth/register returns 403 — no new
    # accounts can be created via the self-service registration flow.
    # Default True for backward compatibility with existing deployments.
    ALLOW_SELF_REGISTRATION: bool = True

    # Super-admin bootstrap credentials — the idempotent startup seed reads
    # these to create (or update) the single super-admin account.  If both
    # are empty (default), no admin account is seeded (backward compat).
    SUPERADMIN_EMAIL: str = ""
    SUPERADMIN_PASSWORD: str = ""

    # Compaction settings (P0)
    COMPACTION_AUTO_THRESHOLD: float = 0.8  # context window usage threshold
    COMPACTION_MICROCOMPACT_THRESHOLD: int = 30000  # tokens to trigger microcompact
    COMPACTION_FULL_THRESHOLD: int = 50000  # tokens to trigger full compact
    COMPACTION_MAX_CONTEXT_MESSAGES: int = 100
    COMPACTION_PRESERVE_RECENT: int = 6

    # Provider Profile (P3)
    ACTIVE_PROVIDER: str = ""  # empty = use OPENAI_* env vars

    # ── Model Layer (Part 2 Gap Analysis) ────────────────────────────────
    # Task-based model routing — JSON mapping of task_type → model_name.
    # Example: '{"simple_chat":"deepseek-chat","reasoning":"deepseek-reasoner"}'
    # When empty or unset, all tasks use settings.LLM_MODEL.
    MODEL_TASK_ROUTING: str = ""

    # LLM response cache (Redis). Only caches temperature=0 calls.
    LLM_RESPONSE_CACHE_ENABLED: bool = False
    LLM_RESPONSE_CACHE_TTL_S: int = 3600  # 1 hour

    # Per-model context window sizes (tokens). Used by compaction logic.
    # Example: '{"deepseek-chat":64000,"gpt-4o":128000,"deepseek-reasoner":64000}'
    MODEL_CONTEXT_WINDOWS: str = ""

    # Provider health-based routing (circuit breaker).
    # When True, call_llm() uses select_provider() to pick the healthiest
    # provider instead of the fixed order from LLM_FALLBACK_PROVIDERS.
    LLM_HEALTH_ROUTING_ENABLED: bool = False

    # Inject ``parallel_tool_calls: True`` into LLM request payloads for
    # OpenAI-compatible models (deepseek*, openai/*, gpt-*, qwen*,
    # moonshot/kimi*, minimax*, glm/zhipu*).  Anthropic models are excluded
    # automatically (they reject the field).  When False (default), no
    # injection — behaviour is unchanged.
    LLM_PARALLEL_TOOL_CALLS_ENABLED: bool = False

    # Seed well-known LLM catalog rows (kimi-k2, MiniMax-M2, ...) into the
    # llm_models table on startup. "missing-only" (default) inserts a row
    # only when no row with the same model_id exists for the tenant, so
    # admin edits survive reseeding; "skip" disables catalog seeding.
    LLM_CATALOG_SEED_MODE: str = "missing-only"

    # ── Deep Research Mode (v1) ────────────────────────────────────────
    # In-loop research protocol: plan → parallel gather rounds (with
    # critique/refine between rounds) → cited final report. Flag-gated;
    # when False, research metadata on a message is ignored.
    DEEP_RESEARCH_ENABLED: bool = False
    # Hard cap on gather rounds regardless of depth preset.
    DEEP_RESEARCH_MAX_ROUNDS: int = 3
    # Per-LLM-call timeout inside the research protocol (seconds).
    # 2026-10-10: 90s also capped the WHOLE deep-research turn stream —
    # raised to 300s so a real research pass completes.
    DEEP_RESEARCH_TIMEOUT_S: int = 300

    # ── P1-5 context economics: per-model tool-output caps ──────────────
    # Each entry caps the *content* of a single tool-role message at the
    # given token count.  Smaller caps for small-context models prevent a
    # single big tool result from blowing the whole window.  Sorted-key
    # longest-prefix matching (deepseek-* before deepseek).
    # 4 chars/token is the conservative floor; we use it directly here
    # so the cap is the same regardless of any padding heuristics.
    TOOL_OUTPUT_CAP_BY_MODEL: str = (
        '{"qwen3.6-27b":12288,'
        '"deepseek-v4-flash":24576,'
        '"deepseek-chat":24576,'
        '"gpt-4o":16384,'
        '"gpt-4o-mini":16384,'
        '"claude-sonnet":32768,'
        '"claude-opus":32768,'
        '"claude-3-5-sonnet":32768}'
    )

    # P1-5 escalation ladder: tier index → action.  Called by the
    # agents loop when ``get_context_window`` is about to overflow.
    # The actions are:
    #   0 compact                 — run auto_compact_if_needed
    #   1 truncate_tool_outputs   — apply per-model tool-output caps
    #   2 drop_old_tool_messages  — drop the oldest tool-role messages
    #   3 fallback_to_different_model — switch to a model with a larger
    #                                   context window
    CONTEXT_ESCALATION_LADDER: str = (
        '["compact","truncate_tool_outputs",'
        '"drop_old_tool_messages","fallback_to_different_model"]'
    )

    # ── P1-5: data-source-runtime compact mode ─────────────────────────
    # When the target model's context_window is <= DSR_COMPACT_MODE_MAX_CONTEXT,
    # the "Bound Data Sources" block is structurally compressed
    # (drop sample rows, verbose descriptions; cap concept catalog).
    # Models with context_window > this threshold see the full prompt
    # (unchanged behaviour).
    # Set to 0 to disable compact mode entirely.
    DSR_COMPACT_MODE_MAX_CONTEXT: int = 70_000
    DSR_COMPACT_CONCEPT_MAX_LINES: int = 20

    # ── Rate Limiting ───────────────────────────────────────────────────
    RATE_LIMIT_ENABLED: bool = False
    RATE_LIMIT_RPM: int = 60  # max requests per minute per user+app
    RATE_LIMIT_WINDOW_S: int = 60  # sliding window duration
    RATE_LIMIT_WHITELIST: str = "[]"  # JSON array of exempt user IDs

    # ── Task Queue ──────────────────────────────────────────────────────
    TASK_QUEUE_ENABLED: bool = False
    TASK_QUEUE_MAX_RETRIES: int = 3

    # ── Retrieval Hybrid (Context Handling) ────────────────────────────
    RETRIEVAL_HYBRID_ENABLED: bool = False
    RETRIEVAL_HYBRID_TOP_K: int = 10
    RETRIEVAL_HYBRID_MIN_SCORE: float = 0.5

    # ── Chat Upload RAG (2026-08-31) ───────────────────────────────────
    # Route LARGE uploaded files (and many-file turns) through the existing
    # ChromaDB + local-embedding pipeline instead of dumping the whole text
    # into the prompt (the 120k-char / ~30k-token wall). Files smaller than
    # RAG_UPLOADS_INLINE_MAX_CHARS keep the exact inline behavior; bigger
    # files are indexed once (idempotent per session+file) and the top-k
    # most relevant chunks are injected per turn. Embeddings are LOCAL
    # (sentence-transformers MiniLM, cached under /app/data/hf_cache) — no
    # cloud dependency. Fail-open: any embedding/store error degrades back
    # to the plain text dump.
    RAG_UPLOADS_ENABLED: bool = True
    RAG_UPLOADS_COLLECTION: str = "chat_uploads"
    RAG_UPLOADS_TOP_K: int = 8
    # Files with extracted text at or below this many chars stay fully
    # inline (no retrieval loss for small files).
    RAG_UPLOADS_INLINE_MAX_CHARS: int = 6000
    # When a single turn has more uploaded files than this, retrieve
    # instead of dumping every file's full text.
    RAG_UPLOADS_MAX_FILES_INLINE: int = 3

    # ── Dynamic Tool Loading (2026-08-31) ──────────────────────────────
    # Mode "dynamic": per turn, keep the always-on core tools and add only
    # the intent-relevant periphery tools (embedding or lexical match on the
    # user message). Mode "all": every tool schema every turn (legacy).
    # Fail-open: any selection error returns the full list.
    TOOL_LOADING_MODE: str = "dynamic"
    TOOL_LOADING_PERIPHERY_TOP_K: int = 12
    TOOL_LOADING_CORE: list[str] = [
        # conversation / memory
        "memory", "project_memory", "session_search", "clarify",
        # files + code
        "read_file", "write_file", "execute_code", "sandbox_code",
        # knowledge + grounding
        "web_search", "web_extract", "search_skills", "list_skills",
        "list_default_skills", "list_knowledge_bases",
        # delegation / agentic
        "delegate_task", "send_message", "interrupt",
        # data + dashboards (core Zhanlu value)
        "sql_query", "ask_data_agent", "comprehensive_data",
        "create_dashboard", "create_fullstack_dashboard", "edit_dashboard",
        "create_artifact", "edit_artifact",
        # skills
        "load_skill_body", "skill_manager", "list_fullstack_dashboards",
        "approval",
    ]

    # ── NLU / Intent (Part 2 Gap Analysis) ────────────────────────────
    INTENT_PLANNER_ENABLED: bool = False
    QUERY_REWRITE_ENABLED: bool = False

    # ── Eval Pipeline (Part 2 Gap Analysis) ───────────────────────────
    EVAL_PIPELINE_ENABLED: bool = False
    EVAL_SAMPLE_RATE: float = 0.1
    EVAL_MAX_SAMPLES_PER_RUN: int = 50
    PROMPT_AB_TEST_ENABLED: bool = False

    # ── Golden-eval regression gate (2026-08-29) ──────────────────────
    # Blocks (409) an admin model change when the candidate fails the golden
    # test-case suite at parity with the champion. Default OFF — flipping it
    # on activates the inline gate in the LLM admin endpoints (force=true
    # bypasses). The /api/admin/evals/regression preflight endpoint works
    # regardless of this flag.
    EVAL_GATE_ENABLED: bool = False
    EVAL_GATE_FLOOR: float = 0.8          # absolute minimum candidate pass rate
    EVAL_GATE_PARITY_TOLERANCE: float = 0.05  # max allowed drop vs champion
    EVAL_GATE_CASE_TIMEOUT_S: int = 60    # per-case fail-closed timeout
    GOLDEN_EVAL_MAX_ITERATIONS: int = 6   # agent-loop iteration budget per case

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
    # ONE shared per-turn budget for PLAN-level refinement (2026-09-20).
    # ACT re-plan and VERIFY re-plan were independent, so their limits
    # MULTIPLIED: SYNEXIA_ACT_REPLAN_MAX=2 x SYNEXIA_VERIFY_REPLAN_MAX=1
    # allowed up to six plans in a single turn (observed live: 4 plans, 12+
    # nodes, 129s). Each failure bought another plan, and the planner's own
    # chart step was left `pending` — planned but never reached. This cap
    # counts TOTAL plans per turn (the initial plan plus refinements) and is
    # shared by every re-plan path because they all persist a Plan row
    # against the same execution_id. 2 = the turn may re-plan once. Raise for
    # more self-correction at the cost of wall clock; the turn always answers
    # from the best content already produced and never ships an empty reply.
    SYNEXIA_TURN_REPLAN_MAX: int = 2
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

    # ── PPT generation pipeline (multi-stage deck exporter) ──────────────
    # The four stage toggles (deck planner, smart router, LLM polish, audit)
    # default OFF so the existing report-card renderer path is byte-for-byte
    # unchanged until a deployment opts in.  When enabled they add: LLM deck
    # planning (content structuring), deterministic smart routing (structured
    # vs sandbox html2pptx), and a one-shot LLM copy-polish pass.  Two
    # downstream flags default ON — PPT_DESIGN_BY_DEFAULT (design is the
    # default routing target) and PPT_AUDIT_BLOCKING_ENABLED (a failed
    # semantic audit blocks delivery); set either False to restore the
    # legacy behavior.
    # NOTE: the P0 audit/repair loop (audit_deck.py → repair_deck.py) is a
    # pre-existing always-on safety net gated by ZHANLU_AUDIT_REPAIR_CYCLES and
    # is NOT toggled here — PPT_AUDIT_ENABLED is reserved for an explicit
    # opt-out/opt-in override in future releases.
    PPT_DECK_PLANNER_ENABLED: bool = False
    PPT_SMART_ROUTER_ENABLED: bool = False
    # Design is the default routing target for pptx requests: route_deck sends
    # every deck to the sandbox HTML design renderer unless the user explicitly
    # asks for a plain data dump / simple text output (see _STRUCTURED_KEYWORDS
    # in deck_router.py).  Set False to restore the legacy structured-first
    # default.  Only consulted when PPT_SMART_ROUTER_ENABLED is on.
    PPT_DESIGN_BY_DEFAULT: bool = True
    # Phase: fully-dynamic document generation. When on, the docx / pptx
    # renderers execute an explicit ordered `blocks` plan (agent-authored or
    # produced by the server-side architect from the gathered data) instead of
    # a fixed template — yielding richer, data-tailored documents.
    DYNAMIC_DOCUMENT_PLANNING_ENABLED: bool = True
    # Phase (hybrid): after the deterministic architect builds the structure, an
    # LLM enriches the narrative (executive summary prose, hand-picked findings,
    # targeted recommendations) — the part the rule-based architect cannot do
    # well. Falls back to the deterministic narrative silently if the LLM is
    # unavailable. This is the "skill → LLM plans narrative, platform owns
    # structure" pattern that matches modern report agents (Kimi / MiniMax).
    DYNAMIC_DOCUMENT_LLM_NARRATIVE_ENABLED: bool = True
    PPT_LLM_POLISH_ENABLED: bool = False
    PPT_AUDIT_ENABLED: bool = False
    # Blocking audit gate: when the deck still FAILs the semantic audit after
    # the deterministic repair loop (and after the polish re-audit), the
    # dispatcher returns empty bytes + the FAIL report instead of delivering
    # the deck.  Default True (quality is a hard gate); set False to restore
    # the historical ship-anyway behavior.
    PPT_AUDIT_BLOCKING_ENABLED: bool = True
    # New: route create_artifact(type="pptx") through the professional deck
    # pipeline (planner → router → layout engine → audit/repair/polish) so the
    # in-chat artifact matches the download. Precondition: PPT_DECK_PLANNER_ENABLED.
    PPT_CREATE_ARTIFACT_PIPELINE_ENABLED: bool = False
    # New: ground deck data in the REAL query rows (Execution ObservationRecords
    # + conversation tool results) instead of only the LLM-authored payload
    # chart data. Used by both the download path and the tool path.
    PPT_DECK_DATA_GROUNDING_ENABLED: bool = False
    # Phase 4: intent-driven deck profiles (data_report / executive_brief /
    # pitch_narrative / periodic_review).  When OFF, every deck uses the
    # historical data_report structure (backward compatible, zero risk).
    DECK_PROFILES_ENABLED: bool = False
    # Phase 1C: deterministic per-slide deck EDITING tools (edit_slide /
    # add_slide / restyle_deck / update_chart / remove_slide / reorder_slide).
    # Routes "edit my deck"-style messages to the six deck-edit tools while
    # keeping "regenerate / redo / from scratch" on the full re-generation path.
    DECK_EDIT_ROUTING_ENABLED: bool = False
    # PPT turn-guard: enforce that a requested PPT deck is actually produced.
    # Synthesis-boundary nudge (cap 1/turn) + T-3 tool_choice forcing toward
    # create_artifact(type="pptx"); the dashboard guard takes precedence.
    PPT_TURN_GUARD_ENABLED: bool = False
    FILE_TURN_GUARD_ENABLED: bool = True
    # Phase 1B-REPORTS: server-side auto-analysis of cached DataExecution
    # rows when create_artifact builds a docx/pptx/pdf. Derives summary,
    # kpis, key_findings, recommendations, breakdown sections and an
    # aggregated chart from the data — so a bare LLM payload becomes a
    # real report (not a 25-row table dump). LLM-provided fields still
    # override; auto-analysis only fills blanks.
    REPORT_AUTO_ANALYSIS_ENABLED: bool = True
    # Phase 1B: layout-engine / data-table tuning (all defaults safe-off).
    LAYOUT_ENGINE_TEMPLATE_PATH: str = ""
    PPTX_DARK_THEME_PATH: str = ""
    PPTX_TOP_TABLE_ROWS: int = 8
    PPTX_DATA_TABLE_HIGHLIGHT_TOP: int = 3
    PPTX_VISUAL_VERIFY_ENABLED: bool = False

    # ── HTML design renderer (Phase 4) ────────────────────────────────
    # New render path for design-heavy decks. Renders the deck as HTML
    # via the theme catalog, then converts to PPTX (v1.0: image fill via
    # LibreOffice + pdftoppm; v1.1: editable text). When OFF, all decks
    # fall through to the existing structured python-pptx renderer.
    HTML_DESIGN_RENDERER_ENABLED: bool = False
    # Editable text pipeline (v1.1). Until this lands, only image_fill
    # is available regardless of HTML_DESIGN_RENDERER_ENABLED.
    HTML_DESIGN_EDITABLE_ENABLED: bool = False
    # Restrict the theme catalog. Empty list = all 12 presets available.
    # Populated by deployment, e.g. ["bold_signal", "electric_studio"].
    HTML_DESIGN_THEMES: list[str] = []

    # ── Deck hero art & motion (2026-08-29) ─────────────────────────────
    # Hero art: deterministic theme-aware SVG background on cover / section
    # dividers / closing. DECK_HERO_ART_ENABLED is the reliable offline path
    # (no API key). DECK_HERO_AI_IMAGES_ENABLED additionally tries the
    # configured image provider (image_generation) and falls back to SVG on
    # ANY failure — a deck is never blocked or slowed by image gen.
    DECK_HERO_ART_ENABLED: bool = True
    DECK_HERO_AI_IMAGES_ENABLED: bool = False
    # Slide transitions: inject a fade <p:transition> into every slide of
    # both PPTX paths. Pure XML addition, wrapped so it can never break a
    # render. Image-fill slides are static PNGs so the transition is the
    # only motion they can carry.
    DECK_TRANSITIONS_ENABLED: bool = True

    # ── DELIVERABLE GUARANTEE (2026-08-18 incident hardening) ────────────
    # Fix 1a: a mid-turn report card (html_report artifact) must NOT satisfy
    # an explicit pptx/docx/xlsx/dashboard request. When enabled, the
    # orchestrator fallback only counts artifacts whose stored type matches
    # the requested format; non-matching ids no longer preempt the fallback.
    DOC_REQUEST_STRICT_ARTIFACT_MATCH_ENABLED: bool = False
    # Fix 1b: max synthesis-boundary nudges the PPT turn-guard may emit per
    # turn. After the last nudge the v3/v2 loop FORCES create_artifact via
    # tool_choice instead of accepting another prose deflection.
    PPTX_NUDGE_MAX: int = 2
    # Fix 2: deterministic plausibility detectors (part > whole, cross-call
    # total drift) feed the answer-verification retry/disclose path.
    ANSWER_PLAUSIBILITY_CHECK_ENABLED: bool = False
    # Fix 3: NL2SQL JOIN fan-out guard — pre-aggregation-before-join rules
    # in the SQL prompt + structural warnings that trigger one self-correct.
    NL2SQL_FANOUT_GUARD_ENABLED: bool = False
    # Fix 5: consonant-skeleton fuzzy match so "Dashbord"-class typos still
    # route to the dashboard machinery across all four intent detectors.
    DASHBOARD_FUZZY_MATCH_ENABLED: bool = False
    # ── GOAL CONTRACT (shared agent loop) ─────────────────────────────────
    # A machine-checkable Goal Contract is built once per turn from the
    # user's message and updated at runtime from tool results. Before ANY
    # loop exit the contract is checked; unmet criteria force remediation
    # steps (artifact tool, re-query with distinct values, announced tool),
    # bounded by GOAL_CONTRACT_MAX_FORCES. Off = bit-for-bit current
    # behavior (all detectors/guards run their legacy paths).
    GOAL_CONTRACT_ENABLED: bool = False
    GOAL_CONTRACT_MAX_FORCES: int = 3

    # ── CLARIFY TURN SUSPENSION (2026-08-28) ─────────────────────────────
    # When True, a successful `clarify` tool call ends the turn immediately —
    # the user's next message IS the answer. Off = current behavior: the loop
    # keeps iterating after clarify, which lets the model burn the remaining
    # iteration budget on guard blocks / failing tools and end in a confusing
    # "phase_enter.act failed" + "Verification failed" instead of a clean
    # pause awaiting the user's answer (observed on the Sales Performance
    # Dashboard turn, conv f62e4c2b: 2x clarify + 2 blocked create_artifact +
    # 3 failed execute_code before the loop died).
    # Default ON since 2026-09-02 (clarify-card parity): an agent that asks
    # the user a question must STOP and wait for the answer — never continue
    # working on a guess. Rollback: set False (pre-2026-09-02 behavior).
    CLARIFY_SUSPENDS_TURN_ENABLED: bool = True

    # ── LIVE REASONING STREAM (2026-08-27) ──────────────────────────────
    # When True, the streaming LLM payload requests the provider's
    # chain-of-thought mode (vLLM qwen3: chat_template_kwargs
    # enable_thinking=True) so the agent loop receives per-token
    # `reasoning` deltas and relays them to the frontend as
    # `reasoning_delta` SSE events (the one-line "Live" status line).
    # Off = current behavior (no thinking requested, reasoning empty).
    LLM_ENABLE_THINKING: bool = False
    # Fix 1c: cap on turn-end "verify on stop" nudges. Shares the goal-contract
    # force budget when the contract is active (a nudge is bounded by
    # min(VERIFY_NUDGE_MAX, GOAL_CONTRACT_MAX_FORCES)); otherwise used as the
    # standalone nudge cap. Default 2 matches the historical hardcoded value.
    VERIFY_NUDGE_MAX: int = 2

    # ── DELIVERABLE PHASE-LOCK + QUERY-PURPOSE TAGGING (2026-08-21) ──────
    # Bug 1/2/3 fix for the "unprofessional agent" trace (supply-chain turn):
    # (1) DELIVERABLE_PHASE_LOCK_ENABLED — when the contract requires data,
    #     block create_artifact / run_sandbox_skill mid-loop until an
    #     answer-tagged dataset exists; build the deliverable ONCE post-loop
    #     from the FINAL answer dataset; strip internal-reference sentences
    #     from the final bubble.
    # (2) QUERY_PURPOSE_TAGGING_ENABLED — tag every ask_data_agent result
    #     probe / auxiliary / answer via catalog table_role + SQL shape;
    #     only answer-tagged results feed the deliverable + synthesis.
    # Both default OFF per project convention (flag-off = current behavior).
    DELIVERABLE_PHASE_LOCK_ENABLED: bool = False
    QUERY_PURPOSE_TAGGING_ENABLED: bool = False

    # ── Multi-Domain Enrichment (2026-08-25) ──────────────────────────────
    # Default per-turn cap for ask_data_agent calls. The dynamic estimator
    # (_estimate_ask_data_agent_cap) raises the cap above this for queries
    # with explicit metric lists, but for plain queries ("give me last
    # month sales report") the old floor of 2 was too low. Bumped to 4 so
    # the LLM has room to query across ERP / Inventory / Market / CRM /
    # Documents domains and produce a comprehensive answer.
    ASK_DATA_AGENT_DEFAULT_CAP: int = 4

    # When True, after the first ask_data_agent result lands in the loop,
    # inject a reflection prompt nudging the LLM to consider whether
    # additional domains should be queried before finalizing. The LLM
    # decides autonomously what (if anything) to query — this is guidance,
    # not a hard rule. Disabled in tests / evaluation runs.
    ASK_DATA_AGENT_REFLECT_AFTER_FIRST: bool = True

    # When True, append the data-domain coverage hint block to all
    # db-bound agent system prompts. Tells the LLM which business
    # domains (ERP, Inventory, Market, CRM, Documents) are typically
    # available so it can reason about cross-domain enrichment. Generic
    # — does not hardcode table names; agents discover via describe_schema.
    DATA_DOMAIN_HINTS_ENABLED: bool = True

    # ── Agent fast mode (2026-08-25, universalized 2026-08-27) ───────────
    # Per-model overrides were originally gated to qwen3-local vLLM only;
    # proven to cut turn time 200s → 42-52s while KEEPING answer quality
    # (8-15 LLM calls/turn → 3-5). Applied to ALL models now — big cloud
    # models (deepseek, etc.) are equally capable and faster, so the same
    # lean loop produces smoother, faster, high-quality answers.
    # 2026-10-10 goal-based amendment: fast mode ON by default was silently
    # capping the loop at 10 tool iterations (vs 40) AND disabling the
    # figure-grounding verify nudges (AGENT_FAST_VERIFY_NUDGE_MAX=0) for
    # EVERY model and turn — a hard-coded speed-over-quality governor the
    # user never opted into. Complex multi-query data turns can end early
    # and ungrounded answers ship uncorrected. Now opt-in (env
    # AGENT_FAST_MODE_ENABLED=true restores the lean loop).
    AGENT_FAST_MODE_ENABLED: bool = False
    AGENT_FAST_MAX_TOOL_ITERATIONS: int = 10                 # was 40
    # ONE shared refinement budget per turn (2026-09-20). Every post-answer gate
    # that discards a completed answer and regenerates it now spends from this
    # single budget, instead of each of the 12+ gates getting its own attempt.
    # Measured before this cap: one "Data Analyst" turn took 559.3s while the
    # SAME question on general_assistant took 285.7s — that spread is
    # re-synthesis count, not model speed. Raise to allow more self-correction
    # at the cost of wall-clock; 0 disables regeneration (the turn still answers
    # from the best content already produced — it never ships an empty reply).
    AGENT_REFINEMENT_MAX: int = 1
    # Dashboard builds (2026-08-27): a full-stack dashboard turn is a LONG
    # multi-phase pipeline (design system → schema → data collection → build →
    # verify). Fast-mode budgets (10 tool iterations / 180s wall clock) fire
    # BEFORE the build finishes — the user gets the canned "turn ended" note
    # instead of the live app. Dashboard turns therefore get their own
    # generous budget: 40 iterations and a 30-minute wall clock (effectively
    # no cap; the per-conversation AGENT_MAX_ITERATIONS=100 stays the outer
    # bound, and long-running tools emit progress frames to keep the SSE
    # connection alive).
    DASHBOARD_BUILD_MAX_TOOL_ITERATIONS: int = 40
    DASHBOARD_BUILD_WALL_CLOCK_CAP_S: float = 1800.0
    # CAD/Fusion builds: same class of long-running, multi-step build pipeline
    # as dashboard builds, and they hit the SAME wall. The agent loop's 180s cap
    # fired mid-build (logger: "v3 stream: wall-clock cap 180s hit"); the model
    # had built the hex-head sketch and was cut off before extruding, so the user
    # got a partial model and a BUILDING preamble instead of a finished part.
    # A reasoning model needs ~17k reasoning tokens and ~60s per LLM call, and a
    # bolt build is 8+ calls (clear/sketch/extrude/thread/chamfer/verify).
    FUSION_BUILD_WALL_CLOCK_CAP_S: float = 1800.0
    # Per-delegate wall-clock for ask_data_agent DURING dashboard turns.
    # The default delegate budget (60s) truncates data collection before all
    # metrics are gathered. Dashboard delegates get 10 minutes (no practical
    # cap — ask_data_agent is a long-running tool, so progress frames keep
    # the SSE connection alive). Injected via the `budget_seconds` arg.
    DASHBOARD_DELEGATE_BUDGET_SECONDS: float = 600.0
    # Turn planning (2026-08-27): deterministic intent-derived plan (todo
    # list) emitted BEFORE the loop (plan_step_added SSE) and injected into
    # the model context so the agent visibly plans then follows the plan.
    # Previously planning was optional model prose that weak local LLMs never
    # produced — the loop freewheeled into repeated data-source calls.
    # Flag-off = legacy behavior (plan events only from voluntary model prose).
    TURN_PLAN_ENABLED: bool = True
    # Dynamic per-request plans (2026-08-27): one cheap LLM planning call
    # tailors the turn plan to the SPECIFIC request (metrics, regions, tools
    # named by the user) instead of the fixed intent template. Falls back to
    # the fixed template when the LLM output is invalid or the call fails/
    # times out. Flag-off = fixed templates only.
    TURN_PLAN_DYNAMIC_ENABLED: bool = True
    TURN_PLAN_DYNAMIC_TIMEOUT_S: float = 20.0
    AGENT_FAST_GOAL_CONTRACT_ENABLED: bool = False           # was True
    AGENT_FAST_VERIFY_NUDGE_MAX: int = 0                     # was 2
    AGENT_FAST_DATA_AGENT_BUDGET_SECONDS: float = 30.0        # was 60
    AGENT_FAST_FORCE_SYNTHESIS_MAX_RETRIES: int = 0          # was 1
    # Synthesis quality knobs:
    AGENT_FAST_SYNTHESIS_MAX_TOKENS: int = 3072              # was 1536
    AGENT_FAST_SYNTHESIS_TEMPERATURE: float = 0.3            # was default

    # D4 (2026-08-20): deterministic category-subset coverage check for the
    # verification gate. When True, an answer to a category/portfolio request
    # whose query results only cover a PROPER SUBSET of the catalog's known
    # members for that category (from catalog_meta sample_values) is flagged
    # INCOMPLETE and nudged to re-query the unified table. Default OFF.
    CATEGORY_SUBSET_CHECK_ENABLED: bool = False
    # D5 (2026-08-20): deterministic arithmetic-consistency gate. When True,
    # stated arithmetic claims in the draft answer ("X of Y leaving Z",
    # "X/Y ≈ Z months", "X% of Y") are recomputed; mismatches >2% flag
    # INCOMPLETE. Default OFF.
    ARITHMETIC_CONSISTENCY_ENABLED: bool = False

    # Phase 1 response grounding: char cap for the findings block injected
    # into the FINALIZE response prompt (spec §3.3). Metadata is preserved
    # over data rows under the cap.
    # 2026-10-10: was 2000 — a 10-row top-N result (Chinese product names +
    # full-precision revenue figures ≈ 250 chars/row) did not fit, so the
    # narrator got the aggregates but only ~3 itemized rows and disclosed the
    # rest as "not included in the returned extract". 8000 chars ≈ a 60-row
    # extract; the per-row guard (_TOP_ROWS=50) still bounds pathological dumps.
    SYNEXIA_GROUNDING_MAX_CHARS: int = 8000

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

    # ── Nightly forecast scheduler ─────────────────────────────
    # Gates the /bootstrap-nightly-forecast endpoint. When False, the
    # endpoint returns 403. The engine itself is gated by FORECASTING_ENABLED.
    NIGHTLY_FORECAST_ENABLED: bool = True
    # Cron expression for the nightly forecast run. Default: 2 AM daily.
    NIGHTLY_FORECAST_CRON: str = "0 2 * * *"

    # ── Forecast Phase 1 enhancement flags ─────────────────────
    FORECAST_EXOG_ENABLED: bool = True
    FORECAST_PREPROCESS_ENABLED: bool = True
    FORECAST_CASCADE_ENABLED: bool = True
    FORECAST_ADAPTIVE_WEIGHTS_FACTOR: float = 0.3
    FORECAST_DRIFT_THRESHOLD_PCT: float = 20.0
    # Domain-signal overlay (feedstock elasticities + seasonal rules).
    # Enable per A/B validation (tests/test_domain_signals_ab.py).
    FORECAST_DOMAIN_SIGNALS_ENABLED: bool = False
    # LLM analyst brief writer. When False, the deterministic template
    # brief is used everywhere (no LLM calls).
    FORECAST_ANALYST_LLM_ENABLED: bool = False

    # ── Forecast self-learning flags (P0/P2) ──────────────────────
    # p_rise isotonic calibration: recalibrate rise probabilities weekly
    # from realized decision data using IsotonicCalibrator.
    FORECAST_P_RISE_CALIBRATION_ENABLED: bool = False
    # Champion/challenger: shadow-run alternative models nightly, persist
    # metrics to DB, auto-promote when challenger beats champion consistently.
    FORECAST_CHAMPION_CHALLENGER_ENABLED: bool = False

    # ── Forecast feature-activation flags (centralized 2026-08-21) ──────
    # All previously read via scattered os.environ.get() calls; now declared
    # here so the admin API can introspect them and Docker restarts pick up
    # changes from .env. All default False per project convention.
    FORECAST_ERP_SMOOTHING_ENABLED: bool = False
    FORECAST_ERP_SMOOTHING_WINDOW: int = 7
    FORECAST_REGIME_AWARE_POOL_ENABLED: bool = False
    FORECAST_STACKING_ENABLED: bool = False
    FORECAST_ENHANCED_PREPROCESS_ENABLED: bool = False
    FORECAST_ERP_VOLUME_EXOG_ENABLED: bool = False
    FORECAST_DEMAND_SIGNAL_EXOG_ENABLED: bool = False
    FORECAST_EXTERNAL_EXOG_ENABLED: bool = False
    FORECAST_TECHNICAL_INDICATORS_ENABLED: bool = False
    FORECAST_FOURIER_FEATURES_ENABLED: bool = False
    FORECAST_REGIME_DETECTION_ENABLED: bool = False
    FORECAST_REGIME_ADAPTIVE_WEIGHTS_ENABLED: bool = False
    FORECAST_ADVANCED_GUARD_ENABLED: bool = False
    FORECAST_MONOTONICITY_ENABLED: bool = False
    FORECAST_SOFT_GATE_ENABLED: bool = False
    FORECAST_SOFT_GATE_MARGIN_PCT: float = 2.0
    # Model-pool flags
    FORECAST_XGB_DIRECT_ENABLED: bool = False
    FORECAST_FOUNDATION_MODELS_ENABLED: bool = False
    FORECAST_FOUNDATION_MODEL_CHRONOS_ENABLED: bool = True
    FORECAST_FOUNDATION_MODEL_MOIRAI_ENABLED: bool = True
    FORECAST_VAR_ENABLED: bool = False
    FORECAST_XGB_TUNING_ENABLED: bool = False
    FORECAST_FEATURE_SELECTION_ENABLED: bool = False
    # Fine-tuning flags
    FORECAST_FINETUNING_ENABLED: bool = False
    FORECAST_FINETUNING_PROMPT_TOKENS: int = 16
    FORECAST_FINETUNING_LR: float = 1e-3
    FORECAST_FINETUNING_EPOCHS: int = 5
    FORECAST_FINETUNING_DIR: str = "/tmp/forecast_finetuning"
    # Cross-product lag features (P1-2A)
    FORECAST_CROSS_PRODUCT_LAGS_ENABLED: bool = False
    # Feedback training (P1-4A)
    FORECAST_FEEDBACK_TRAINING_ENABLED: bool = False
    # Quantile regression (P3-2B)
    FORECAST_QUANTILE_REGRESSION_ENABLED: bool = False
    # Adversarial shift detector (P3-1D)
    FORECAST_ADVERSARIAL_SHIFT_ENABLED: bool = False
    # Self-accuracy feature (P3-2C)
    FORECAST_SELF_ACCURACY_FEATURE_ENABLED: bool = False
    # Preprocessing flags
    FORECAST_ANOMALY_DETECTION_ENABLED: bool = False
    # Decision-engine thresholds
    FORECAST_BUY_THRESHOLD: float = 0.70
    FORECAST_SELL_THRESHOLD: float = 0.30
    FORECAST_BUY_MIN_CHANGE: float = 0.03
    FORECAST_SELL_MIN_CHANGE: float = -0.03
    FORECAST_EDGE_THRESHOLD: float = 0.55
    FORECAST_P_HIGH_MARGIN: float = 0.25
    # Drift/accuracy tuning
    FORECAST_DRIFT_BLEND_FACTOR: float = 0.2
    FORECAST_ACCURACY_THRESHOLD_EXCELLENT: float = 8.0
    FORECAST_ACCURACY_THRESHOLD_ACCEPTABLE: float = 15.0
    FORECAST_ACCURACY_THRESHOLD_CRITICAL: float = 25.0
    # Analyst signals
    FORECAST_DEMAND_SIGNAL_ENABLED: bool = False
    FORECAST_EXTERNAL_SIGNAL_ENABLED: bool = False

    # ── Feature flags (legacy reference migration) ─────────────────────
    # Per-module gates — each enables one category of ported services.
    # All default False so the agent is safe to register in every deployment;
    # enable progressively as phases 5A→5F are completed and tested.
    KNOWLEDGE_GRAPH_ENABLED: bool = False   # 5A
    PRICING_ENABLED: bool = False           # 5B
    PERCEPTION_ENABLED: bool = False        # 5C
    INTEL_ENHANCED_ENABLED: bool = False    # 5D
    ERP_WRITEBACK_ENABLED: bool = False     # 5F
    SCHEMA_DEPRECATION_ENABLED: bool = True  # legacy column over material_model hint

    # ── Project Knowledge Cache (2026-08-25) ───────────────────────────
    # Project-scoped facade over project_entity / project_entity_link /
    # project_metric / project_catalog_overlay / catalog tables. Scoped to
    # PROJECT_KNOWLEDGE_AGENT_NAMES. All default False.
    PROJECT_KNOWLEDGE_CACHE_ENABLED: bool = False   # master gate; enables cache facade
    PROJECT_KNOWLEDGE_QWEN_FAST_PATH: bool = False  # inject cached answer into agent loop pre-LLM
    QWEN_FAST_PATH_MODEL_PREFIXES: list[str] = ["qwen", "Qwen"]
    # These flags are generic no-op gates kept for deployment compat — the
    # default BI agent was REMOVED 2026-08-27 (industry-specific default).
    # Runtime reads per-app domain config;
    # PROJECT_KNOWLEDGE_AGENT_NAMES stays empty so no agent ever matches
    # the legacy project-cache / allowlist triggers.
    PROJECT_KNOWLEDGE_AGENT_NAMES: list[str] = []
    PROJECT_KNOWLEDGE_INGEST_BG_WORKER: bool = True    # run ingestion in asyncio.to_thread
    PROJECT_KNOWLEDGE_LAYER_ENTITIES_ENABLED: bool = True   # Layer 2 (entity lookup) on/off
    PROJECT_KNOWLEDGE_LAYER_METRICS_ENABLED: bool = True    # Layer 3 (metric lookup) on/off

    # ── Schema Linker Allowlist (DE-HARDCODED 2026-08-27) ──────────────
    # The table allowlist no longer lives in code. Per-app domain configs
    # (backend/app/domain_configs/<agent_name>.json) carry each app's own
    # curated table list; platform default is NO allowlist (fully generic
    # schema discovery). These settings are retained as legacy no-op flags
    # for backward compatibility — the runtime reads domain_config.
    SCHEMA_LINKER_ALLOWLIST_ENABLED: bool = False
    SCHEMA_LINKER_TABLE_ALLOWLIST: list[str] = []

    # ── Supply Chain Snapshot (per-product ERP panel under RegionalTable) ──
    SUPPLY_CHAIN_SNAPSHOT_ENABLED: bool = False      # Gate for GET /market-dashboard/supply-chain/{product_id}

    # ── Forecasting (legacy engine) ────────────────────────────────────
    # The unified ForecastingAgentService facade was removed with the
    # market dashboard feature; forecast tools call ForecastEngine directly.
    UNIFIED_FORECASTING_SERVICE_ENABLED: bool = False

    # ── Weekly Summary & Forecast Digest ────────────────────────────────
    # REMOVED with the market dashboard feature (weekly_digest service was
    # dashboard-only). Settings kept for config compatibility; no scheduler
    # or API consumes them.
    WEEKLY_DIGEST_ENABLED: bool = False             # Master gate for weekly digest scheduler + API
    WEEKLY_DIGEST_HOUR_UTC: int = 0                 # Generation hour (0 = 08:00 CST Monday)
    WEEKLY_DIGEST_DAY_UTC: int = 0                  # Day of week (0=Monday, 6=Sunday)
    WEEKLY_DIGEST_PPTX_EXPORT_ENABLED: bool = False # One-click PPTX export of a weekly digest report

    # ── Full-Stack Dashboard Pipeline (replaces legacy SQL-widget dashboards) ──
    # FULLSTACK_DASHBOARD_ENABLED: master gate for the NEW pipeline — agent
    #   calls uiux_design_system (--persist) → confirms data contract → calls
    #   create_fullstack_dashboard → generator fills Jinja2 templates →
    #   manager mounts a FastAPI sub-router → WebSocket pushes live data.
    # LEGACY_DASHBOARD_ENABLED: keeps the OLD create_dashboard SQL-widget tool
    #   + Dashboard model + REST router available for emergency rollback.
    # Both default False. When FULLSTACK is on, dashboard-shaped requests are
    #   routed to the new pipeline and the legacy tool is hidden from the agent.
    FULLSTACK_DASHBOARD_ENABLED: bool = False
    LEGACY_DASHBOARD_ENABLED: bool = False
    # T10: default visibility scope for newly-created full-stack dashboards.
    # "personal" = visible only to the creator; "company" = visible to the
    # whole org. The agent can still ask the user at creation time and pass
    # an explicit scope to create_fullstack_dashboard.
    DASHBOARD_DEFAULT_SCOPE: str = "personal"
    # T11: Postgres LISTEN/NOTIFY upgrade. When True AND the app DB is
    # Postgres, generated dashboard pollers subscribe to a per-dashboard
    # channel (zhanlu_dashboard_{slug}) and refresh immediately on NOTIFY,
    # instead of pure interval polling. Interval hash-polling is kept as the
    # fallback (catches external writers that never NOTIFY). Non-Postgres
    # backends always use interval polling. Default False.
    DASHBOARD_PG_LISTEN_ENABLED: bool = False
    # #7: require a logged-in user on generated dashboard-app DATA endpoints
    # (metrics REST, config.json, WS, non-shell SPA paths). The SPA shell and
    # hashed static bundles stay anonymous so the iframe can boot; the shell
    # then fetches config/metrics WITH the session token (?token=) that the
    # template frontend reads from localStorage (2026-09-02 token flow).
    # Default False (public dashboards); flip True to enforce.
    DASHBOARD_ENDPOINT_AUTH_ENABLED: bool = False
    # T12: per-turn cap on describe_schema calls when the user request is
    # dashboard-shaped. Prevents the agent from exhausting its tool-loop
    # budget on redundant schema exploration instead of calling
    # create_fullstack_dashboard. Default 2 (matches SKILL.md "at most two
    # exploratory calls before building"). Set to 0 to disable the cap.
    MAX_DESCRIBE_SCHEMA_PER_DASHBOARD_TURN: int = 2

    # Fix 3: total-exploration cap per dashboard turn. Counts describe_schema
    # + execute_query + execute_sql + sql_query COMBINED so a weak model
    # cannot burn the whole tool budget on query exploration instead of
    # calling the build tool. Fires independently of the describe_schema cap
    # above (whichever trips first). Set to 0 to disable.
    MAX_DASHBOARD_EXPLORATION_PER_TURN: int = 8

    # Fix 6: dashboard-narration nudge guard. When the model exits the v3
    # loop with ONLY narration ("I'll build you an ERP dashboard...") and no
    # tool call on a dashboard-shaped turn, inject a hard nudge naming the
    # exact next workflow step and continue the loop. Default False.
    DASHBOARD_NARRATION_NUDGE_ENABLED: bool = False
    # Cap on narration nudges per turn — a weak model cannot be nudged
    # forever; after the cap the narration-only exit is accepted. Default 1.
    MAX_DASHBOARD_NARRATION_NUDGES: int = 1

    # ── Experience layer (self-learning agent) ──────────────────────────
    # 3-layer additive system on the main chat loop (backend/app/routers/agents.py):
    #   Layer 1 (recipes): record successful tool sequences per question
    #     intent class and inject them as a playbook into the system prompt
    #     so future turns follow proven recipes instead of exploring.
    #   Layer 2 (semantic cache): cache final answers keyed by question
    #     embedding + data version; similar questions (sim >= 0.92) with the
    #     same data version get near-instant cached replies. Shared across
    #     users for data-driven reports, per-user for conversational answers.
    #   Layer 3 (user profile): per-user-per-agent preferences (language,
    #     products, depth) injected into the prompt.
    # All default False — safe to ship; enable per phase after tests pass.
    RECIPE_LEARNING_ENABLED: bool = False   # Layer 1
    USER_PROFILE_ENABLED: bool = False      # Layer 3
    RESPONSE_CACHE_ENABLED: bool = False    # Layer 2
    FEEDBACK_ENABLED: bool = False          # Phase C wiring
    AUTOMATION_FASTPATH_ENABLED: bool = False  # DISABLED — automation only via UI "New Automation Task" button

    # ── Data Execution cache / cleanup / intent router ───────────────────
    # All default False — safe to ship; enable per phase after tests pass.
    DATA_EXECUTION_CACHE_ENABLED: bool = False      # Persistent session cache for DataExecution
    DATA_EXECUTION_CLEANUP_ENABLED: bool = False    # Scheduled TTL + per-session cap cleanup for DataExecution
    INTENT_ROUTER_ENABLED: bool = False             # Intent classification routing for data-execution flows

    # ── Role-based personalization ───────────────────────────────────────
    # ROLE_PERSONALIZATION_ENABLED: inject the user's free-text
    #   role_descriptions into the agent system prompt as a
    #   [User Role Context] block so answers are tailored to the user's role(s).
    # ROLE_FEEDBACK_THROTTLE: show the "Relevant to your role?" 1-5 rating on
    #   every Nth assistant message (client-side throttle, N >= 1).
    ROLE_PERSONALIZATION_ENABLED: bool = False
    ROLE_FEEDBACK_THROTTLE: int = 3

    # ── Project Knowledge Graph / Semantic Catalog ───────────────────────
    # Phase 1 flags.
    # SEMANTIC_CATALOG_ENABLED: background indexer per KB (tables, columns,
    #   FK, LLM descriptions, Chroma embeddings).
    # SCHEMA_LINKING_ENABLED: query-time RRF retrieval + join-path expansion
    #   into a curated ~800-token DDL slice for NLAnswerService/Data Agent.
    #   Enabled: cuts Data Agent query time ~50% by replacing full-table
    #   describe_schema with targeted 3-5 table DDL slices from ChromaDB.
    SEMANTIC_CATALOG_ENABLED: bool = True
    SCHEMA_LINKING_ENABLED: bool = True
    # NL2SQL_SHADOW_MODE_ENABLED: run the governed nl2sql validator/policy
    #   alongside the live agent data path (NLAnswerService), logging
    #   divergence to nl2sql_query_logs. The served result is NEVER altered
    #   in shadow mode; the serving switch happens only on eval evidence.
    #
    #   P5-A: this flag is OBSERVABILITY ONLY. It controls whether the divergence
    #   COMPARISON IS LOGGED, never whether execution is validated — the checks
    #   owed to the database run unconditionally at the boundary itself
    #   (services/db/execution_gate.py, called from QueryService.execute). Setting
    #   this to False does not disable, weaken or skip any check.
    NL2SQL_SHADOW_MODE_ENABLED: bool = False
    # ── Source readiness gate (P1: dynamic database semantic enrichment) ────
    # A database source moves through connected → discovering → indexed →
    # enriched → validated → nl2sql_ready (see services/db/readiness.py).
    # NL2SQL_READINESS_GATE_ENABLED: answering paths REFUSE a source that is
    #   not nl2sql_ready instead of silently querying a pending/failed/stalled
    #   catalog. Turning this off restores the old permissive behaviour; the
    #   verdict is still computed and attached as a disclosure, so the
    #   limitation stays visible either way.
    NL2SQL_READINESS_GATE_ENABLED: bool = True
    # NL2SQL_READINESS_ALLOW_PARTIAL: a source whose structure is known but
    #   whose descriptions/examples are partial (enrichment_partial) may still
    #   answer, provided every answer states the limitation.
    NL2SQL_READINESS_ALLOW_PARTIAL: bool = True
    # CAPABILITY_PROBE_ENABLED: at index time, actively probe the source for
    #   its capability profile (server version, timezone, locale, views,
    #   window functions, read-only account, latency). Probe failures are
    #   recorded as evidence and never fail the index.
    CAPABILITY_PROBE_ENABLED: bool = True
    # KG_RESOURCE_ROUTER_ENABLED: deterministic per-question resource routing
    #   (database / document / memory / report / multi_resource) consumed by
    #   delegation tools. Fallback decisions = behave exactly as today.
    KG_RESOURCE_ROUTER_ENABLED: bool = False
    # KG_RESOURCE_REGISTRY_ENABLED: unified project resource registry
    #   (visibility-tiered) + registry indexers + read API.
    KG_RESOURCE_REGISTRY_ENABLED: bool = False
    # ENTITY_GRAPH_ENABLED: generic entity extraction from project memory +
    #   entity-aware retrieval + "Project Data Map" prompt section.
    ENTITY_GRAPH_ENABLED: bool = False
    # REPORT_RECIPES_ENABLED: first-class report recipes (SQL bundles direct
    #   via QueryService, charts via sandbox, validation rules enforced).
    REPORT_RECIPES_ENABLED: bool = False
    # DEPTH_ANALYSIS_LOOP_ENABLED: bounded hypothesis->query->validate loop
    #   for "why" questions; evidence packs; LLM explains, never decides.
    DEPTH_ANALYSIS_LOOP_ENABLED: bool = False
    # ── Schema-Aware Multi-Table Analysis (join edge inference) ──────────
    # CATALOG_JOIN_EDGES_ENABLED: at index time, sample joinable columns
    #   (int + short varchar) and infer VALUE_OVERLAP / NAME_MATCH edges via
    #   join_edge_detector, persisting them into kb_table_relation alongside
    #   declared FKs. All inferred edges are ranked strictly below FK (1.0).
    CATALOG_JOIN_EDGES_ENABLED: bool = False
    # JOIN_EDGE_SAMPLE_LIMIT: max distinct values sampled per joinable column
    #   for value-overlap inference (kept low to bound index-time cost).
    JOIN_EDGE_SAMPLE_LIMIT: int = 200
    # ── Catalog description completeness (index-time) ───────────────────
    # The batched description call (8 tables x 60-100 ERP columns) routinely
    # returns a PARTIAL column list, and blank was silently persisted as the
    # column's description. Blank descriptions are what let the model pick
    # between look-alike columns on name alone — e.g. FCLOSESTATUS vs
    # FDOCUMENTSTATUS on erp_t_crm_contract, which two runs answered with two
    # different columns. Backfill re-asks per table for the columns that came
    # back empty, so the catalog ends up carrying the business meaning.
    # CATALOG_DESC_BACKFILL_ENABLED: run the repair pass (one extra LLM call
    #   per incomplete table, bounded below).
    CATALOG_DESC_BACKFILL_ENABLED: bool = True
    # CATALOG_DESC_BACKFILL_MAX_TABLES: cap on tables re-asked per index run
    #   (0 disables). Index time only; nothing runs per user turn.
    CATALOG_DESC_BACKFILL_MAX_TABLES: int = 60
    # ── Schema Graph (runtime presentation + join planning) ─────────────
    # SCHEMA_GRAPH_ENABLED: build a structural SchemaGraph over the catalog-
    #   selected candidate tables and render it through describe_schema /
    #   NLAnswerService (columns, types, keys, row-count estimates, sample
    #   rows, join edges with kind + confidence).
    SCHEMA_GRAPH_ENABLED: bool = False
    # SCHEMA_GRAPH_SQL_VALIDATOR_ENABLED: run the sqlglot structural validator
    #   before executing generated SQL; on failure return available_columns
    #   feedback for one retry.
    SCHEMA_GRAPH_SQL_VALIDATOR_ENABLED: bool = False
    # SCHEMA_VALIDATOR_DID_YOU_MEAN_ENABLED: when the structural validator
    #   rejects a query for unknown tables/columns, include up to 5 fuzzy
    #   "did you mean" suggestions (difflib vs the catalog) plus FK-master
    #   hints so the agent self-corrects in one retry.
    SCHEMA_VALIDATOR_DID_YOU_MEAN_ENABLED: bool = False
    # SCHEMA_GRAPH_AUTOJOIN_THRESHOLD: minimum edge confidence for the agent
    #   to auto-join silently (FK or value-overlap); below this it must ask
    #   (except automation runs, which never ask).
    SCHEMA_GRAPH_AUTOJOIN_THRESHOLD: float = 0.8
    # SCHEMA_GRAPH_SAMPLE_ROWS: sample rows rendered per table in the graph
    #   context block.
    SCHEMA_GRAPH_SAMPLE_ROWS: int = 3
    # SCHEMA_GRAPH_TOKEN_BUDGET: approximate token budget for the rendered
    #   context block (~4 chars/token).
    SCHEMA_GRAPH_TOKEN_BUDGET: int = 1200

    # ── Schema Discovery (2026-08-25) ────────────────────────────────────
    # When describe_schema runs, include this many sample rows per table
    # so the LLM can classify tables by VALUE content (works regardless
    # of arbitrary table/column naming). Set to 0 to disable.
    # Defaults to 2 — enough to see the data shape, small enough not to
    # bloat the schema payload.
    SCHEMA_DESCRIBE_SAMPLE_ROWS: int = 2

    # When True, the data-domain hint block tells the LLM to classify
    # tables by sample-row values (primary), with column patterns as a
    # last-resort hint. Set to False to revert to name-only matching
    # (legacy behavior — fragile for non-English / abbreviated schemas).
    SCHEMA_CLASSIFY_BY_SAMPLE: bool = True

    # ── Entity Master Filter (universal master-first query pattern) ─────
    # ENTITY_MASTER_FILTER_ENABLED: master gate. When True, ALL database-bound
    #   agents get the _ENTITY_MASTER_FILTER_BLOCK prompt section + the cached
    #   "Known Entity Masters" map injected into their system prompt, and the
    #   schema graph renders table_role annotations.
    # TABLE_ROLE_AUTO_CLASSIFY_ENABLED: sub-flag. When True, the catalog
    #   indexer auto-classifies each table's role (entity_master / fact /
    #   dimension / bridge / unknown) using structural heuristics (row count,
    #   column-name patterns, FK relationships) and persists it in
    #   kb_table_meta.table_role.
    # ENTITY_MASTER_MAX_ROW_COUNT: row-count ceiling used by the classifier to
    #   decide whether a table is small enough to be an entity master.
    ENTITY_MASTER_FILTER_ENABLED: bool = False
    TABLE_ROLE_AUTO_CLASSIFY_ENABLED: bool = False
    ENTITY_MASTER_MAX_ROW_COUNT: int = 50000

    # ── Business Semantic Layer (Approach A) ────────────────────────────
    # KG_COVERAGE_PROBE_ENABLED: probe min_date/max_date per table at index
    #   time (5s timeout), stored in kb_table_meta.coverage_json.
    # KG_FRESHNESS_SHORTCIRCUIT_ENABLED: if a relative-date question's window
    #   starts after all relevant tables' max_date, answer immediately with a
    #   plain-language stale-data statement (~1s) instead of scanning the table.
    # KG_BUSINESS_CONTEXT_ENABLED: inject matched approved project_metric
    #   definitions + coverage annotations into the NL2SQL prompt.
    # KG_METRIC_BOOTSTRAP_ENABLED: LLM-propose project_metric rows at index
    #   time (status='proposed'; human approval required before use).
    KG_COVERAGE_PROBE_ENABLED: bool = False
    KG_FRESHNESS_SHORTCIRCUIT_ENABLED: bool = False
    KG_BUSINESS_CONTEXT_ENABLED: bool = False
    KG_METRIC_BOOTSTRAP_ENABLED: bool = False

    # ── Business domains (rich semantic layer) ─────────────────────────────
    # KG_DOMAIN_DISCOVERY_ENABLED: at index time, group catalog tables into
    #   named business domains — Louvain over the FK/join graph + K-Means over
    #   the table embeddings, merged on centroid cosine similarity, named by
    #   one batched LLM call over each cluster's own tables/descriptions/samples.
    # KG_DOMAIN_SCOPING_ENABLED: at question time, restrict the table-selection
    #   TOC to the domains that match the question (only for catalogs above
    #   KG_DOMAIN_SCOPING_MIN_TABLES). Falls back to the full TOC whenever the
    #   domain map is missing or the scope would be implausibly large.
    # KG_DOMAIN_MERGE_THRESHOLD: centroid cosine at or above which a structural
    #   and a semantic cluster are united (0.75 in the schema-discovery pattern).
    # KG_DOMAIN_LOUVAIN_RESOLUTION / KG_DOMAIN_MAX_CLUSTERS: cluster granularity.
    # KG_DOMAIN_MIN_TABLES: domains smaller than this fold into their nearest peer.
    KG_DOMAIN_DISCOVERY_ENABLED: bool = False
    KG_DOMAIN_SCOPING_ENABLED: bool = False
    KG_DOMAIN_MERGE_THRESHOLD: float = 0.75
    KG_DOMAIN_LOUVAIN_RESOLUTION: float = 1.0
    KG_DOMAIN_MAX_CLUSTERS: int = 20
    KG_DOMAIN_MIN_TABLES: int = 2
    KG_DOMAIN_SCOPING_MIN_TABLES: int = 40
    KG_DOMAIN_SCOPING_MIN_SCORE: float = 0.25
    KG_DOMAIN_SCOPING_MAX_TABLES: int = 120

    # ── SemanticStore (one read-only facade over the semantic foundation) ──
    # SEMANTIC_STORE_ENABLED: when on, callers may build a SemanticStore over
    #   the KBs they are already authorised to read. The store is a facade —
    #   it adds no tables and writes nothing — so this only gates the
    #   consumption of existing catalog/glossary/workload rows.
    # SEMANTIC_STORE_WORKLOAD_ENABLED: allow execution telemetry
    #   (nl2sql_query_logs, query_plan_memory) to contribute a RANKING score.
    #   It never grants access and never becomes verified truth; turn this off
    #   to rank on meaning alone.
    # SEMANTIC_STORE_LOG_SCAN_LIMIT: how many telemetry rows a workload scan
    #   reads (bounded — never a full-table scan of the log).
    # SEMANTIC_SOURCE_ROUTING_ENABLED: when an agent has MORE THAN ONE
    #   database bound, rank them for the question and show the shortlist in
    #   the "Bound Data Sources" prompt block. Narrows the choice; the agent
    #   still owns the final pick.
    # SEMANTIC_STORE_GLOSSARY_ENABLED: inject matched human-authored business
    #   vocabulary (approved metrics, glossary terms, catalog aliases) into the
    #   live SQL prompt.
    SEMANTIC_STORE_ENABLED: bool = True
    SEMANTIC_STORE_WORKLOAD_ENABLED: bool = True
    SEMANTIC_STORE_LOG_SCAN_LIMIT: int = 2000
    SEMANTIC_SOURCE_ROUTING_ENABLED: bool = True
    SEMANTIC_STORE_GLOSSARY_ENABLED: bool = True

    # ── OCI NL2SQL parity stages (2026-09-22) ─────────────────────────────
    # NOTE: these default ON by explicit user decision — a deliberate exception
    # to the all-flags-default-False convention. Each flag is a rollback switch
    # for one OCI stage; flag-off restores byte-identical legacy behavior.
    # NL2SQL_SQL_REWRITE_ENABLED: deterministic, semantics-preserving SQL
    #   rewrite pass (limit enforcement on the exploratory path, conservative
    #   predicate push-down, join elimination with uniqueness evidence) before
    #   execution. See services/db/sql_rewriter.py. Rewritten SQL still passes
    #   the unconditional execution gate.
    # NL2SQL_SQL_REWRITE_EXPLAIN_ENABLED: EXPLAIN-plan feedback for the
    #   rewriter (telemetry only; never auto-executes costlier variants).
    #   Default OFF: EXPLAIN round-trips cost latency on the serving path.
    # NL2SQL_QUERY_LOG_RETRIEVAL_ENABLED: retrieve SIMILAR past successful
    #   questions + their SQL from nl2sql_query_logs / query_plan_memory at
    #   plan time and inject them as few-shot grounding into the SQL prompt.
    # NL2SQL_WORKLOAD_MINING_ENABLED: offline Celery job mining the query
    #   logs for frequent joins / common filters / popular tables into
    #   query_workload_stat, which SemanticStore reads as workload evidence.
    # DATA_AGENT_PLANNER_ENABLED: an explicit planning step at the start of
    #   the ask_data_agent sub-loop (intent classification, multi-part
    #   decomposition, tool strategy). Falls back to today's behavior on any
    #   planner failure.
    NL2SQL_SQL_REWRITE_ENABLED: bool = True
    NL2SQL_SQL_REWRITE_EXPLAIN_ENABLED: bool = False
    NL2SQL_QUERY_LOG_RETRIEVAL_ENABLED: bool = True
    NL2SQL_WORKLOAD_MINING_ENABLED: bool = True
    DATA_AGENT_PLANNER_ENABLED: bool = True
    # DATA_AGENT_SWARM_ENABLED (IBM agent-selection / hierarchical mode):
    # route the ask_data_agent sub-pipeline through the SwarmRuntime (own
    # system prompt from the builtin data_agent definition + enforced tool
    # whitelist) instead of the hand-rolled sub-loop. Fail-open: any swarm
    # error falls through to the legacy loop. Default OFF (measure first).
    DATA_AGENT_SWARM_ENABLED: bool = False
    # PROMPT_TOKEN_BUDGET (IBM prompt-optimization, 2026-10-08): approximate
    # token ceiling for the assembled system prompt. get_system_prompt logs
    # every assembly at debug and warns with a per-block breakdown when the
    # assembled prompt exceeds this — block accretion (base + tone + grounding
    # + schema + analyst + entity + enterprise + memory) silently crowds out
    # tool schemas and conversation history otherwise.
    PROMPT_TOKEN_BUDGET: int = 24000
    # SYNTHESIS_REFLEXION_ENABLED (IBM Reflexion, default OFF): one
    # self-critique LLM pass on the synthesized report narrative before it
    # ships (fabrication check, dimension coverage, units/periods). Costs
    # one extra LLM call per report — enable for deliverable-quality
    # deployments; failures are non-fatal (original prose kept).
    SYNTHESIS_REFLEXION_ENABLED: bool = False
    # A2A_API_KEY (IBM agent-protocols #2): server-to-server key for
    # POST /a2a (X-API-Key header). Empty = user-JWT auth only (legacy).
    # Set to a long random string to let machine callers delegate tasks
    # without an interactive login.
    A2A_API_KEY: str = ""
    # RED_TEAM_ENABLED (ADLC P1-4): scheduled adversarial campaigns against
    # published agents every 6h (probes in services/red_team.py). Costs
    # ~len(probes) LLM calls per agent per cycle — enable in production
    # once the eval baseline is stable.
    RED_TEAM_ENABLED: bool = False
    # ── IBM multi-agent system fixes (2026-10-08) ──
    # PLAN_DAG_PARALLEL_ENABLED: execute independent ready plan nodes in
    #   parallel batches (isolated DB sessions per node) instead of strictly
    #   serially. Dependent stages, adaptive checkpoints and error-replan
    #   still run serially. PLAN_DAG_MAX_CONCURRENT bounds each batch.
    PLAN_DAG_PARALLEL_ENABLED: bool = True
    PLAN_DAG_MAX_CONCURRENT: int = 4
    # DELEGATE_MAX_CONCURRENT: bounds delegate_task batch fan-out (was
    #   unbounded asyncio.gather — every sub-agent launched at once).
    DELEGATE_MAX_CONCURRENT: int = 4
    # DELEGATE_CONTEXT_INHERITANCE: inject a compact digest of the parent
    #   conversation into every sub-agent's system prompt (shared knowledge
    #   base — sub-agents no longer start from zero context).
    DELEGATE_CONTEXT_INHERITANCE: bool = True
    # DELEGATE_VERIFY_ENABLED: optional LLM-judge quality gate attached to
    #   sub-agent results before the parent consumes them (one judge call
    #   per delegation — enable when sub-agent quality matters more than
    #   latency).
    DELEGATE_VERIFY_ENABLED: bool = False
    # ── IBM ReAct article fixes (2026-10-08) ──
    # REACT_SIGNAL_NUDGE_ENABLED: after a data-grounding tool returns usable
    #   data, append a system nudge telling the model it may close the loop
    #   (pure budgets otherwise leave it iterating speculatively).
    REACT_SIGNAL_NUDGE_ENABLED: bool = True
    # REACT_SCRATCHPAD_FALLBACK_ENABLED: parse classic ReAct "Action:" text
    #   into a real tool call when the model has no native function calling.
    #   Default False — native tool calling is the primary path.
    REACT_SCRATCHPAD_FALLBACK_ENABLED: bool = False
    # LOOP_POSTMORTEM_ENABLED: scheduled mining of failed agent loops into
    #   AgentMemory "LESSON" rows (failure-side counterpart of q_sql_examples).
    LOOP_POSTMORTEM_ENABLED: bool = False
    # CREDENTIAL_ENCRYPTION_KEY (IBM framework-audit #3): Fernet key (or any
    # passphrase — SHA-256-derived) encrypting stored credentials at rest
    # (MCP OAuth tokens, external-agent bearer tokens). Empty = derived from
    # JWT_SECRET (zero-setup encryption). Rotate carefully: ciphertexts from
    # the old key become undecryptable.
    CREDENTIAL_ENCRYPTION_KEY: str = ""
    # ── Agentic RAG + agentic chunking (2026-10-08) ──
    # RAG_DOC_HYBRID_ENABLED: document KB retrieval uses dense + lexical RRF
    #   hybrid scoring (the hybrid infrastructure existed but was only wired
    #   to the NL2SQL side). Falls back to dense-only on any error.
    RAG_DOC_HYBRID_ENABLED: bool = True
    # RAG_GRADE_ENABLED: LLM grades whether retrieved passages answer the
    #   question; insufficient → one query-rewrite + re-retrieve round
    #   (agentic-RAG self-validation). Costs one judge call per answer.
    RAG_GRADE_ENABLED: bool = True
    # RAG_DECOMPOSE_ENABLED: multi-part questions are split into sub-queries,
    #   retrieved in parallel, and merged (agentic-RAG planning agent).
    RAG_DECOMPOSE_ENABLED: bool = True
    # RAG_AGENTIC_CHUNKING_ENABLED: ingestion uses LLM proposition grouping
    #   with per-chunk title/summary metadata (agentic chunking). Bounded:
    #   docs < 800 tokens skipped, ≤6 LLM windows per doc, ≤64 chunks.
    #   OFF by default — enable per deployment; it multiplies ingest cost.
    RAG_AGENTIC_CHUNKING_ENABLED: bool = False
    # ── RL for agents (2026-10-09, Phases 1-4) ──
    # RL_BANDIT_ENABLED: ε-greedy bandit over loop-level policy decisions
    #   (route/model arms), fed by trajectory rewards. Advisory only.
    RL_BANDIT_ENABLED: bool = True
    # RL_ART_*: OpenPipe/ART GRPO training (Phase 3). The GPU training
    # server runs vLLM + LoRA; the backend only needs these to submit
    # rollouts. Empty = training not configured (the harness raises a
    # clear RuntimeError instead of failing silently).
    RL_ART_SERVER_URL: str = ""
    RL_ART_BASE_MODEL: str = ""          # e.g. Qwen/Qwen3-8B
    RL_ART_MODEL_NAME: str = "zhanlu-agent"
    RL_ART_ROLLOUTS_PER_TASK: int = 4
    RL_ART_MAX_STEPS: int = 20
    RL_ART_BACKEND: str = ""             # optional serverless backend id
    # ── Semantic tool-selection (2026-10-09, IBM tool-calling P0-2) ──
    # When the eligible tool catalog exceeds TOOL_SEARCH_TOP_K schemas,
    # inject only the top-K most relevant for the user message plus an
    # essential set + the search_tools meta-tool (discovery escape hatch).
    TOOL_SEARCH_ENABLED: bool = True
    TOOL_SEARCH_TOP_K: int = 32
    # ── Case-based reasoning (2026-10-09, IBM agent-memory P1-3) ──
    # Successful turns are recorded as episodic task→solution cases and
    # recalled into the system prompt for similar future tasks.
    CBR_RECORDING_ENABLED: bool = True
    CBR_RECALL_ENABLED: bool = True
    # P1-5: repeated episodic cases are distilled into procedural playbooks
    # (scheduled every 24h; mirrors loop_postmortem).
    PROCEDURAL_PROMOTION_ENABLED: bool = True
    # P1-4: ReWOO light mode for research-shaped sub-agent tasks — one
    # planner call + deterministic tool execution + one solver call,
    # instead of an interleaved ReAct round-trip per step. Fail-open to
    # ReAct on any error. Default False (behavior-changing; enable per
    # deployment).
    REWOO_ENABLED: bool = False
    REWOO_MAX_STEPS: int = 6
    # ── Outbound agent identity (2026-10-09, IBM best practice #3) ──
    # Sent as X-Agent-Id + User-Agent on every A2A/ACP/MCP outbound call so
    # remote parties can trace which platform agent contacted them.
    A2A_SELF_AGENT_ID: str = "zhanlu-platform"
    # ── Turn-health monitor (2026-10-09, empty-bubble root-fix layer 3) ──
    # Scheduled DB scan for generic-fallback / apology assistant turns;
    # CRITICAL log when the ratio breaches the threshold.
    TURN_HEALTH_MONITOR_ENABLED: bool = True
    # ── Local embedding fallback (2026-10-09) ──
    # When no EMBEDDING_API_KEY is configured, embeddings fall back to an
    # in-process sentence-transformers model. Default:
    # sentence-transformers/all-MiniLM-L6-v2 (cached, ST-compatible, 384-dim).
    # For stronger Chinese semantics, download BAAI/bge-m3 and set
    # EMBEDDING_LOCAL_MODEL=BAAI/bge-m3. Consumers: semantic memory recall,
    # tool-search, case-based reasoning.
    EMBEDDING_LOCAL_FALLBACK_ENABLED: bool = True
    EMBEDDING_LOCAL_MODEL: str = "sentence-transformers/all-MiniLM-L6-v2"
    # 2026-10-15: EMBEDDING_PROVIDER decides where embeddings come from:
    #   local = ALWAYS the in-process sentence-transformers model — never
    #           any remote API (this deployment: no embeddings API key;
    #           the old code inherited LLM_API_KEY and hammered
    #           api.openai.com with a DeepSeek key → doomed 401 per call).
    #   auto  = API only when a DEDICATED EMBEDDING_API_KEY is set, or an
    #           explicit EMBEDDING_API_BASE was configured; otherwise local.
    #   api   = always call the API (operator override; local on failure).
    EMBEDDING_PROVIDER: str = "auto"
    # ROUTING_FEEDBACK_ENABLED (IBM continuous optimization): when True,
    # routing_feedback.get_suggested_overrides may nudge the FSM-vs-ReAct
    # classifier threshold from measured per-route success/latency. Read
    # only + fail-open; default OFF keeps hand-tuned thresholds.
    ROUTING_FEEDBACK_ENABLED: bool = False

    # ── OCI NL2SQL parity — planning, feedback loop, visualization ────────
    # All four default OFF (the standing convention); each is an independent
    # rollback switch and flag-off restores byte-identical legacy behaviour.
    # NL2SQL_PLANNER_ENABLED: one bounded LLM planning call before retrieval
    #   that resolves the business entity, metric (against the semantic
    #   store's VERIFIED vocabulary), time window and slices. See
    #   services/db/nl2sql_planner.py.
    # NL2SQL_CLARIFY_ON_UNCERTAIN_ENABLED: when the planner resolves the
    #   question with LOW confidence and self-reports ambiguity, return a
    #   structured clarification (metric/time/slice options) instead of
    #   guessing. Requires NL2SQL_PLANNER_ENABLED. Without it the planner's
    #   defaults + assumptions are applied silently.
    # NL2SQL_EXAMPLE_AUTO_PROMOTION_ENABLED: promote VERIFIED successful
    #   Q→SQL runs into q_sql_examples (source='workload', deduped by
    #   sql_hash, capped per datasource) so the few-shot corpus learns how
    #   this business actually asks.
    # NL2SQL_CHART_SUGGESTION_ENABLED: attach a deterministic chart-type
    #   suggestion inferred from result shape to the answer payload. See
    #   services/db/chart_suggestor.py.
    # 2026-10-09: activated (was False) — the article's "Agent Planning"
    # stage: one bounded LLM call, fail-soft to byte-identical legacy
    # behavior, grounded in the semantic store's VERIFIED vocabulary only.
    # Required for the clarify gate and multi-turn state below to function.
    NL2SQL_PLANNER_ENABLED: bool = True
    # 2026-10-09: activated (was False). The planner's structured
    # clarification (metric/time/slice options) is consumed at both call
    # sites (nl_answer_service + delegation_tools) and relayed to the user
    # by the calling agent — the OCI multi-turn clarify behavior.
    NL2SQL_CLARIFY_ON_UNCERTAIN_ENABLED: bool = True
    # P0-1: mined workload hints (join paths + common filters) injected into
    # the SQL prompt; empty for fresh warehouses (nothing mined yet).
    NL2SQL_WORKLOAD_HINTS_ENABLED: bool = True
    # P2-5: EXPLAIN-based scan-size assessment + rewrite-outcome history.
    # Read-only toward the datasource; enable per deployment once EXPLAIN
    # permissions on the warehouse are confirmed.
    NL2SQL_EXPLAIN_FEEDBACK_ENABLED: bool = False
    # 2026-10-09: activated (was False) — deduped + capped per datasource,
    # never overwrites authored rows, only promotes contract-passing runs.
    NL2SQL_EXAMPLE_AUTO_PROMOTION_ENABLED: bool = True
    NL2SQL_EXAMPLE_AUTO_PROMOTION_MAX: int = 100
    # 2026-10-09: activated (was False) — deterministic, additive payload key.
    NL2SQL_CHART_SUGGESTION_ENABLED: bool = True
    # NL2SQL_JOIN_PARTNER_GUARANTEE_ENABLED: when the catalog knows a relation
    #   for a selected table but its partner was NOT selected, pull the best
    #   partner into the selection before the DDL slice is built. Kills the
    #   "model invents a join to a table it never saw" failure on join
    #   questions. Default ON (serving-path accuracy precedent): only ADDS a
    #   table the catalog already vouches for, never removes; bounded (max 2);
    #   flag-off restores the unexpanded selection. See services/db/join_partner.py.
    NL2SQL_JOIN_PARTNER_GUARANTEE_ENABLED: bool = True
    # NL2SQL_COVERAGE_EXPANSION_ENABLED: pull NON-selected catalog tables that
    #   share an entity token with a selected table into the selection (bounded).
    #   Fixes recall failures like "contracts expiring" selecting only the
    #   purchase-contract table while the sales-contract table exists.
    # NL2SQL_COVERAGE_SELF_CHECK_ENABLED: after a successful execution, if the
    #   SQL never touched catalog tables that share an entity token with the
    #   touched tables, run bounded supplementary queries and attach them as
    #   supplementary_snapshots to the answer payload. Measured failure:
    #   "how many contracts expire this month" answered 25 (purchase only)
    #   while 190 sales contracts existed in an untouched sibling table.
    NL2SQL_COVERAGE_EXPANSION_ENABLED: bool = True
    NL2SQL_COVERAGE_SELF_CHECK_ENABLED: bool = True

    # ── Data Agent latency optimizations ───────────────────────────────
    # DATA_AGENT_FASTPATH_ENABLED: when the call targets a single bound
    #   database source, ask_data_agent bypasses the iterative sub-agent
    #   loop (3-5 LLM calls) and runs NLAnswerService directly (2 LLM
    #   calls), falling back to the loop on any failure.
    # SCHEMA_CACHE_TTL_SECONDS: in-process TTL cache for live schema
    #   introspection (list_tables / describe_table / describe_all).
    #   0 disables the cache.
    DATA_AGENT_FASTPATH_ENABLED: bool = False
    SCHEMA_CACHE_TTL_SECONDS: int = 3600

    # ── Schema-shape change detection ──────────────────────────────────────
    # SCHEMA_SHAPE_ENABLED: add the live (table, column) digest to every schema
    #   cache key, so a table/column added to a datasource the agent is already
    #   pointed at is an automatic cache miss instead of staying invisible for
    #   SCHEMA_CACHE_TTL_SECONDS (1h). Also enables catalog-drift reporting.
    #   Off = exactly the pre-existing connection-identity-only behaviour.
    # SCHEMA_SHAPE_TTL_SECONDS: how long one shape probe is reused. The probe is
    #   ONE dialect query fetching every (table, column) pair — measured 0.9-3.6 s
    #   on a 143-table / 2,643-column warehouse over a remote link, against ~24 s
    #   to re-introspect every table. This TTL is therefore the upper bound on how
    #   long a schema change can go unnoticed, and it is deliberately far below the
    #   1 h schema TTL. 0 = probe on every lookup, negative = never probe.
    SCHEMA_SHAPE_ENABLED: bool = True
    SCHEMA_SHAPE_TTL_SECONDS: int = 300

    # ── Demo/test source guard ─────────────────────────────────────────────
    # Comma-separated substrings (case-insensitive) matched against bound
    # KnowledgeBase name + database_name. When a conversation has BOTH a
    # demo-marked source AND a real (non-demo) database source, the demo
    # source is excluded from the agent's bound set so it can never build a
    # deliverable from demo data (user-visible fabrication). Demo-only
    # workspaces are unaffected. Empty string disables the guard.
    DEMO_SOURCE_MARKERS: str = "demo,test,sample,e2e,sandbox,staging"

    # Layer-2 provenance guard on the artifact pipeline (backstop to
    # DEMO_SOURCE_MARKERS): when an artifact's cited source is demo-marked
    # while a real source was bound to the agent, "reject" raises before the
    # artifact is persisted, "warn" persists but tags the payload with
    # _provenance_warning, "off" disables the check entirely.
    ARTIFACT_PROVENANCE_GUARD: str = "reject"

    # ── Hierarchical LLM Configuration ───────────────────────────────────
    # Master switch for admin-managed LLM catalog + project/agent model
    # binding + locked-badge UX.  When False, the system behaves exactly as
    # today (user model picker, single LLM_API_KEY/LLM_BASE_URL).
    # Default True: feature is available out of the box. Set to False
    # via env HIERARCHICAL_LLM_ENABLED=false to roll back to legacy
    # single-LLM behavior.
    HIERARCHICAL_LLM_ENABLED: bool = True

    # Fernet key for encrypting llm_models.api_key at rest.
    # Generate with: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    LLM_CRYPTO_KEY: str = ""

    # ── Unified Agent Harness (P1) ──────────────────────────────────────
    # Master switch: when True, SwarmRuntime _run_sub_agent
    # delegate to the AgentRunOrchestrator with persisted run records.
    AGENT_HARNESS_ENABLED: bool = False

    # Phase-2 fan-out: when BOTH this and AGENT_HARNESS_ENABLED are True,
    # ask_* tools accept an optional ``async: true`` param that returns a
    # run_id immediately instead of blocking (fire-and-collect pattern).
    DELEGATION_ASYNC_ENABLED: bool = False

    # ── Swarm fan-out pool (roadmap item 6) ────────────────────────────
    # Bounded parallel executor for swarm_orchestrate. Budgets gate NEW
    # spawns only (in-flight tasks complete); skipped tasks are reported
    # as failed results so the lead's summary still accounts for them.
    SWARM_MAX_CONCURRENT: int = 8        # max simultaneous workers (0 = unlimited)
    SWARM_MAX_COST_USD: float = 0.0      # cost cap per orchestration (0 = unlimited)
    SWARM_MAX_WALL_S: float = 900.0      # wall-clock cap per orchestration (0 = unlimited)

    # ── MCP client (2026-09-16) ─────────────────────────────────────────
    # Per-call timeout for external MCP tool invocations. Without a bound, a
    # hung MCP server blocked the ReAct loop until the turn's wall-clock
    # budget killed the whole turn — the agent lost all its work instead of
    # failing one tool call.
    MCP_CALL_TIMEOUT_S: float = 60.0
    # Liveness probe timeout used before handing a *cached* MCP session to an
    # agent. A failed ping forces a reconnect rather than surfacing a dead
    # connection as a tool error.
    MCP_HEALTHCHECK_TIMEOUT_S: float = 5.0
    # TTL for cached MCP connections (seconds).
    MCP_SESSION_TTL_S: float = 600.0
    # Publish each connected MCP server's tools to the agent tool registry
    # (one entry per tool, named mcp__<server>__<tool>). Off means a connected
    # server is probeable from the UI but no agent can call its tools.
    MCP_TOOLS_ENABLED: bool = True

    # ── P2 Durability & Observability ────────────────────────────────────
    # When AGENT_HARNESS_ENABLED + AGENT_TRACING_ENABLED are both True,
    # the orchestrator loop opens OTEL spans for each iteration and each
    # tool call.  Requires TRACING_PROVIDER != "noop" to produce output.
    AGENT_TRACING_ENABLED: bool = False

    # When AGENT_HARNESS_ENABLED + AGENT_CHECKPOINTING_ENABLED are both
    # True, the orchestrator persists an AgentRunStep row on every
    # llm_call / tool_call event so crashed queued runs can resume.
    AGENT_CHECKPOINTING_ENABLED: bool = False

    # ── P3 Deep Research & Planning ──────────────────────────────────────
    # Enables the reflexion loop: intent_planner → harness.run →
    # self_critic → verification_stop → reflexion → replan.
    # Requires AGENT_HARNESS_ENABLED.
    RESEARCH_LOOP_ENABLED: bool = False

    # Enables the deep-research mode with parallel sub-agent fan-out.
    # Requires AGENT_HARNESS_ENABLED + DELEGATION_ASYNC_ENABLED +
    # RESEARCH_LOOP_ENABLED.
    DEEP_RESEARCH_MODE_ENABLED: bool = False

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
    # Status vocabulary enforcement: when enabled, _ensure_schema adds a
    # DB-level CHECK constraint on automation_tasks.status so non-canonical
    # values (e.g. "running" written by an LLM tool call) fail at write
    # time instead of being silently skipped by the dispatcher. Default
    # False so a legacy non-canonical row can never block startup.
    AUTOMATION_STATUS_CHECK_CONSTRAINT_ENABLED: bool = False

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
    # 2026-10-10: 120s killed real data-munging scripts ("timed out after
    # 120s"); 300s aligns with the SANDBOX env var default (300).
    SANDBOX_DEFAULT_TIMEOUT_SECONDS: int = 300
    SANDBOX_DEFAULT_MEMORY_MB: int = 1024
    SANDBOX_DEFAULT_CPUS: float = 1.0
    SANDBOX_DEFAULT_PIDS_LIMIT: int = 128
    SANDBOX_ALLOW_DOCKER_SOCKET_ONLY_IN_WORKER: bool = True
    SANDBOX_TMP_ROOT: str = "/tmp/zhanlu_sandbox"

    # ── LLM proxy for skill-driven sandbox jobs ─────────────────────────
    # The sandbox container runs --network none.  To let an LLM inside
    # the container plan and generate documents, we mount a Unix-socket
    # LLM proxy (see app/services/sandbox/llm_proxy.py) into the
    # container.  This is the only path the container has to the LLM;
    # no TCP/IP network is ever opened to the container.
    # SANDBOX_LLM_PROXY_SOCKET — host-side Unix socket path the proxy
    #   listens on.  The container sees this path bind-mounted at
    #   SANDBOX_LLM_PROXY_SOCKET_IN_CONTAINER.
    # SANDBOX_LLM_PROXY_ENABLED — master switch.  When False, the
    #   container is started WITHOUT the proxy mount and skill-driven
    #   generation immediately falls back to the deterministic
    #   generator (no LLM call attempted).
    SANDBOX_LLM_PROXY_ENABLED: bool = True
    SANDBOX_LLM_PROXY_SOCKET: str = "/var/run/zhanlu-llm-proxy.sock"
    SANDBOX_LLM_PROXY_SOCKET_IN_CONTAINER: str = "/var/run/llm-proxy.sock"
    # Model the in-container client requests by default.  Must be in
    # the proxy's allowlist (see llm_proxy.ALLOWED_MODELS).
    SANDBOX_LLM_PROXY_MODEL: str = "gpt-4o-mini"
    # Per-job resource budget for skill-driven runs (vs the lighter
    # default 1g/120s).  Skill-driven jobs spawn Node.js + (sometimes)
    # Chromium for html2pptx, so they need more headroom.
    SANDBOX_SKILL_MEMORY_MB: int = 2048
    SANDBOX_SKILL_TIMEOUT_SECONDS: int = 240

    # ── B3: Skill safety scanner ───────────────────────────────────────
    SKILL_SCAN_ENABLED: bool = True            # master switch (warn-only)

    # ── A1: Distributed tracing ─────────────────────────────────────────
    TRACING_PROVIDER: str = "noop"            # "noop" | "otel"
    OTEL_EXPORTER_OTLP_ENDPOINT: str = ""      # standard OTEL env var
    OTEL_SERVICE_NAME: str = "zhanlu-backend"
    TRACE_SPAN_SAMPLING_RATE: float = 1.0     # 1.0 = all spans; lower = sampled
    TRACE_INCLUDE_TOOL_INPUT: bool = False    # never send raw tool args to traces
    TRACE_METRICS_ENABLED: bool = False       # in-process rollup for Admin dashboard
    AUTHZ_PROVIDER: str = "rbac"              # "rbac" | "none"

    # ── B4: Computer use (managed-browser facade) ─────────────────────
    # computer_use tool over the agent-browser CLI. OFF by default — the
    # handler returns the legacy "not implemented" stub until enabled.
    COMPUTER_USE_ENABLED: bool = False
    # Per-conversation action budget; `close` resets it.
    COMPUTER_USE_MAX_STEPS: int = 20
    # Reserved for a future engine launch config (the agent-browser CLI
    # has no --viewport flag yet).
    COMPUTER_USE_VIEWPORT: str = "1280,800"
    # Idle session expiry in seconds (one shared Chromium per conversation).
    COMPUTER_USE_SESSION_TTL_S: int = 1800
    # URL allowlist for computer_use `open` — JSON array of hosts and/or
    # URL prefixes (e.g. ["example.com", "https://docs.example.com/a"]).
    # Empty = SSRF guard only.
    COMPUTER_USE_ALLOWED_URLS: list[str] = []

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
    def design_system_path(self) -> Path:
        """Root dir for persisted design systems: ``{GENERATED_DIR}/design-system``."""
        p = Path(self.GENERATED_DIR) / "design-system"
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
