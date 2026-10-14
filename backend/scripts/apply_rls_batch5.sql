-- ── Row-Level Security — batch 5 (2026-10-14): fix the NULL-bypass quirk ───
-- Apply in the stop-app window: docker exec -i zhanlu-postgres psql -U zhanlu -d zhanlu < scripts/apply_rls_batch5.sql
--
-- BUG (found live, intermittent login 401s after batch 4 added `users`):
-- Postgres custom-GUC quirk — after `SET LOCAL app.current_org = '<org>'`
-- runs on a pooled connection (every authenticated request pins via
-- database.py after_begin), `current_setting('app.current_org', true)` in
-- SUBSEQUENT transactions on that connection returns EMPTY STRING, not
-- NULL — the value reverts but the setting stays "set". The policies'
-- NULL-bypass (`... IS NULL`) then fails: unpinned queries (login, refresh,
-- background jobs, seed) hitting a sticky connection matched
-- org_id = '' and returned ZERO rows.
--
-- FIX: the bypass treats BOTH NULL and '' as unpinned:
--   coalesce(current_setting('app.current_org', true), '') = ''
--
-- Security: '' can only arise from the SET LOCAL lifecycle — clients cannot
-- run SQL; the only SET comes from after_begin with a regex-validated org.
--
-- Regenerates the tenant_isolation policy on EVERY table that has one.
DO $$
DECLARE
    t text;
BEGIN
    FOR t IN
        SELECT pol.polrelid::regclass::text
        FROM pg_policy pol
        WHERE pol.polname = 'tenant_isolation'
        LOOP
        EXECUTE format('DROP POLICY IF EXISTS tenant_isolation ON %I', t);
        EXECUTE format($f$
            CREATE POLICY tenant_isolation ON %I
            USING (
                coalesce(current_setting('app.current_org', true), '') = ''
                OR org_id = current_setting('app.current_org', true)
            )
        $f$, t);
        RAISE NOTICE 'fixed tenant_isolation on %', t;
    END LOOP;
END
$$;

-- Verify: every RLS table's policy now contains the coalesce bypass:
--   SELECT polrelid::regclass::text FROM pg_policy WHERE polname='tenant_isolation'
--   AND pg_get_expr(polqual, polrelid) LIKE '%coalesce%';
