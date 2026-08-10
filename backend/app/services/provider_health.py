"""Provider health tracking with circuit-breaker pattern.

Tracks per-provider health (consecutive failures, average latency, last success)
and implements a circuit-breaker: after ``CIRCUIT_BREAKER_THRESHOLD`` consecutive
failures the provider is marked unhealthy for ``CIRCUIT_BREAKER_COOLDOWN_S``.
After the cooldown period, one probe request is allowed; if it succeeds the
provider recovers; if it fails the cooldown resets.

Thread-safe: uses ``threading.Lock`` for the in-process state. Suitable for the
sync+async hybrid that ``llm_service.py`` uses.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

CIRCUIT_BREAKER_THRESHOLD: int = 3
CIRCUIT_BREAKER_COOLDOWN_S: float = 30.0


@dataclass
class _ProviderState:
    """Per-provider health state (not for external use)."""

    name: str
    consecutive_failures: int = 0
    total_successes: int = 0
    total_failures: int = 0
    total_latency_ms: float = 0.0
    last_success_at: float = 0.0
    last_failure_at: float = 0.0
    circuit_open_at: float = 0.0  # monotonic timestamp when breaker opened
    probe_allowed: bool = False  # True when one probe request is allowed


_state_lock = threading.RLock()
_providers: dict[str, _ProviderState] = {}


def _ensure_state(provider_name: str) -> _ProviderState:
    with _state_lock:
        if provider_name not in _providers:
            _providers[provider_name] = _ProviderState(name=provider_name)
        return _providers[provider_name]


def is_healthy(provider_name: str) -> bool:
    """Return True if the provider is currently healthy or in probe mode."""
    st = _ensure_state(provider_name)
    with _state_lock:
        if st.consecutive_failures < CIRCUIT_BREAKER_THRESHOLD:
            return True
        now = time.monotonic()
        if now - st.circuit_open_at < CIRCUIT_BREAKER_COOLDOWN_S:
            # Cooldown not expired — only allow one probe
            if not st.probe_allowed:
                return False
            st.probe_allowed = False
            logger.info("Provider '%s' — probe request allowed", provider_name)
            return True
        # Cooldown expired — allow probe
        st.circuit_open_at = now
        st.consecutive_failures = 0  # reset for probe
        logger.info("Provider '%s' — cooldown expired, probe allowed", provider_name)
        return True


def record_success(provider_name: str, latency_ms: float) -> None:
    """Record a successful LLM call for the provider."""
    st = _ensure_state(provider_name)
    with _state_lock:
        st.consecutive_failures = 0
        st.total_successes += 1
        st.total_latency_ms += latency_ms
        st.last_success_at = time.monotonic()
        st.probe_allowed = False


def record_failure(provider_name: str, error_type: str = "") -> None:
    """Record a failed LLM call for the provider."""
    st = _ensure_state(provider_name)
    with _state_lock:
        st.consecutive_failures += 1
        st.total_failures += 1
        st.last_failure_at = time.monotonic()
        if st.consecutive_failures >= CIRCUIT_BREAKER_THRESHOLD and st.circuit_open_at == 0.0:
            st.circuit_open_at = time.monotonic()
            st.probe_allowed = False
            logger.warning(
                "Provider '%s' circuit OPEN after %d consecutive failures",
                provider_name, st.consecutive_failures,
            )


def average_latency_ms(provider_name: str) -> float:
    """Return average latency across all successful calls, or 0.0."""
    st = _ensure_state(provider_name)
    with _state_lock:
        if st.total_successes == 0:
            return 0.0
        return st.total_latency_ms / st.total_successes


def select_provider(providers) -> object:
    """Select the healthiest provider from an ordered list.

    Filters out unhealthy providers (circuit open, no probe allowed),
    then picks the one with the lowest average latency among healthy
    candidates. If no provider is healthy, returns the first provider
    on the list as a last resort.
    """
    from app.services.llm_service import LLMProvider

    # Build dict name -> provider for fast lookup
    provider_map: dict[str, LLMProvider] = {}
    ordered_names: list[str] = []
    for p in providers:
        provider_map[p.name] = p
        ordered_names.append(p.name)

    # Filter healthy
    healthy = [name for name in ordered_names if is_healthy(name)]

    if healthy:
        # Prefer lowest latency among healthy
        best = min(healthy, key=lambda n: average_latency_ms(n))
        return provider_map[best]

    # All unhealthy — return first as last resort
    if ordered_names:
        return provider_map[ordered_names[0]]
    return providers[0] if providers else None


def health_summary() -> dict:
    """Return a summary dict for monitoring dashboards."""
    with _state_lock:
        return {
            name: {
                "consecutive_failures": st.consecutive_failures,
                "total_successes": st.total_successes,
                "total_failures": st.total_failures,
                "avg_latency_ms": round(st.total_latency_ms / max(st.total_successes, 1), 1),
                "healthy": is_healthy(name),
                "circuit_open": st.circuit_open_at > 0
                and (time.monotonic() - st.circuit_open_at) < CIRCUIT_BREAKER_COOLDOWN_S,
            }
            for name, st in _providers.items()
        }


__all__ = [
    "is_healthy",
    "record_success",
    "record_failure",
    "average_latency_ms",
    "select_provider",
    "health_summary",
]
