"""UserSetting model — 50+ configuration fields covering UI, security, compliance, monitoring, cost, infrastructure."""

from sqlalchemy import String, Integer, Float, Boolean, JSON
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import TimestampedBase


class UserSetting(TimestampedBase):
    __tablename__ = "user_settings"

    # --- UI / Localization ---
    language: Mapped[str | None] = mapped_column(String(10), nullable=True, default="zh")
    timezone: Mapped[str | None] = mapped_column(String(100), nullable=True)
    date_format: Mapped[str | None] = mapped_column(String(20), nullable=True, default="YYYY-MM-DD")

    # --- Model Configuration ---
    context_length: Mapped[int | None] = mapped_column(Integer, nullable=True)
    default_model: Mapped[str | None] = mapped_column(String(255), nullable=True)
    fallback_model: Mapped[str | None] = mapped_column(String(255), nullable=True)
    max_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    temperature: Mapped[float | None] = mapped_column(Float, nullable=True)

    # --- Features ---
    file_upload_enabled: Mapped[bool | None] = mapped_column(Boolean, nullable=True, default=True)
    session_retention_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    voice_interaction_enabled: Mapped[bool | None] = mapped_column(Boolean, nullable=True, default=False)
    voice_language: Mapped[str | None] = mapped_column(String(20), nullable=True)

    # --- Security ---
    sso_enabled: Mapped[bool | None] = mapped_column(Boolean, nullable=True, default=False)
    mfa_required: Mapped[bool | None] = mapped_column(Boolean, nullable=True, default=False)
    rbac_mode: Mapped[str | None] = mapped_column(String(20), nullable=True, default="flat")
    session_timeout_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    password_min_length: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # --- Quotas ---
    compute_quota: Mapped[int | None] = mapped_column(Integer, nullable=True)
    kb_storage_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    agent_call_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    task_count_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    gpu_quota: Mapped[int | None] = mapped_column(Integer, nullable=True)
    api_rate_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # --- Data Protection ---
    data_masking: Mapped[bool | None] = mapped_column(Boolean, nullable=True, default=False)
    pii_detection: Mapped[bool | None] = mapped_column(Boolean, nullable=True, default=False)
    encryption_enabled: Mapped[bool | None] = mapped_column(Boolean, nullable=True, default=True)
    third_party_whitelist: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    ip_whitelist: Mapped[str | None] = mapped_column(String(1000), nullable=True)

    # --- Audit & Compliance ---
    audit_logging: Mapped[bool | None] = mapped_column(Boolean, nullable=True, default=True)
    audit_retention_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    compliance_mode: Mapped[str | None] = mapped_column(String(20), nullable=True, default="standard")
    data_residency: Mapped[str | None] = mapped_column(String(100), nullable=True)
    data_quality_check: Mapped[bool | None] = mapped_column(Boolean, nullable=True, default=False)
    lineage_tracking: Mapped[bool | None] = mapped_column(Boolean, nullable=True, default=False)
    data_retention_policy: Mapped[str | None] = mapped_column(String(500), nullable=True)

    # --- Monitoring ---
    monitoring_enabled: Mapped[bool | None] = mapped_column(Boolean, nullable=True, default=True)
    log_level: Mapped[str | None] = mapped_column(String(20), nullable=True, default="info")
    tracing_enabled: Mapped[bool | None] = mapped_column(Boolean, nullable=True, default=False)
    alerting_enabled: Mapped[bool | None] = mapped_column(Boolean, nullable=True, default=False)

    # --- Cost Management ---
    monthly_budget: Mapped[float | None] = mapped_column(Float, nullable=True)
    cost_alert_threshold: Mapped[float | None] = mapped_column(Float, nullable=True)
    usage_analytics: Mapped[bool | None] = mapped_column(Boolean, nullable=True, default=True)
    api_key_rotation_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    webhook_url: Mapped[str | None] = mapped_column(String(500), nullable=True)

    # --- Infrastructure ---
    sdk_access_enabled: Mapped[bool | None] = mapped_column(Boolean, nullable=True, default=False)
    cluster_region: Mapped[str | None] = mapped_column(String(100), nullable=True)
    auto_scaling: Mapped[bool | None] = mapped_column(Boolean, nullable=True, default=False)
    maintenance_window: Mapped[str | None] = mapped_column(String(255), nullable=True)
    backup_schedule: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # --- Smart Alerts (adaptive thresholds + notification preferences) ---
    # v1 user-facing knobs. All default to the quietest/safest behavior so
    # the master SMART_ALERTS_ENABLED flag alone decides whether the feature
    # is active; individual knobs tune the noise floor per user.
    alert_min_severity: Mapped[str | None] = mapped_column(
        String(10), nullable=True, default="low"
    )
    alert_muted_products: Mapped[list | None] = mapped_column(
        JSON, nullable=True, default=list
    )
    alert_channels: Mapped[list | None] = mapped_column(
        JSON, nullable=True, default=lambda: ["in_app"]
    )
    alert_threshold_multiplier: Mapped[float | None] = mapped_column(
        Float, nullable=True, default=1.0
    )
    alert_quiet_hours: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    alert_recommendations_enabled: Mapped[bool | None] = mapped_column(
        Boolean, nullable=True, default=True
    )
    alert_webhook_secret: Mapped[str | None] = mapped_column(String(64), nullable=True)
