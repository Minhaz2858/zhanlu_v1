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

    # ── EDIA Market Dashboard — read-only mirror of EDIA's MySQL warehouse.
    # Empty = dashboard routes return 503 (the rest of zhanlu is unaffected).
    # Example: mysql+pymysql://root:PASSWORD@10.10.10.49:3306/aipdp_data_warehouse_prod
    EDIA_MYSQL_URL: str = ""

    @property
    def is_edia_enabled(self) -> bool:
        """True when EDIA_MYSQL_URL is set (the dashboard will at least try)."""
        return bool(self.EDIA_MYSQL_URL)

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

    # Models that ignore or reject a caller-supplied ``temperature`` value.
    # Reasoning models like Moonshot kimi-k2.6 only accept temperature=1, and
    # OpenAI o1/o3 reject the field outright. For these, the temperature key is
    # omitted from chat/completions requests so the provider's own default is
    # used (otherwise a valid key surfaces as a misleading HTTP 400). Comma-
    # separated substrings matched case-insensitively against the model id.
    LLM_FIXED_TEMPERATURE_MODELS: str = "kimi-k2,o1,o3"

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
    AGENT_MAX_ITERATIONS: int = 100

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

    # ── NLU / Intent (Part 2 Gap Analysis) ────────────────────────────
    EDIA_INTENT_PLANNER_ENABLED: bool = False
    QUERY_REWRITE_ENABLED: bool = False

    # ── Eval Pipeline (Part 2 Gap Analysis) ───────────────────────────
    EVAL_PIPELINE_ENABLED: bool = False
    EVAL_SAMPLE_RATE: float = 0.1
    EVAL_MAX_SAMPLES_PER_RUN: int = 50
    PROMPT_AB_TEST_ENABLED: bool = False

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

    # ── PPT generation pipeline (multi-stage deck exporter) ──────────────
    # All four flags default OFF so the existing report-card renderer path is
    # byte-for-byte unchanged until a deployment opts in.  When enabled they
    # add: LLM deck planning (content structuring), deterministic smart routing
    # (structured vs sandbox html2pptx), and a one-shot LLM copy-polish pass.
    # NOTE: the P0 audit/repair loop (audit_deck.py → repair_deck.py) is a
    # pre-existing always-on safety net gated by ZHANLU_AUDIT_REPAIR_CYCLES and
    # is NOT toggled here — PPT_AUDIT_ENABLED is reserved for an explicit
    # opt-out/opt-in override in future releases.
    PPT_DECK_PLANNER_ENABLED: bool = False
    PPT_SMART_ROUTER_ENABLED: bool = False
    PPT_LLM_POLISH_ENABLED: bool = False
    PPT_AUDIT_ENABLED: bool = False
    # Phase 4: intent-driven deck profiles (data_report / executive_brief /
    # pitch_narrative / periodic_review).  When OFF, every deck uses the
    # historical data_report structure (backward compatible, zero risk).
    DECK_PROFILES_ENABLED: bool = False
    # Phase 1C: deterministic per-slide deck EDITING tools (edit_slide /
    # add_slide / restyle_deck / update_chart / remove_slide / reorder_slide).
    # Routes "edit my deck"-style messages to the six deck-edit tools while
    # keeping "regenerate / redo / from scratch" on the full re-generation path.
    DECK_EDIT_ROUTING_ENABLED: bool = False
    # Phase 1B: layout-engine / data-table tuning (all defaults safe-off).
    LAYOUT_ENGINE_TEMPLATE_PATH: str = ""
    PPTX_DARK_THEME_PATH: str = ""
    PPTX_TOP_TABLE_ROWS: int = 8
    PPTX_DATA_TABLE_HIGHLIGHT_TOP: int = 3
    PPTX_VISUAL_VERIFY_ENABLED: bool = False

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
    # Enable quality eval in non-FSM paths (ReAct v2, v3 non-FSM streaming).
    # When True (default), both ReAct (v2) and v3 streaming non-FSM paths
    # run the standalone evaluate_response_quality() after the response is
    # generated.  Set False to restrict quality eval to FSM-only.
    QUALITY_EVAL_ALL_PATHS: bool = True

    # Universal Self-Evaluation & Re-Planning loop (all agents, all data
    # sources). When enabled, the agent loop verifies its draft answer against
    # the user's request at the synthesis boundary (deterministic detectors +
    # one optional LLM strict-inspector call) and re-plans up to
    # SELF_EVAL_MAX_REPLANS times before answering with a gap disclosure.
    # Default OFF: flag-off keeps behavior byte-identical.
    SELF_EVAL_REPLAN_ENABLED: bool = False
    # Sub-flag: run the single LLM strict-inspector call at the synthesis
    # boundary. When False, only deterministic detectors run.
    SELF_EVAL_LLM_GATE_ENABLED: bool = True
    # Max re-plan nudges per user turn before escalating to IMPOSSIBLE/disclose.
    SELF_EVAL_MAX_REPLANS: int = 3
    # Timeout for the synchronous LLM evaluator call (mirrors quality_eval).
    SELF_EVAL_LLM_GATE_TIMEOUT_S: float = 15.0

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
    # Domain-signal overlay (naphtha→C5 elasticities + seasonal rules).
    # Enable per A/B validation (tests/test_domain_signals_ab.py).
    FORECAST_DOMAIN_SIGNALS_ENABLED: bool = False
    # LLM analyst brief writer. When False, the deterministic template
    # brief is used everywhere (no LLM calls).
    FORECAST_ANALYST_LLM_ENABLED: bool = False

    # ── Ecisco BI feature flags (EDIA 5.1.2 → zhanlu migration) ─────────
    # Master switch for EDIA-specific delegated tools in the ecisco_bi_assistant
    # agent.  When False, the agent runs as a plain general assistant with
    # standard zhanlu tools only.
    ECISCO_BI_AGENT_ENABLED: bool = False

    # Per-module gates — each enables one category of EDIA ported services.
    # All default False so the agent is safe to register in every deployment;
    # enable progressively as phases 5A→5F are completed and tested.
    ECISCO_BI_KNOWLEDGE_GRAPH_ENABLED: bool = False   # 5A
    ECISCO_BI_PRICING_ENABLED: bool = False           # 5B
    ECISCO_BI_PERCEPTION_ENABLED: bool = False        # 5C
    ECISCO_BI_INTEL_ENHANCED_ENABLED: bool = False    # 5D
    ECISCO_BI_ERP_WRITEBACK_ENABLED: bool = False     # 5F
    ECISCO_BI_ALERTS_ENABLED: bool = False           # 5G — proactive market alert detection + UI
    ECISCO_BI_SCHEMA_DEPRECATION_ENABLED: bool = True  # F_PAEZ_JC over material_model hint for ecisco_bi_assistant

    # ── Supply Chain Snapshot (per-product ERP panel under RegionalTable) ──
    SUPPLY_CHAIN_SNAPSHOT_ENABLED: bool = False      # Gate for GET /market-dashboard/supply-chain/{product_id}

    # ── CEO Morning Digest (built on top of 5G alerts) ────────────────
    ALERT_DIGEST_ENABLED: bool = False              # Master gate for CEO morning digest
    ALERT_DIGEST_LLM_ENABLED: bool = True           # LLM narrator (default ON, template is fallback)
    ALERT_DIGEST_HOUR_UTC: int = 0                  # Digest generation hour (0 = 08:00 CST)
    ALERT_DIGEST_TOP_N: int = 5                     # Top-N items in digest

    # ── Market alert detection thresholds (tunable without code edits) ──
    # 7-day % change required to trigger a price_spike / price_drop alert,
    # and the magnitude for a "high" severity classification. Defaults match
    # the historical hardcoded constants in alert_service.py.
    ALERT_PRICE_SPIKE_THRESHOLD: float = 3.0        # +3% 7d change → price_spike
    ALERT_PRICE_DROP_THRESHOLD: float = -3.0        # -3% 7d change → price_drop
    ALERT_HIGH_SEVERITY_THRESHOLD: float = 6.0      # >= |6|% → high severity

    # ── Smart Alerts (adaptive thresholds + context + recommendations) ──
    # Master gate. When False, alert detection is byte-identical to the static
    # ±3%/±6% thresholds above. When True, per-product LLM-judged baselines
    # adapt the trigger, alerts carry ¥/day impact + templated recommendations,
    # and user preferences + webhook delivery are honored.
    SMART_ALERTS_ENABLED: bool = False               # Master gate for intelligent alerts
    ALERT_BASELINE_LLM_ENABLED: bool = True          # LLM judges per-product weekly volatility baseline
    ALERT_BASELINE_TTL_SECONDS: int = 604800         # Baseline cache TTL (7 days)
    ALERT_BASELINE_DEFAULT_PCT: float = 2.5          # Seeded baseline when LLM unavailable
    ALERT_IMPACT_ENABLED: bool = True                # ¥/day business impact on each alert
    ALERT_RECOMMENDATIONS_ENABLED: bool = True       # Templated "why" + suggested next steps
    ALERT_WEBHOOK_ENABLED: bool = True               # Webhook delivery channel (HMAC-signed)
    ALERT_HIGH_SEVERITY_FLOOR: float = 6.0           # Floor for "high" severity in adaptive mode

    # ── Unified Forecasting Service (Layer 1 backend facade) ────────────
    # Thin facade ForecastingAgentService over the 4 fragmented forecasting
    # data paths. When False, routers/tools keep calling the individual
    # services directly; flip to True to route them through the unified API.
    UNIFIED_FORECASTING_SERVICE_ENABLED: bool = False

    # ── Weekly Summary & Forecast Digest ────────────────────────────────
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
    ECISCO_BI_RECIPE_LEARNING_ENABLED: bool = False   # Layer 1
    ECISCO_BI_USER_PROFILE_ENABLED: bool = False      # Layer 3
    ECISCO_BI_RESPONSE_CACHE_ENABLED: bool = False    # Layer 2
    ECISCO_BI_FEEDBACK_ENABLED: bool = False          # Phase C wiring
    ECISCO_BI_AUTOMATION_FASTPATH_ENABLED: bool = False  # DISABLED — automation only via UI "New Automation Task" button

    # ── Role-based personalization ───────────────────────────────────────
    # ROLE_PERSONALIZATION_ENABLED: inject the user's free-text
    #   role_descriptions into the agent system prompt as a
    #   [User Role Context] block so answers are tailored to the user's role(s).
    # ROLE_FEEDBACK_THROTTLE: show the "Relevant to your role?" 1-5 rating on
    #   every Nth assistant message (client-side throttle, N >= 1).
    ROLE_PERSONALIZATION_ENABLED: bool = False
    ROLE_FEEDBACK_THROTTLE: int = 3

    # ── Project Knowledge Graph / Semantic Catalog ───────────────────────
    # Phase 1 flags (default OFF — roll out after catalog indexer + schema
    # linker are tested against the EDIA warehouse).
    # SEMANTIC_CATALOG_ENABLED: background indexer per KB (tables, columns,
    #   FK, LLM descriptions, Chroma embeddings).
    # SCHEMA_LINKING_ENABLED: query-time RRF retrieval + join-path expansion
    #   into a curated ~800-token DDL slice for NLAnswerService/Data Agent.
    SEMANTIC_CATALOG_ENABLED: bool = True
    SCHEMA_LINKING_ENABLED: bool = False
    # NL2SQL_SHADOW_MODE_ENABLED: run the governed nl2sql validator/policy
    #   alongside the live agent data path (NLAnswerService), logging
    #   divergence to nl2sql_query_logs. The served result is NEVER altered
    #   in shadow mode; the serving switch happens only on eval evidence.
    NL2SQL_SHADOW_MODE_ENABLED: bool = False
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
    # Master switch: when True, SwarmRuntime and EDIA _run_sub_agent
    # delegate to the AgentRunOrchestrator with persisted run records.
    AGENT_HARNESS_ENABLED: bool = False

    # Phase-2 fan-out: when BOTH this and AGENT_HARNESS_ENABLED are True,
    # ask_* tools accept an optional ``async: true`` param that returns a
    # run_id immediately instead of blocking (fire-and-collect pattern).
    EDIA_DELEGATION_ASYNC_ENABLED: bool = False

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
    # Requires AGENT_HARNESS_ENABLED + EDIA_DELEGATION_ASYNC_ENABLED +
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
    SANDBOX_DEFAULT_TIMEOUT_SECONDS: int = 120
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
