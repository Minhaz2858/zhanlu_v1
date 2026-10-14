-- ── Row-Level Security rollout — batch 1 (2026-10-13, tenancy D4) ──────────
-- Apply: docker exec -i zhanlu-postgres psql -U zhanlu -d zhanlu < scripts/apply_rls.sql
--
-- Model: FORCE RLS (owner included — the app connects as the table owner)
-- + one policy per table with a NULL-bypass: rows are confined to
-- app.current_org WHEN it is set (verified JWT claim, pinned per
-- transaction by app/database.py); when UNSET (background jobs, seed
-- scripts, maintenance), the bypass keeps them functional. Per-path
-- tightening of the bypass comes later.
--
-- Add tables to this batch as their query paths are audited. NEVER enable
-- RLS on a table whose queries run as a role that cannot be trusted to
-- have the org pinned.

DO $$
DECLARE
    t text;
BEGIN
    FOREACH t IN ARRAY ARRAY[
        'agent_conversations',
        'chat_messages',
        'artifacts',
        'projects',
        'user_files',
        'agent_portfolio_entries'
    ]
    LOOP
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
        EXECUTE format('DROP POLICY IF EXISTS tenant_isolation ON %I', t);
        EXECUTE format($f$
            CREATE POLICY tenant_isolation ON %I
            USING (
                org_id = current_setting('app.current_org', true)
                OR current_setting('app.current_org', true) IS NULL
            )
        $f$, t);
        RAISE NOTICE 'RLS enabled on %', t;
    END LOOP;
END
$$;

-- ── Batch 2 (2026-10-14): value-layer + ops tables ─────────────────────────
DO $$
DECLARE
    t text;
BEGIN
    FOREACH t IN ARRAY ARRAY[
        'nl2sql_query_logs',
        'cost_ledger',
        'experience_entries',
        'agent_runs',
        'automation_tasks',
        'session_states',
        'dashboard_apps',
        'agent_todos'
    ]
    LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
        EXECUTE format('DROP POLICY IF EXISTS tenant_isolation ON %I', t);
        EXECUTE format($f$
            CREATE POLICY tenant_isolation ON %I
            USING (
                org_id = current_setting('app.current_org', true)
                OR current_setting('app.current_org', true) IS NULL
            )
        $f$, t);
        RAISE NOTICE 'RLS enabled on %', t;
    END LOOP;
END
$$;
