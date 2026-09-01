"""Comprehensive end-to-end test suite for the Skills platform.

Validates the full lifecycle of skills across all core functionalities:

PHASE 1 — Three creation methods:
  1. SkillFactory.create_from_description (LLM-generated from natural language)
  2. SkillFactory.create_from_code (upload/paste code)
  3. SkillCollectionService.collect_from_url (web-scraping via agent-browser)

PHASE 2 — Collection pipeline stages (extract → structure → validate → persist)

PHASE 3 — Runtime execution via skills_tool (search, load, execute, run)

PHASE 4 — Quality assertions on generated/collected skill content

PHASE 5 — Execution recorder verification (SkillRun DB rows)

All external dependencies (LLM, agent-browser) are mocked. The SkillsRegistry
is pointed at a temp directory so created skills are genuinely discoverable.
"""
import os
os.environ.setdefault("DATABASE_URL", "sqlite:///./test_runtime.db")

import asyncio
import shutil
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from app.database import Base, engine, SessionLocal
import app.models  # noqa: F401 — register all models

Base.metadata.create_all(engine)


# ──────────────────────────────────────────────────────────────────────────
# Shared fixtures
# ──────────────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def isolated_skills_env(monkeypatch):
    """Point USER_SKILLS_DIR + the global registry at a temp directory.

    This ensures skills written by the factory / collection service are
    genuinely discoverable via get_skill() — a true end-to-end check
    rather than just verifying a file exists on disk.
    """
    temp_dir = Path(tempfile.mkdtemp(prefix="e2e_skills_"))

    # Patch the USER_SKILLS_DIR that write_skill_md writes to
    import app.services.skill_sync as skill_sync_mod
    monkeypatch.setattr(skill_sync_mod, "USER_SKILLS_DIR", temp_dir)

    # Reset the global registry singleton so get_skills_registry() creates
    # a fresh SkillsRegistry pointing at our temp dir.
    import app.services.skills_loader as sl
    monkeypatch.setattr(sl, "_registry", None)
    monkeypatch.setenv("ZHANLU_SKILLS_DIR", str(temp_dir))

    yield temp_dir

    shutil.rmtree(temp_dir, ignore_errors=True)
    # Reset registry after test so it doesn't leak into other tests
    monkeypatch.setattr(sl, "_registry", None)


@pytest.fixture
def mock_llm_for_factory():
    """Mock call_llm to return a realistic SKILL.md body for factory tests."""
    skill_body = """## Overview

This skill helps agents create comprehensive data analysis reports from raw datasets. It covers data profiling, statistical analysis, visualization selection, and narrative composition.

## Prerequisites

- Access to the Zhanlu platform with data agent enabled
- A connected data source (database or uploaded file)
- Basic understanding of statistical concepts

## Steps

1. **Profile the dataset** — Examine column types, null distributions, and cardinality to understand the data shape.
2. **Compute summary statistics** — Generate mean, median, standard deviation, and quartiles for numeric columns.
3. **Identify trends and outliers** — Use time-series decomposition and IQR-based outlier detection.
4. **Select visualizations** — Choose chart types based on data characteristics (line for temporal, bar for categorical, scatter for correlation).
5. **Compose the narrative** — Weave insights into a structured report with executive summary, methodology, findings, and recommendations.

## Tool References

- `query_data` — Execute SQL against the connected data source
- `create_chart` — Generate visualizations from query results
- `create_report` — Assemble findings into a formatted document

## Best Practices

- Always validate data quality before drawing conclusions
- Use confidence intervals when reporting estimates
- Avoid cherry-picking data points that support a predetermined narrative
- Include methodology limitations in the final report

## Example Usage

When a user asks "analyze my sales data and create a report", the agent activates this skill, profiles the sales table, computes monthly trends, identifies top-performing segments, and produces a structured report with charts."""

    mock_result = {
        "response": skill_body,
        "model": "test-model",
        "usage": {"prompt_tokens": 50, "completion_tokens": 200},
        "data": None,
    }
    with patch("app.services.llm_service.call_llm", new_callable=AsyncMock, return_value=mock_result) as mock:
        yield mock


@pytest.fixture
def mock_agent_browser_extract():
    """Mock _agent_browser to simulate a successful page extraction."""
    page_content = """# How to Build a REST API with FastAPI

This guide covers best practices for building production-grade REST APIs with FastAPI.

## Overview

FastAPI is a modern, fast web framework for building APIs with Python. It combines the performance of Starlette with the developer experience of type hints and automatic documentation.

## Prerequisites

- Python 3.8+
- FastAPI installed (`pip install fastapi`)
- Uvicorn ASGI server

## Steps

1. **Define your data models** — Use Pydantic BaseModel classes to describe request and response schemas.
2. **Create route handlers** — Use decorators like `@app.get()` and `@app.post()` to define endpoints.
3. **Add dependency injection** — Use `Depends()` to inject shared logic like authentication and database sessions.
4. **Configure middleware** — Add CORS, error handlers, and logging middleware.
5. **Write tests** — Use TestClient to test endpoints without starting a server.

## Best Practices

- Use async route handlers for I/O-bound operations
- Validate input with Pydantic — never trust raw request data
- Version your API endpoints (/api/v1/, /api/v2/)
- Document every endpoint with docstrings — FastAPI auto-generates OpenAPI specs

## Example Usage

Create a simple CRUD API for a user resource with GET, POST, PUT, and DELETE endpoints, including input validation and error handling."""

    def _browser_side_effect(args, db=None, context=None):
        action = args.get("action")
        if action == "extract":
            return {"success": True, "text": page_content, "url": args.get("url", "")}
        if action == "close":
            return {"success": True}
        return {"success": False, "error": f"Unknown action: {action}"}

    with patch(
        "app.services.tool_handlers.agent_browser_tool._agent_browser",
        new_callable=AsyncMock,
        side_effect=_browser_side_effect,
    ) as mock:
        yield mock


