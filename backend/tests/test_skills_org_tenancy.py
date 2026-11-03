"""Phase 11 / S1 — skills multi-tenant isolation (P0).

Contract (closes the Phase 10 blocked S2.1):

* Tenant-owned skills live under ``~/.zhanlu/skills/orgs/<org_id>/…``;
  the legacy root directory is the SYSTEM/LEGACY tier (bundled +
  pre-migration files) and stays visible to everyone.
* ``SkillsRegistry.get(name, org_id=…)`` returns an org-scoped skill ONLY
  to its owning org; cross-org and org-less lookups get None (no
  existence leak).
* Duplicate names across orgs coexist — each org resolves its own.
* ``write_skill_md``/``delete_skill_md`` take an ``org_id``: writes land
  in the org's own directory; deletes can never touch another org's
  files. Path traversal in name/category/org is refused.
* DB-backed skill lookups are org-scoped: a caller sees system sources
  (builtin/marketplace) and its own org's rows — never another org's.
* Execution-time authorization: the caller org is resolved from verified
  context (tenant middleware → user row), NEVER from client arguments;
  anonymous execution of org-scoped skills is denied.
"""
import pytest
from types import SimpleNamespace
from sqlalchemy.pool import StaticPool

from app.services import skill_sync as ss
from app.services.skills_loader import SkillsRegistry


@pytest.fixture()
def skills_home(tmp_path, monkeypatch):
    """Isolated skills root + fresh registry for each test."""
    root = tmp_path / "skills"
    root.mkdir()
    monkeypatch.setattr(ss, "USER_SKILLS_DIR", root)
    reg = SkillsRegistry(skills_dir=tmp_path / "bundled")
    monkeypatch.setattr("app.services.skills_loader.get_skills_registry",
                        lambda: reg, raising=False)
    return root, reg


# ── filesystem layout: org-scoped writes/reads ──────────────────────────

def test_write_with_org_lands_in_org_dir(skills_home):
    root, _ = skills_home
    path = ss.write_skill_md("reporter", "d", "body", org_id="org-A")
    assert f"orgs{ss.os.sep}org-A" in path or "orgs/org-A" in path
    assert (root / "orgs" / "org-A" / "custom" / "reporter" / "SKILL.md").exists()


def test_write_without_org_keeps_legacy_root(skills_home):
    root, _ = skills_home
    path = ss.write_skill_md("legacy-skill", "d", "body")
    assert "orgs" not in path
    assert (root / "custom" / "legacy-skill" / "SKILL.md").exists()


def test_org_scoped_skill_visible_only_to_owner(skills_home):
    root, reg = skills_home
    ss.write_skill_md("org-a-skill", "d", "body", org_id="org-A")
    reg2 = SkillsRegistry(skills_dir=root.parent / "bundled-none")
    # rescan via a fresh registry sharing the same user dir root
    reg._skills.clear()
    reg._loaded = False
    reg.load()
    assert reg.get("org-a-skill", org_id="org-A") is not None
    assert reg.get("org-a-skill", org_id="org-B") is None, \
        "cross-org read must not resolve"
    assert reg.get("org-a-skill") is None, \
        "org-less lookup must not resolve an org-scoped skill"


def test_duplicate_names_across_orgs_coexist(skills_home):
    root, reg = skills_home
    ss.write_skill_md("shared-name", "A", "body A", org_id="org-A")
    ss.write_skill_md("shared-name", "B", "body B", org_id="org-B")
    reg._skills.clear()
    reg._loaded = False
    reg.load()
    a = reg.get("shared-name", org_id="org-A")
    b = reg.get("shared-name", org_id="org-B")
    assert a is not None and b is not None and a is not b
    assert a.org_id == "org-A" and b.org_id == "org-B"


def test_bundled_skills_are_system_tier_visible_to_all_orgs(skills_home):
    root, reg = skills_home
    bundled = root.parent / "bundled"
    (bundled / "cat" / "sys-skill").mkdir(parents=True)
    (bundled / "cat" / "sys-skill" / "SKILL.md").write_text(
        "---\nname: sys-skill\ndescription: sys\n---\nbody")
    reg2 = SkillsRegistry(skills_dir=bundled)
    reg2.load()
    assert reg2.get("sys-skill", org_id="org-A") is not None
    assert reg2.get("sys-skill", org_id="org-B") is not None
    assert reg2.get("sys-skill") is not None


def test_legacy_root_skills_remain_visible_system_tier(skills_home):
    root, reg = skills_home
    ss.write_skill_md("legacy-skill", "d", "body")
    reg._skills.clear()
    reg._loaded = False
    reg.load()
    assert reg.get("legacy-skill", org_id="org-A") is not None
    assert reg.get("legacy-skill", org_id="org-B") is not None


