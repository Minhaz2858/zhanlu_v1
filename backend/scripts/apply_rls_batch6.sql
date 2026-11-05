-- ── Row-Level Security — batch 6 (2026-10-25): close the cad_revisions gap ──
-- Apply in the stop-app window:
--   docker exec -i zhanlu-postgres psql -U zhanlu -d zhanlu -f scripts/apply_rls_batch6.sql
-- (psql needs -f/-i — a bare `< file` pipe silently no-ops in some exec paths.)
--
-- WHY: cad_revisions (migration 101_cad_revisions, 2026-10-19 — the CAD P3
-- engineering-change revision store) carries org_id NOT NULL but was created
-- AFTER the 2026-10-13/14 RLS rollout, so it was never enrolled. It is pure
-- org-scoped data (per-app CAD revisions tied to an org). Without a policy it
-- sits OUTSIDE the RLS membrane and any org could read another org's CAD
-- revisions through a background/unpinned path.
--
-- POLICY: identical to every other tenant_isolation policy — FORCE RLS (owner
-- included, because the app connects as the table owner) + the coalesce(''='')
-- NULL-bypass so unpinned background/seed contexts still function (see
-- apply_rls_batch5.sql for the sticky-GUC quirk this guards against).
--
-- The OTHER org_id tables still outside RLS are INTENTIONAL and kept out by
-- design (documented here so the gap list is auditable):
--   organizations          tenant root — global slug/email resolution, RLS
--                           would break tenant creation/login.
--   tools, external_agents, global cross-tenant catalogs — the per-org skill
--   external_skills,        namespacing work (separate item) is the real fix;
--   market_agents,          RLS here would empty shared catalogs.
--   skill_sources
--   chat_shares,            sharing ACLs ARE the isolation mechanism; org RLS
--   resource_access_policies, could hide a grant's target from its grantee
--   resource_registry,      path. Service layer enforces org bounds; a future
--   resource_shares         custom policy (org_id = current_org OR
--                           shared_with_user = jwt.sub) is the long-term shape.
--   alembic_version         system table, no tenant data.

DO $$
DECLARE
    t text;
BEGIN
    FOREACH t IN ARRAY ARRAY[ 'cad_revisions' ]
    LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
        EXECUTE format('DROP POLICY IF EXISTS tenant_isolation ON %I', t);
        EXECUTE format($f$
            CREATE POLICY tenant_isolation ON %I
            USING (
                coalesce(current_setting('app.current_org', true), '') = ''
                OR org_id = current_setting('app.current_org', true)
            )
        $f$, t);
        RAISE NOTICE 'RLS batch6 enabled on %', t;
    END LOOP;
END
$$;

-- Verify: SELECT relname, relrowsecurity, relforcerowsecurity
-- FROM pg_class WHERE relname = 'cad_revisions';
--   → cad_revisions | t | t
