"""Profile registry for the comprehensive_data pipeline.

A Profile is a frozen dataclass that captures everything the
profiler / executor / synthesizer need to behave differently per
question shape (``enterprise`` for executive-style business reports,
``market`` for institutional-grade market overview decks, ...).

Why a registry (not conditionals everywhere):
- Adding a new profile is one file under ``profiles/<name>.py``.
- The new profile is auto-loaded on first ``get_profile(name)`` call;
  no restart of the orchestrator required.
- Profiles are pure data + strings (no executable code), so unit
  tests can construct profiles directly without mocking.

Adding a profile
----------------
1. Create ``profiles/<name>.py`` exposing a ``build() -> Profile``
   that returns a fully-populated ``Profile`` instance.
2. Add an entry to ``_PROFILE_BUILDERS`` below.
3. (Optional) Add a flag gate in ``config.py`` for safe rollout.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Mapping, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Profile:
    """A complete behavior pack for one question shape.

    Attributes:
        name:        Canonical profile id (e.g. ``"enterprise"``, ``"market"``).
        label:       Human-readable label (used in logs and payload metadata).
        facet_spec:  Tuple of dimension / hint names the profiler should
                     pre-seed for this profile. Optional; the LLM profiler
                     may add or remove facets as it sees fit, but having a
                     named seed list dramatically improves coverage when
                     the user message is short.
        section_schema: Ordered tuple of section headings that
                        ``synthesizer`` should render. Empty tuple means
                        "use the default enterprise schema".
        profiler_prompt: Optional system-prompt body for the profiler LLM
                         call. Empty string means "use the default enterprise
                         profiler prompt in ``profiler.py:_PROFILER_PROMPT``".
        synthesizer_prompt: Optional system-prompt body for the synthesizer
                            LLM call. Empty string means "synthesizer runs in
                            deterministic mode (no extra LLM call)".
        facet_to_dimension: Maps ``facet_id`` (the id the LLM emits on
                            each facet) to a stable *coverage dimension*
                            name. The synthesizer emits a payload with
                            ``coverage_dimensions: List[str]`` derived
                            from this map × facet availability.
                            Use ``resolve_dimension(profile, facet_id)``
                            rather than this dict directly when you want
                            prefix-matching fallback behaviour.
    """

    name: str
    label: str
    facet_spec: Tuple[str, ...] = ()
    section_schema: Tuple[str, ...] = ()
    profiler_prompt: str = ""
    synthesizer_prompt: str = ""
    facet_to_dimension: Mapping[str, str] = field(default_factory=dict)

    def dimension_for_facet(self, facet_id: str) -> Optional[str]:
        """Return the coverage dimension a successful facet contributes to.

        Returns ``None`` when this profile doesn't map the facet explicitly —
        the caller (synthesizer) should then treat the facet as a unique
        coverage dimension (``f"{profile.name}:{facet_id}"``).
        """
        return self.facet_to_dimension.get(facet_id)


def _load_profile(builder_name: str):
    """Lazy-import a profile builder.

    Defers the import so an optional profile with a missing/incompatible
    dependency doesn't break collection of all OTHER profiles.
    """
    try:
        module = __import__(
            f"app.services.enterprise_orchestrator.profiles.{builder_name}",
            fromlist=["build"],
        )
        return module.build()
    except Exception as exc:  # noqa: BLE001
        logger.error("Could not load profile '%s': %s", builder_name, exc)
        raise


# Each entry is imported lazily on first request. ``None`` => deliberately
# not yet implemented (e.g. ``product``, ``competitive``) and surfaces as
# ``ProfileNotFoundError`` to the caller.
_PROFILE_BUILDERS: dict[str, Optional[str]] = {
    "enterprise": "enterprise",
    "market":     "market",
    # "product":     "product",       # reserved — follow-up plan
    # "competitive": "competitive",   # reserved — follow-up plan
}


_PROFILE_CACHE: dict[str, Profile] = {}


class ProfileNotFoundError(LookupError):
    """Raised when ``get_profile(name)`` does not match a registered builder."""


def get_profile(name: str) -> Profile:
    """Return the Profile for *name*, loading + caching on first request.

    Args:
        name: One of ``"enterprise"``, ``"market"``, and any future
              names registered in ``_PROFILE_BUILDERS``.

    Raises:
        ProfileNotFoundError: When the requested profile name is not
                              registered (or is registered but stubbed).
    """
    cached = _PROFILE_CACHE.get(name)
    if cached is not None:
        return cached
    builder = _PROFILE_BUILDERS.get(name)
    if not builder:
        raise ProfileNotFoundError(
            f"Unknown profile '{name}'. Available: "
            f"{sorted(k for k, v in _PROFILE_BUILDERS.items() if v)}"
        )
    profile = _load_profile(builder)
    _PROFILE_CACHE[name] = profile
    logger.debug(
        "Profile loaded: name=%s label=%s facets=%d sections=%d",
        profile.name, profile.label,
        len(profile.facet_spec), len(profile.section_schema),
    )
    return profile


def list_available_profiles() -> Tuple[str, ...]:
    """Return the registered profile names that have a non-None builder."""
    return tuple(sorted(name for name, b in _PROFILE_BUILDERS.items() if b))


def invalidate_cache() -> None:
    """Drop cached profiles (mainly for tests / dynamic config reload)."""
    _PROFILE_CACHE.clear()


# ---------------------------------------------------------------------------
# Cross-profile dimension resolver with prefix-match fallback.
# ---------------------------------------------------------------------------
def resolve_dimension(profile: Profile, facet_id: str) -> Optional[str]:
    """Map a facet_id to a coverage dimension, with prefix-match fallback.

    First tries ``profile.dimension_for_facet(facet_id)`` (exact). When that
    returns None, walks the profile's mapping and picks the first prefix
    that ``facet_id`` starts with. Returns None only when no match exists.

    Why two-stage: the profiler LLM might emit slightly variant ids
    (e.g. ``core_metrics_brent`` instead of ``core_metrics``). Prefix
    matching keeps coverage accurate without forcing a sanitization step
    on the agent LLM's output.
    """
    fid = (facet_id or "").strip()
    if not fid:
        return None
    direct = profile.dimension_for_facet(fid)
    if direct is not None:
        return direct
    for prefix, dimension in profile.facet_to_dimension.items():
        if fid.startswith(prefix):
            return dimension
    return None