@pytest.fixture
def mock_llm_for_collection():
    """Mock call_llm to return structured skill data for the collection pipeline."""
    mock_result = {
        "response": "{}",
        "model": "test-model",
        "usage": {},
        "data": {
            "name": "fastapi-rest-api",
            "description": "A skill for building production-grade REST APIs with FastAPI",
            "body": "## Overview\n\nFastAPI is a modern, fast web framework for building APIs with Python.\n\n## Prerequisites\n\n- Python 3.8+\n- FastAPI installed\n\n## Steps\n\n1. **Define data models** — Use Pydantic BaseModel classes.\n2. **Create route handlers** — Use decorators like @app.get().\n3. **Add dependency injection** — Use Depends() for shared logic.\n4. **Configure middleware** — Add CORS and error handlers.\n5. **Write tests** — Use TestClient.\n\n## Best Practices\n\n- Use async route handlers for I/O-bound operations\n- Validate input with Pydantic\n- Version your API endpoints\n\n## Example Usage\n\nCreate a CRUD API for a user resource with validation.",
        },
    }
    with patch("app.services.llm_service.call_llm", new_callable=AsyncMock, return_value=mock_result) as mock:
        yield mock


# ══════════════════════════════════════════════════════════════════════════
# PHASE 1: Three creation methods
# ══════════════════════════════════════════════════════════════════════════

class TestCreateFromDescription:
    """Method 1: Create a skill from a natural-language description via LLM."""

    @pytest.mark.asyncio
    async def test_full_lifecycle(self, isolated_skills_env, mock_llm_for_factory):
        """Factory → filesystem → registry → dry-run → SkillCandidate DB row."""
        from app.services.agent_studio.skill_factory import SkillFactory
        from app.services.skills_loader import get_skill
        from app.models.skill_candidate import SkillCandidate

        db = SessionLocal()
        try:
            factory = SkillFactory(db)
            candidate = await factory.create_from_description(
                name="data-analysis-report",
                description="Create comprehensive data analysis reports from datasets",
            )

            # 1. LLM was actually called
            assert mock_llm_for_factory.called, "LLM was never called"

            # 2. Skill persisted to filesystem
            skill_file = isolated_skills_env / "custom" / "data-analysis-report" / "SKILL.md"
            assert skill_file.exists(), f"SKILL.md not at {skill_file}"
            content = skill_file.read_text()
            assert "data analysis reports" in content.lower()

            # 3. Skill discoverable in the registry (true E2E)
            meta = get_skill("data-analysis-report")
            assert meta is not None, "Skill not found in registry after creation"
            assert meta.body is not None
            assert len(meta.body) > 0

            # 4. SkillCandidate persisted to DB
            assert candidate.name == "data-analysis-report"
            assert candidate.source_type == "description"
            assert candidate.source_data.get("llm_used") is True
            assert candidate.source_data.get("skill_path") is not None
            db_candidate = db.query(SkillCandidate).filter(
                SkillCandidate.name == "data-analysis-report"
            ).first()
            assert db_candidate is not None
        finally:
            db.close()

    @pytest.mark.asyncio
    async def test_generated_skill_has_frontmatter(self, isolated_skills_env, mock_llm_for_factory):
        """The persisted SKILL.md must have valid YAML frontmatter."""
        from app.services.agent_studio.skill_factory import SkillFactory

        db = SessionLocal()
        try:
            factory = SkillFactory(db)
            await factory.create_from_description(
                name="frontmatter-test",
                description="Test frontmatter generation",
            )
            skill_file = isolated_skills_env / "custom" / "frontmatter-test" / "SKILL.md"
            content = skill_file.read_text()
            assert content.startswith("---\n"), "SKILL.md must start with frontmatter"
            # Frontmatter should contain name, description, version
            assert "name:" in content[:500]
            assert "description:" in content[:500]
            assert "version:" in content[:500]
        finally:
            db.close()


class TestCreateFromCode:
    """Method 2: Create a skill by uploading/pasting code."""

    @pytest.mark.asyncio
    async def test_full_lifecycle(self, isolated_skills_env):
        """Factory create_from_code → filesystem → registry → SkillCandidate."""
        from app.services.agent_studio.skill_factory import SkillFactory
        from app.services.skills_loader import get_skill
        from app.models.skill_candidate import SkillCandidate

        db = SessionLocal()
        try:
            factory = SkillFactory(db)
            code = """import pandas as pd

def analyze_csv(file_path):
    df = pd.read_csv(file_path)
    summary = df.describe()
    return summary.to_dict()
"""
            candidate = await factory.create_from_code(
                name="csv-analyzer",
                code=code,
                description="Analyze CSV files and produce summary statistics",
            )

            # 1. Skill persisted
            skill_file = isolated_skills_env / "custom" / "csv-analyzer" / "SKILL.md"
            assert skill_file.exists()
            content = skill_file.read_text()
            assert "csv" in content.lower()
            # The code should be embedded in the SKILL.md
            assert "pandas" in content or "analyze_csv" in content

            # 2. Registry discoverable
            meta = get_skill("csv-analyzer")
            assert meta is not None, "Code-created skill not in registry"

            # 3. DB candidate
            assert candidate.source_type == "template"
            assert candidate.generated_code is not None
            assert "pandas" in candidate.generated_code
            db_candidate = db.query(SkillCandidate).filter(
                SkillCandidate.name == "csv-analyzer"
            ).first()
            assert db_candidate is not None
        finally:
            db.close()


