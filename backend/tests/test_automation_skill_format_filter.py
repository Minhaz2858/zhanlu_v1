"""Tests for skill/output_format reconciliation in the automation executor.

Pins the two user-required contracts:

1. Universal skills (research/methodology — no ``compatible_formats`` declared)
   stay in the prompt regardless of ``task.output_format``. E.g.
   ``web-research`` + ``output_format=docx`` is a valid, meaningful case.

2. Format-bound skills whose declared format conflicts with
   ``task.output_format`` are silently excluded from the skills context and
   logged at INFO. E.g. ``pptx`` + ``output_format=docx`` -> excluded, ships
   DOCX with no skill interference.

3. The filter is fail-safe: a loader exception returns the original list
   unchanged (never drops a legitimate skill on a transient error).
"""
import os
import sys

_BACKEND_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)


# ---------------------------------------------------------------------------
# 1. _filter_skills_by_output_format (pure partition)
# ---------------------------------------------------------------------------

def test_filter_keeps_universal_skill_for_any_format(monkeypatch):
    """web-research (universal) + docx -> kept."""
    from app.services import automation_executor

    def _resolve(name, db=None):
        return []  # no compatible_formats -> universal

    monkeypatch.setattr(
        "app.services.skills_loader._resolve_skill_compatible_formats", _resolve
    )
    compatible, excluded = automation_executor._filter_skills_by_output_format(
        ["web-research"], "docx", db=None
    )
    assert compatible == ["web-research"]
    assert excluded == []


def test_filter_excludes_format_bound_skill_on_mismatch(monkeypatch):
    """pptx (format-bound) + docx -> excluded."""
    from app.services import automation_executor

    def _resolve(name, db=None):
        return {"pptx": ["pptx"]}.get(name, [])

    monkeypatch.setattr(
        "app.services.skills_loader._resolve_skill_compatible_formats", _resolve
    )
    compatible, excluded = automation_executor._filter_skills_by_output_format(
        ["pptx"], "docx", db=None
    )
    assert compatible == []
    assert excluded == ["pptx"]


def test_filter_keeps_format_bound_skill_on_match(monkeypatch):
    """pptx (format-bound) + pptx -> kept."""
    from app.services import automation_executor

    def _resolve(name, db=None):
        return {"pptx": ["pptx"]}.get(name, [])

    monkeypatch.setattr(
        "app.services.skills_loader._resolve_skill_compatible_formats", _resolve
    )
    compatible, excluded = automation_executor._filter_skills_by_output_format(
        ["pptx"], "pptx", db=None
    )
    assert compatible == ["pptx"]
    assert excluded == []


def test_filter_mixed_partition(monkeypatch):
    """web-research + pptx, output_format=docx -> research kept, pptx dropped."""
    from app.services import automation_executor

    def _resolve(name, db=None):
        return {"pptx": ["pptx"]}.get(name, [])

    monkeypatch.setattr(
        "app.services.skills_loader._resolve_skill_compatible_formats", _resolve
    )
    compatible, excluded = automation_executor._filter_skills_by_output_format(
        ["web-research", "pptx"], "docx", db=None
    )
    assert compatible == ["web-research"]
    assert excluded == ["pptx"]


def test_filter_normalizes_output_format_case(monkeypatch):
    """output_format 'DOCX' must still match compatible_formats ['docx']."""
    from app.services import automation_executor

    def _resolve(name, db=None):
        return {"docx": ["docx"]}.get(name, [])

    monkeypatch.setattr(
        "app.services.skills_loader._resolve_skill_compatible_formats", _resolve
    )
    compatible, excluded = automation_executor._filter_skills_by_output_format(
        ["docx"], "DOCX", db=None
    )
    assert compatible == ["docx"]
    assert excluded == []


def test_filter_failsafe_on_loader_error(monkeypatch):
    """A resolver exception returns the original list unchanged (fail-safe)."""
    from app.services import automation_executor

    def _boom(name, db=None):
        raise RuntimeError("registry down")

    monkeypatch.setattr(
        "app.services.skills_loader._resolve_skill_compatible_formats", _boom
    )
    compatible, excluded = automation_executor._filter_skills_by_output_format(
        ["pptx"], "docx", db=None
    )
    assert compatible == ["pptx"]
    assert excluded == []


def test_filter_empty_input():
    from app.services import automation_executor
    assert automation_executor._filter_skills_by_output_format([], "docx") == ([], [])
    assert automation_executor._filter_skills_by_output_format(None, "docx") == ([], [])


# ---------------------------------------------------------------------------
# 2. _build_skills_context integration (filter wired in + INFO log)
# ---------------------------------------------------------------------------

class _FakeTask:
    def __init__(self, skills=None, output_format="docx"):
        self.skills = skills
        self.output_format = output_format
        self.name = "Test Task"
        self.id = "task-1"


def test_build_skills_context_excludes_mismatched_skill(monkeypatch, caplog):
    """pptx + docx -> the loader only receives the compatible subset."""
    import logging
    from app.services import automation_executor

    received = []

    def _resolve(name, db=None):
        return {"pptx": ["pptx"]}.get(name, [])

    def _fake_metadata(names, db=None):
        received.append(list(names))
        return "## Available Skills\n- **web-research**: research"

    monkeypatch.setattr(
        "app.services.skills_loader._resolve_skill_compatible_formats", _resolve
    )
    monkeypatch.setattr(
        "app.services.skills_loader.get_skill_metadata_for_agent", _fake_metadata
    )

    with caplog.at_level(logging.INFO):
        out = automation_executor._build_skills_context(
            _FakeTask(["web-research", "pptx"], output_format="docx"), None
        )

    assert "web-research" in out
    assert "pptx" not in received[0], "pptx must be filtered before the loader"
    assert received[0] == ["web-research"]

    log_msgs = [r.getMessage() for r in caplog.records]
    assert any("skipped skill 'pptx'" in m for m in log_msgs), (
        "expected an INFO log for the excluded pptx skill"
    )
    assert any("output_format 'docx'" in m for m in log_msgs)


def test_build_skills_context_keeps_universal_skill(monkeypatch, caplog):
    """web-research + docx -> indexed, no exclusion log."""
    import logging
    from app.services import automation_executor

    def _resolve(name, db=None):
        return []

    def _fake_metadata(names, db=None):
        return "## Available Skills\n- **web-research**: research"

    monkeypatch.setattr(
        "app.services.skills_loader._resolve_skill_compatible_formats", _resolve
    )
    monkeypatch.setattr(
        "app.services.skills_loader.get_skill_metadata_for_agent", _fake_metadata
    )

    with caplog.at_level(logging.INFO):
        out = automation_executor._build_skills_context(
            _FakeTask(["web-research"], output_format="docx"), None
        )

    assert "web-research" in out
    assert not any("skipped skill" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# 3. Real resolver against the bundled skills registry
# ---------------------------------------------------------------------------

def test_resolve_pptx_skill_has_compatible_formats():
    """The bundled pptx skill must declare compatible_formats=[pptx] so the
    name fallback is not relied upon."""
    from app.services.skills_loader import get_skills_registry
    skill = get_skills_registry().get("pptx")
    if skill is None:
        # Registry may not include document-skills in this environment.
        return
    assert "pptx" in skill.compatible_formats


def test_resolve_universal_skill_has_no_compatible_formats():
    """A research skill must NOT declare compatible_formats (stays universal)."""
    from app.services.skills_loader import get_skills_registry
    skill = get_skills_registry().get("web-research")
    if skill is None:
        return
    assert skill.compatible_formats == []