def test_delete_is_org_scoped(skills_home):
    root, reg = skills_home
    ss.write_skill_md("doomed", "d", "body", org_id="org-A")
    # org-B cannot delete org-A's file
    assert ss.delete_skill_md("doomed", org_id="org-B") is False
    assert (root / "orgs" / "org-A" / "custom" / "doomed").exists()
    # owner can
    assert ss.delete_skill_md("doomed", org_id="org-A") is True
    assert not (root / "orgs" / "org-A" / "custom" / "doomed").exists()


def test_path_traversal_refused(skills_home):
    root, _ = skills_home
    with pytest.raises(ValueError):
        ss.write_skill_md("../../escape", "d", "body", org_id="org-A")
    with pytest.raises(ValueError):
        ss.write_skill_md("ok-name", "d", "body", org_id="../../escape")
    with pytest.raises(ValueError):
        ss.delete_skill_md("../escape", org_id="org-A")


# ── DB-layer scoping (real sqlite Tool table) ────────────────────────────

@pytest.fixture()
def tool_db():
    """Real in-memory sqlite with the actual Tool table — the org filter
    must survive genuine SQLAlchemy or_/in_ expressions."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from app.database import Base
    from app.models.tool import Tool

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False},
        poolclass=StaticPool)
    Base.metadata.create_all(engine, tables=[Tool.__table__])
    s = sessionmaker(bind=engine)()
    s.add(Tool(name="mine", org_id="org-A", source="custom",
               summary="mine", enabled=True))
    s.add(Tool(name="sys", org_id="default-org", source="builtin",
               summary="system", enabled=True))
    s.add(Tool(name="other", org_id="org-B", source="marketplace_user",
               summary="B-secret", enabled=True))
    s.commit()
    yield s
    s.close()
    engine.dispose()


def test_db_lookup_hides_other_orgs_rows(tool_db):
    from app.services.skills_loader import _lookup_skill_summary

    assert _lookup_skill_summary("other", db=tool_db, org_id="org-A") is None, \
        "another org's tenant skill leaked through the DB lookup"


def test_db_lookup_shows_system_and_own_org(tool_db):
    from app.services.skills_loader import _lookup_skill_summary

    assert _lookup_skill_summary("mine", db=tool_db, org_id="org-A") == "mine"
    assert _lookup_skill_summary("sys", db=tool_db, org_id="org-A") == "system"
    # default tier (org-less context): default-org + system only
    assert _lookup_skill_summary("sys", db=tool_db) == "system"
    assert _lookup_skill_summary("other", db=tool_db) is None


# ── execution-time authorization ─────────────────────────────────────────

def test_caller_org_resolution_never_trusts_client_args():
    from app.services.tool_handlers.skills_tool import _resolve_caller_org

    # user row wins; a client-supplied org_id in args/context is ignored
    class _UQ:
        def filter(self, *a, **k):
            return self

        def first(self):
            return SimpleNamespace(org_id="org-real")

    db = SimpleNamespace(query=lambda *a: _UQ())
    assert _resolve_caller_org(db, "u1", None) == "org-real"
    assert _resolve_caller_org(db, "u1", {"org_id": "org-EVIL"}) == "org-real"


def test_caller_org_unresolvable_is_none():
    from app.services.tool_handlers.skills_tool import _resolve_caller_org

    class _UQ:
        def filter(self, *a, **k):
            return self

        def first(self):
            return None

    assert _resolve_caller_org(
        SimpleNamespace(query=lambda *a: _UQ()), None, None) is None


# ── migration 133 + legacy migration script contract ─────────────────────

def test_migration_133_chains_from_current_head_and_is_idempotent():
    from pathlib import Path
    p = Path("alembic/versions/133_skills_org_tenancy.py")
    assert p.exists(), "migration 133 missing"
    src = p.read_text()
    assert 'down_revision = "132_automation_retry_scheduling"' in src
    # repeatable: only touches rows still at the legacy default
    assert "t.org_id = 'default-org'" in src
    # auditable: logs the backfill count
    assert "backfilled" in src
    # safe: no schema change
    assert "add_column" not in src.lower() and "drop_column" not in src.lower()


def test_legacy_migration_script_exists_and_fails_safe():
    from pathlib import Path
    p = Path("scripts/migrate_skill_dirs.py")
    assert p.exists(), "legacy skill-dir migration script missing"
    src = p.read_text()
    # fail-safe: unresolvable ownership stays in the legacy root
    assert "unresolvable" in src
    assert "shutil.move" in src  # real moves are audited/logged
    assert "--dry-run" in src