class TestCreateFromUrl:
    """Method 3: Create a skill by scraping a web page via agent-browser."""

    @pytest.mark.asyncio
    async def test_full_lifecycle(
        self, isolated_skills_env, mock_agent_browser_extract, mock_llm_for_collection
    ):
        """collect_from_url → extract → structure → validate → persist → registry."""
        from app.services.skill_collection_service import SkillCollectionService
        from app.services.skills_loader import get_skill

        db = SessionLocal()
        try:
            service = SkillCollectionService(db=db)
            result = await service.collect_from_url(
                url="https://example.com/fastapi-guide",
                skill_name="fastapi-rest-api",
            )

            # 1. Pipeline succeeded
            assert result["success"] is True
            assert result["skill_name"] == "fastapi-rest-api"
            assert result["skill_path"] is not None
            assert result["source_url"] == "https://example.com/fastapi-guide"

            # 2. agent_browser was called for extraction
            assert mock_agent_browser_extract.called

            # 3. LLM was called for structuring
            assert mock_llm_for_collection.called

            # 4. Security scan findings present
            assert "scan_findings" in result
            assert "has_critical" in result["scan_findings"]

            # 5. Skill persisted to collected/ category
            skill_file = isolated_skills_env / "collected" / "fastapi-rest-api" / "SKILL.md"
            assert skill_file.exists()
            content = skill_file.read_text()
            assert "FastAPI" in content

            # 6. Registry discoverable
            meta = get_skill("fastapi-rest-api")
            assert meta is not None, "Collected skill not in registry"
            assert len(meta.body) > 0
        finally:
            db.close()


# ══════════════════════════════════════════════════════════════════════════
# PHASE 2: Collection pipeline stages
# ══════════════════════════════════════════════════════════════════════════

class TestCollectionPipelineStages:
    """Verify each stage of the collect_from_url pipeline independently."""

    @pytest.mark.asyncio
    async def test_stage_extract_failure(self, isolated_skills_env):
        """If agent_browser fails, pipeline returns error at 'extract' stage."""
        from app.services.skill_collection_service import SkillCollectionService

        db = SessionLocal()
        try:
            service = SkillCollectionService(db=db)
            with patch(
                "app.services.tool_handlers.agent_browser_tool._agent_browser",
                new_callable=AsyncMock,
                return_value={"success": False, "error": "Connection timeout"},
            ):
                result = await service.collect_from_url(url="https://unreachable.example.com")

            assert result["success"] is False
            assert result["stage"] == "extract"
            assert "Connection timeout" in result["error"]
        finally:
            db.close()

    @pytest.mark.asyncio
    async def test_stage_extract_empty_page(self, isolated_skills_env):
        """If page content is too short, pipeline fails at 'extract' stage."""
        from app.services.skill_collection_service import SkillCollectionService

        db = SessionLocal()
        try:
            service = SkillCollectionService(db=db)
            with patch(
                "app.services.tool_handlers.agent_browser_tool._agent_browser",
                new_callable=AsyncMock,
                return_value={"success": True, "text": "tiny", "url": "https://x.com"},
            ):
                result = await service.collect_from_url(url="https://x.com")

            assert result["success"] is False
            assert result["stage"] == "extract"
        finally:
            db.close()

    @pytest.mark.asyncio
    async def test_stage_structure_llm_failure(self, isolated_skills_env):
        """If LLM structuring fails, pipeline returns error at 'structure' stage."""
        from app.services.skill_collection_service import SkillCollectionService

        db = SessionLocal()
        try:
            service = SkillCollectionService(db=db)
            good_extract = {"success": True, "text": "# Some Guide\n\n" + "x" * 200, "url": "https://x.com"}
            with patch(
                "app.services.tool_handlers.agent_browser_tool._agent_browser",
                new_callable=AsyncMock,
                return_value=good_extract,
            ):
                with patch(
                    "app.services.llm_service.call_llm",
                    new_callable=AsyncMock,
                    side_effect=Exception("LLM service unavailable"),
                ):
                    result = await service.collect_from_url(url="https://x.com")

            assert result["success"] is False
            assert result["stage"] == "structure"
        finally:
            db.close()

    @pytest.mark.asyncio
    async def test_stage_structure_empty_body(self, isolated_skills_env):
        """If LLM returns an empty body, pipeline fails at 'structure' stage."""
        from app.services.skill_collection_service import SkillCollectionService

        db = SessionLocal()
        try:
            service = SkillCollectionService(db=db)
            good_extract = {"success": True, "text": "# Guide\n\n" + "content " * 50, "url": "https://x.com"}
            empty_llm = {
                "response": "{}",
                "model": "test",
                "usage": {},
                "data": {"name": "empty", "description": "desc", "body": ""},
            }
            with patch(
                "app.services.tool_handlers.agent_browser_tool._agent_browser",
                new_callable=AsyncMock,
                return_value=good_extract,
            ):
                with patch(
                    "app.services.llm_service.call_llm",
                    new_callable=AsyncMock,
                    return_value=empty_llm,
                ):
                    result = await service.collect_from_url(url="https://x.com")

            assert result["success"] is False
            assert result["stage"] == "structure"
        finally:
            db.close()

    @pytest.mark.asyncio
    async def test_name_derivation_from_url(self):
        """_derive_name_from_url produces valid kebab-case names."""
        from app.services.skill_collection_service import _derive_name_from_url

        assert _derive_name_from_url("https://example.com/skills/my-cool-skill") == "my-cool-skill"
        assert _derive_name_from_url("https://example.com/skills/my_cool_skill.md") == "my-cool-skill"
        assert _derive_name_from_url("https://example.com/path/to/Skill-Name.html") == "skill-name"
        assert _derive_name_from_url("https://example.com") != ""

    @pytest.mark.asyncio
    async def test_persist_to_db_tools_table(
        self, isolated_skills_env, mock_agent_browser_extract, mock_llm_for_collection
    ):
        """Collected skill should be synced to the DB tools table."""
        from app.services.skill_collection_service import SkillCollectionService
        from app.models.tool import Tool

        db = SessionLocal()
        try:
            service = SkillCollectionService(db=db)
            await service.collect_from_url(
                url="https://example.com/fastapi-guide",
                skill_name="fastapi-rest-api",
            )

            tool = db.query(Tool).filter(Tool.name == "fastapi-rest-api").first()
            assert tool is not None, "Collected skill not in DB tools table"
            assert tool.category == "collected"
            assert tool.kind == "system_skill"
            assert tool.enabled is True
        finally:
            db.close()


# ══════════════════════════════════════════════════════════════════════════
# PHASE 3: Runtime execution via skills_tool
# ══════════════════════════════════════════════════════════════════════════

class TestRuntimeSearch:
    """Runtime: the 'search' action finds created skills."""

    @pytest.mark.asyncio
    async def test_search_finds_created_skill(self, isolated_skills_env, mock_llm_for_factory):
        """After creating a skill, the search action should find it."""
        from app.services.agent_studio.skill_factory import SkillFactory
        from app.services.tool_handlers.skills_tool import _skills_tool

        db = SessionLocal()
        try:
            # Create a skill first
            factory = SkillFactory(db)
            await factory.create_from_description(
                name="data-analysis-report",
                description="Create comprehensive data analysis reports from datasets",
            )

            # Search for it
            result = await _skills_tool(
                args={"action": "search", "query": "data analysis"},
                db=db,
            )
            assert result["success"] is True
            assert result["count"] > 0
            names = [r.get("name", "") for r in result["results"]]
            assert "data-analysis-report" in names
        finally:
            db.close()

    @pytest.mark.asyncio
    async def test_list_categories(self, isolated_skills_env, mock_llm_for_factory):
        """list_categories should include 'custom' after factory creates a skill."""
        from app.services.agent_studio.skill_factory import SkillFactory
        from app.services.tool_handlers.skills_tool import _skills_tool

        db = SessionLocal()
        try:
            factory = SkillFactory(db)
            await factory.create_from_description(
                name="cat-test-skill",
                description="Test category listing",
            )

            result = await _skills_tool(args={"action": "list_categories"}, db=db)
            assert result["success"] is True
            assert "custom" in result["categories"]
        finally:
            db.close()


class TestRuntimeLoad:
    """Runtime: the 'load' action returns full skill content."""

    @pytest.mark.asyncio
    async def test_load_returns_full_content(self, isolated_skills_env, mock_llm_for_factory):
        """load should return the skill's metadata, body, and scripts list."""
        from app.services.agent_studio.skill_factory import SkillFactory
        from app.services.tool_handlers.skills_tool import _skills_tool

        db = SessionLocal()
        try:
            factory = SkillFactory(db)
            await factory.create_from_description(
                name="data-analysis-report",
                description="Create comprehensive data analysis reports",
            )

            result = await _skills_tool(
                args={"action": "load", "name": "data-analysis-report"},
                db=db,
            )
            assert result["success"] is True
            assert result["name"] == "data-analysis-report"
            assert result["content"] is not None
            assert len(result["content"]) > 100
            assert "scripts" in result
        finally:
            db.close()

    @pytest.mark.asyncio
    async def test_load_unknown_skill_fails(self, isolated_skills_env):
        """Loading a nonexistent skill should return an error."""
        from app.services.tool_handlers.skills_tool import _skills_tool

        result = await _skills_tool(
            args={"action": "load", "name": "does-not-exist-xyz"},
            db=None,
        )
        assert result["success"] is False
        assert "not found" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_load_missing_name_fails(self, isolated_skills_env):
        """load without a name parameter should return an error."""
        from app.services.tool_handlers.skills_tool import _skills_tool

        result = await _skills_tool(args={"action": "load"}, db=None)
        assert result["success"] is False
        assert "name" in result["error"].lower()


class TestRuntimeExecute:
    """Runtime: the 'execute' action returns an instruction + skill content."""

    @pytest.mark.asyncio
    async def test_execute_returns_instruction_and_content(self, isolated_skills_env, mock_llm_for_factory):
        """execute should return an instruction string + truncated skill_content."""
        from app.services.agent_studio.skill_factory import SkillFactory
        from app.services.tool_handlers.skills_tool import _skills_tool

        db = SessionLocal()
        try:
            factory = SkillFactory(db)
            await factory.create_from_description(
                name="data-analysis-report",
                description="Create comprehensive data analysis reports",
            )

            result = await _skills_tool(
                args={"action": "execute", "name": "data-analysis-report", "inputs": {"format": "pdf"}},
                db=db,
                context={"conversation_id": "test-conv-execute", "agent_name": "skill_agent"},
            )
            assert result["success"] is True
            assert result["name"] == "data-analysis-report"
            assert "instruction" in result
            assert "active" in result["instruction"].lower() or "follow" in result["instruction"].lower()
            assert "skill_content" in result
            assert len(result["skill_content"]) > 0
            assert result["inputs"] == {"format": "pdf"}
        finally:
            db.close()

    @pytest.mark.asyncio
    async def test_execute_unknown_skill_fails(self, isolated_skills_env):
        """Executing a nonexistent skill should return an error."""
        from app.services.tool_handlers.skills_tool import _skills_tool

        result = await _skills_tool(
            args={"action": "execute", "name": "no-such-skill"},
            db=None,
        )
        assert result["success"] is False
        assert "not found" in result["error"].lower()


class TestRuntimeCollect:
    """Runtime: the 'collect' action drives the collection pipeline from chat."""

    @pytest.mark.asyncio
    async def test_collect_via_tool(
        self, isolated_skills_env, mock_agent_browser_extract, mock_llm_for_collection
    ):
        """The skills tool 'collect' action should invoke the collection service."""
        from app.services.tool_handlers.skills_tool import _skills_tool

        db = SessionLocal()
        try:
            result = await _skills_tool(
                args={"action": "collect", "url": "https://example.com/fastapi-guide", "name": "fastapi-rest-api"},
                db=db,
            )
            assert result["success"] is True
            assert result["skill_name"] == "fastapi-rest-api"
        finally:
            db.close()

    @pytest.mark.asyncio
    async def test_collect_missing_url_fails(self, isolated_skills_env):
        """collect without a url should return an error."""
        from app.services.tool_handlers.skills_tool import _skills_tool

        result = await _skills_tool(args={"action": "collect"}, db=None)
        assert result["success"] is False
        assert "url" in result["error"].lower()


class TestRuntimeRun:
    """Runtime: the 'run' action executes bundled scripts in the sandbox."""

    def test_run_rejects_unknown_skill(self, isolated_skills_env, monkeypatch):
        """run should reject unknown skills (never raw-exec'd)."""
        from app.services.tool_handlers.skills_tool import _run_skill_script

        result = _run_skill_script(db=None, name="no-such-skill", entry_point="scripts/run.sh")
        assert result["success"] is False
        assert "not found" in result["error"].lower()

    def test_run_rejects_path_traversal(self, isolated_skills_env, tmp_path, monkeypatch):
        """run should reject entry_point with path traversal."""
        from app.services.skills_loader import SkillsRegistry
        import app.services.skills_loader as sl

        # Create a skill with a script
        sk = tmp_path / "traversal-test"
        (sk / "scripts").mkdir(parents=True)
        (sk / "SKILL.md").write_text("---\nname: traversal-test\ndescription: x\n---\n# Test\n")
        (sk / "scripts" / "run.sh").write_text("echo hi")

        reg = SkillsRegistry(skills_dir=str(tmp_path))
        reg.load()
        monkeypatch.setattr(sl, "get_skills_registry", lambda: reg)

        from app.services.tool_handlers.skills_tool import _run_skill_script

        result = _run_skill_script(
            db=None, name="traversal-test", entry_point="../../etc/passwd",
        )
        assert result["success"] is False
        assert "entry_point" in result["error"].lower() or "invalid" in result["error"].lower()


# ══════════════════════════════════════════════════════════════════════════
# PHASE 4: Quality assertions on generated/collected content
# ══════════════════════════════════════════════════════════════════════════

class TestSkillQuality:
    """Assert that generated/collected skills meet predefined quality standards."""

    @pytest.mark.asyncio
    async def test_factory_skill_has_required_sections(self, isolated_skills_env, mock_llm_for_factory):
        """LLM-generated skill must contain Overview, Steps, Best Practices."""
        from app.services.agent_studio.skill_factory import SkillFactory
        from app.services.skills_loader import get_skill

        db = SessionLocal()
        try:
            factory = SkillFactory(db)
            await factory.create_from_description(
                name="quality-test-skill",
                description="A skill for quality testing",
            )
            meta = get_skill("quality-test-skill")
            assert meta is not None
            body_lower = meta.body.lower()
            assert "overview" in body_lower, "Missing 'Overview' section"
            assert "steps" in body_lower, "Missing 'Steps' section"
            assert "best practices" in body_lower, "Missing 'Best Practices' section"
        finally:
            db.close()

    @pytest.mark.asyncio
    async def test_factory_skill_meets_min_length(self, isolated_skills_env, mock_llm_for_factory):
        """Generated skill body must be at least 100 characters (dry-run threshold)."""
        from app.services.agent_studio.skill_factory import SkillFactory
        from app.services.skills_loader import get_skill

        db = SessionLocal()
        try:
            factory = SkillFactory(db)
            await factory.create_from_description(
                name="length-test-skill",
                description="Testing minimum body length",
            )
            meta = get_skill("length-test-skill")
            assert meta is not None
            assert len(meta.body.strip()) >= 100, f"Body too short: {len(meta.body)} chars"
        finally:
            db.close()

    @pytest.mark.asyncio
    async def test_factory_skill_has_actionable_steps(self, isolated_skills_env, mock_llm_for_factory):
        """Steps section must contain numbered, actionable items."""
        from app.services.agent_studio.skill_factory import SkillFactory
        from app.services.skills_loader import get_skill

        db = SessionLocal()
        try:
            factory = SkillFactory(db)
            await factory.create_from_description(
                name="actionable-test",
                description="Testing actionable steps",
            )
            meta = get_skill("actionable-test")
            assert meta is not None
            # Look for numbered list items (1. 2. 3. etc.)
            import re
            numbered_items = re.findall(r"^\d+\.\s+\*\*", meta.body, re.MULTILINE)
            assert len(numbered_items) >= 3, f"Expected >= 3 numbered steps, found {len(numbered_items)}"
        finally:
            db.close()

    @pytest.mark.asyncio
    async def test_collected_skill_has_methodology_structure(
        self, isolated_skills_env, mock_agent_browser_extract, mock_llm_for_collection
    ):
        """Collected skill must have Overview + Steps sections."""
        from app.services.skill_collection_service import SkillCollectionService
        from app.services.skills_loader import get_skill

        db = SessionLocal()
        try:
            service = SkillCollectionService(db=db)
            await service.collect_from_url(
                url="https://example.com/fastapi-guide",
                skill_name="fastapi-rest-api",
            )
            meta = get_skill("fastapi-rest-api")
            assert meta is not None
            body_lower = meta.body.lower()
            assert "overview" in body_lower
            assert "steps" in body_lower
        finally:
            db.close()

    @pytest.mark.asyncio
    async def test_collected_skill_scan_findings_present(
        self, isolated_skills_env, mock_agent_browser_extract, mock_llm_for_collection
    ):
        """Collection result must include security scan findings."""
        from app.services.skill_collection_service import SkillCollectionService

        db = SessionLocal()
        try:
            service = SkillCollectionService(db=db)
            result = await service.collect_from_url(
                url="https://example.com/fastapi-guide",
                skill_name="fastapi-rest-api",
            )
            assert "scan_findings" in result
            sf = result["scan_findings"]
            assert "has_critical" in sf
            assert "summary" in sf
            assert isinstance(sf["has_critical"], bool)
        finally:
            db.close()

    @pytest.mark.asyncio
    async def test_execute_response_includes_skill_content(self, isolated_skills_env, mock_llm_for_factory):
        """execute response must include both instruction and skill_content."""
        from app.services.agent_studio.skill_factory import SkillFactory
        from app.services.tool_handlers.skills_tool import _skills_tool

        db = SessionLocal()
        try:
            factory = SkillFactory(db)
            await factory.create_from_description(
                name="exec-quality-test",
                description="Testing execute response quality",
            )
            result = await _skills_tool(
                args={"action": "execute", "name": "exec-quality-test"},
                db=db,
            )
            assert result["success"] is True
            # Quality: instruction must reference the skill name
            assert "exec-quality-test" in result["instruction"]
            # Quality: skill_content must be non-empty and substantial
            assert len(result["skill_content"]) >= 100
        finally:
            db.close()


class TestDryRunGate:
    """The dry-run validation gate checks schema, registry, and security."""

    @pytest.mark.asyncio
    async def test_dry_run_passes_for_valid_skill(self, isolated_skills_env, mock_llm_for_factory):
        """A well-formed skill should pass the dry-run gate."""
        from app.services.agent_studio.skill_factory import SkillFactory
        from app.services.skill_dry_run import run_dry_run_gate

        db = SessionLocal()
        try:
            factory = SkillFactory(db)
            await factory.create_from_description(
                name="dryrun-pass-test",
                description="A skill that should pass dry-run",
            )
            result = run_dry_run_gate("dryrun-pass-test", db)
            assert result["passed"] is True
            assert result["result"] == "pass"
            checks = {c["check"]: c["passed"] for c in result["checks"]}
            assert checks.get("non_empty") is True
            assert checks.get("required_sections") is True
            assert checks.get("registry_discoverable") is True
        finally:
            db.close()

    @pytest.mark.asyncio
    async def test_dry_run_fails_for_empty_body(self, isolated_skills_env, monkeypatch):
        """An empty skill body should fail the dry-run gate."""
        from app.services.skill_dry_run import run_dry_run_gate

        db = SessionLocal()
        try:
            result = run_dry_run_gate("nonexistent-skill-xyz", db, skill_body="")
            assert result["passed"] is False
            checks = {c["check"]: c["passed"] for c in result["checks"]}
            assert checks.get("non_empty") is False
        finally:
            db.close()

    @pytest.mark.asyncio
    async def test_dry_run_fails_for_missing_sections(self, isolated_skills_env):
        """A skill body missing required sections should fail."""
        from app.services.skill_dry_run import run_dry_run_gate

        db = SessionLocal()
        try:
            # Body without "overview" or "steps"
            body = "This is a skill about something. " * 10
            result = run_dry_run_gate("missing-sections-test", db, skill_body=body)
            assert result["passed"] is False
            checks = {c["check"]: c["passed"] for c in result["checks"]}
            assert checks.get("required_sections") is False
        finally:
            db.close()

    @pytest.mark.asyncio
    async def test_dry_run_persists_test_case(self, isolated_skills_env, mock_llm_for_factory):
        """The dry-run gate should persist a SkillTestCase record."""
        from app.services.agent_studio.skill_factory import SkillFactory
        from app.services.skill_dry_run import run_dry_run_gate
        from app.models.skill_test_case import SkillTestCase

        db = SessionLocal()
        try:
            factory = SkillFactory(db)
            await factory.create_from_description(
                name="testcase-persist-test",
                description="Testing test case persistence",
            )
            result = run_dry_run_gate("testcase-persist-test", db)
            tc = db.query(SkillTestCase).filter(
                SkillTestCase.name == "[auto] testcase-persist-test schema validation",
            ).first()
            assert tc is not None, "SkillTestCase not persisted"
            assert tc.status == result["result"]
            assert tc.run_count >= 1
        finally:
            db.close()


# ══════════════════════════════════════════════════════════════════════════
# PHASE 5: Execution recorder verification
# ══════════════════════════════════════════════════════════════════════════

class TestExecutionRecorder:
    """Verify that SkillRun records are created for every runtime invocation."""

    @pytest.mark.asyncio
    async def test_load_records_completed(self, isolated_skills_env, mock_llm_for_factory):
        """A successful load should create a 'completed' SkillRun."""
        from app.services.agent_studio.skill_factory import SkillFactory
        from app.services.tool_handlers.skills_tool import _skills_tool
        from app.models.skill_run import SkillRun

        db = SessionLocal()
        try:
            factory = SkillFactory(db)
            await factory.create_from_description(
                name="rec-load-test",
                description="Testing load recording",
            )
            await _skills_tool(
                args={"action": "load", "name": "rec-load-test"},
                db=db,
                context={"conversation_id": "conv-rec-load", "agent_name": "skill_agent"},
            )

            run = db.query(SkillRun).filter(
                SkillRun.conversation_id == "conv-rec-load",
            ).order_by(SkillRun.created_date.desc()).first()
            assert run is not None, "No SkillRun recorded for load"
            assert run.status == "completed"
            assert run.input_json["skill_name"] == "rec-load-test"
            assert run.input_json["action"] == "load"
            assert run.input_json["agent_name"] == "skill_agent"
        finally:
            db.close()

    @pytest.mark.asyncio
    async def test_load_unknown_records_failed(self, isolated_skills_env):
        """A failed load (unknown skill) should create a 'failed' SkillRun."""
        from app.services.tool_handlers.skills_tool import _skills_tool
        from app.models.skill_run import SkillRun

        db = SessionLocal()
        try:
            await _skills_tool(
                args={"action": "load", "name": "nonexistent-rec-test"},
                db=db,
                context={"conversation_id": "conv-rec-fail", "agent_name": "test_agent"},
            )

            run = db.query(SkillRun).filter(
                SkillRun.conversation_id == "conv-rec-fail",
            ).order_by(SkillRun.created_date.desc()).first()
            assert run is not None
            assert run.status == "failed"
            assert run.error_message is not None
            assert "not found" in run.error_message.lower()
        finally:
            db.close()

    @pytest.mark.asyncio
    async def test_execute_records_completed(self, isolated_skills_env, mock_llm_for_factory):
        """A successful execute should create a 'completed' SkillRun."""
        from app.services.agent_studio.skill_factory import SkillFactory
        from app.services.tool_handlers.skills_tool import _skills_tool
        from app.models.skill_run import SkillRun

        db = SessionLocal()
        try:
            factory = SkillFactory(db)
            await factory.create_from_description(
                name="rec-exec-test",
                description="Testing execute recording",
            )
            await _skills_tool(
                args={"action": "execute", "name": "rec-exec-test", "inputs": {"k": "v"}},
                db=db,
                context={"conversation_id": "conv-rec-exec", "agent_name": "skill_agent"},
            )

            run = db.query(SkillRun).filter(
                SkillRun.conversation_id == "conv-rec-exec",
            ).order_by(SkillRun.created_date.desc()).first()
            assert run is not None
            assert run.status == "completed"
            assert run.input_json["action"] == "execute"
        finally:
            db.close()

    @pytest.mark.asyncio
    async def test_duration_ms_recorded(self, isolated_skills_env, mock_llm_for_factory):
        """SkillRun should have a non-null duration_ms."""
        from app.services.agent_studio.skill_factory import SkillFactory
        from app.services.tool_handlers.skills_tool import _skills_tool
        from app.models.skill_run import SkillRun

        db = SessionLocal()
        try:
            factory = SkillFactory(db)
            await factory.create_from_description(
                name="rec-duration-test",
                description="Testing duration recording",
            )
            await _skills_tool(
                args={"action": "load", "name": "rec-duration-test"},
                db=db,
                context={"conversation_id": "conv-rec-dur", "agent_name": "skill_agent"},
            )

            run = db.query(SkillRun).filter(
                SkillRun.conversation_id == "conv-rec-dur",
            ).first()
            assert run is not None
            assert run.duration_ms is not None
            assert run.duration_ms >= 0
        finally:
            db.close()

    @pytest.mark.asyncio
    async def test_multiple_actions_all_recorded(self, isolated_skills_env, mock_llm_for_factory):
        """Both load and execute on the same skill should create separate SkillRuns."""
        from app.services.agent_studio.skill_factory import SkillFactory
        from app.services.tool_handlers.skills_tool import _skills_tool
        from app.models.skill_run import SkillRun

        db = SessionLocal()
        try:
            factory = SkillFactory(db)
            await factory.create_from_description(
                name="rec-multi-test",
                description="Testing multiple action recording",
            )
            conv_id = "conv-rec-multi"
            await _skills_tool(
                args={"action": "load", "name": "rec-multi-test"},
                db=db,
                context={"conversation_id": conv_id, "agent_name": "skill_agent"},
            )
            await _skills_tool(
                args={"action": "execute", "name": "rec-multi-test"},
                db=db,
                context={"conversation_id": conv_id, "agent_name": "skill_agent"},
            )

            runs = db.query(SkillRun).filter(
                SkillRun.conversation_id == conv_id,
            ).all()
            assert len(runs) >= 2
            actions = {r.input_json.get("action") for r in runs}
            assert "load" in actions
            assert "execute" in actions
        finally:
            db.close()

    @pytest.mark.asyncio
    async def test_recorder_non_blocking_on_db_error(self, isolated_skills_env):
        """Recorder must not raise when the DB is unavailable."""
        from app.services.skill_execution_recorder import SkillExecutionRecorder

        with patch("app.database.SessionLocal", side_effect=Exception("DB unavailable")):
            # Must not raise
            SkillExecutionRecorder.record(
                skill_name="error-test",
                action="load",
                status="completed",
            )


# ══════════════════════════════════════════════════════════════════════════
# PHASE 6: Full lifecycle integration — create → search → load → execute
# ══════════════════════════════════════════════════════════════════════════

class TestFullLifecycleIntegration:
    """End-to-end: create a skill, then search, load, and execute it at runtime."""

    @pytest.mark.asyncio
    async def test_create_then_search_load_execute(self, isolated_skills_env, mock_llm_for_factory):
        """Full lifecycle: factory create → search → load → execute → verify recording."""
        from app.services.agent_studio.skill_factory import SkillFactory
        from app.services.tool_handlers.skills_tool import _skills_tool
        from app.models.skill_run import SkillRun

        db = SessionLocal()
        try:
            # Step 1: Create
            factory = SkillFactory(db)
            await factory.create_from_description(
                name="lifecycle-integration",
                description="A skill for full lifecycle integration testing",
            )

            # Step 2: Search finds it
            search_result = await _skills_tool(
                args={"action": "search", "query": "lifecycle"},
                db=db,
            )
            assert search_result["success"]
            names = [r.get("name", "") for r in search_result["results"]]
            assert "lifecycle-integration" in names

            # Step 3: Load returns content
            load_result = await _skills_tool(
                args={"action": "load", "name": "lifecycle-integration"},
                db=db,
                context={"conversation_id": "lifecycle-conv", "agent_name": "skill_agent"},
            )
            assert load_result["success"]
            assert len(load_result["content"]) > 100

            # Step 4: Execute returns instruction + content
            exec_result = await _skills_tool(
                args={"action": "execute", "name": "lifecycle-integration"},
                db=db,
                context={"conversation_id": "lifecycle-conv", "agent_name": "skill_agent"},
            )
            assert exec_result["success"]
            assert "instruction" in exec_result
            assert "skill_content" in exec_result

            # Step 5: Both load and execute were recorded
            runs = db.query(SkillRun).filter(
                SkillRun.conversation_id == "lifecycle-conv",
            ).all()
            assert len(runs) >= 2
            statuses = [r.status for r in runs]
            assert all(s == "completed" for s in statuses), f"Expected all completed, got {statuses}"
        finally:
            db.close()

    @pytest.mark.asyncio
    async def test_collect_then_search_load_execute(
        self, isolated_skills_env, mock_agent_browser_extract, mock_llm_for_collection
    ):
        """Full lifecycle: collect from URL → search → load → execute → verify recording."""
        from app.services.skill_collection_service import SkillCollectionService
        from app.services.tool_handlers.skills_tool import _skills_tool
        from app.models.skill_run import SkillRun

        db = SessionLocal()
        try:
            # Step 1: Collect
            service = SkillCollectionService(db=db)
            collect_result = await service.collect_from_url(
                url="https://example.com/fastapi-guide",
                skill_name="fastapi-rest-api",
            )
            assert collect_result["success"]

            # Step 2: Search finds it
            search_result = await _skills_tool(
                args={"action": "search", "query": "fastapi"},
                db=db,
            )
            assert search_result["success"]
            names = [r.get("name", "") for r in search_result["results"]]
            assert "fastapi-rest-api" in names

            # Step 3: Load
            load_result = await _skills_tool(
                args={"action": "load", "name": "fastapi-rest-api"},
                db=db,
                context={"conversation_id": "collect-lifecycle-conv", "agent_name": "skill_agent"},
            )
            assert load_result["success"]
            assert "FastAPI" in load_result["content"]

            # Step 4: Execute
            exec_result = await _skills_tool(
                args={"action": "execute", "name": "fastapi-rest-api"},
                db=db,
                context={"conversation_id": "collect-lifecycle-conv", "agent_name": "skill_agent"},
            )
            assert exec_result["success"]

            # Step 5: Recording
            runs = db.query(SkillRun).filter(
                SkillRun.conversation_id == "collect-lifecycle-conv",
            ).all()
            assert len(runs) >= 2
        finally:
            db.close()
