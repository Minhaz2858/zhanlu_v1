-- ── Row-Level Security rollout — batch 4 (2026-10-14): the DENYLIST ────────
-- Apply in the stop-app window: docker exec -i zhanlu-postgres psql -U zhanlu -d zhanlu -f scripts/apply_rls_batch4.sql
-- (psql needs -f/-i — a bare `< file` pipe silently no-ops in some exec paths.)
--
-- This batch resolves every table the batch-3 dynamic generation excluded.
-- Each decision was made against the table's actual access paths:
--
-- APPLIED (11) — org-scoped data, NULL-bypass keeps pre-auth/unpinned paths working:
--   users                  login/refresh resolve identity BEFORE org pinning
--                          (NULL-bypass); authenticated requests get
--                          DB-enforced org isolation on user rows. Global
--                          email UNIQUE constraint (users_email_key) is the
--                          cross-org duplicate guard the service layer lost
--                          when its check became org-filtered.
--   refresh_tokens         same pre-tenant auth shape as users
--   revoked_tokens
--   analytics_events       telemetry written org-scoped; platform-wide reads
--   audit_logs             run in background/unpinned contexts (bypass)
--   audit_events
--   response_cache_entries cached ANSWER TEXT — cross-org cache replay would
--                          leak data; scope=shared is per-AGENT and agents
--                          belong to an org, so org-scoping is correct
--   q_sql_examples         few-shot examples contain schema vocabulary
--   semantic_guidelines    org-authored guidance
--   report_recipes         per-KB org content
--   removed_curated_urls   crawler bookkeeping (defensive)
--
-- KEPT OUT (10) — by-design cross-org, with rationale:
--   organizations          tenant root; global slug/email resolution
--   market_agents          global catalogs / marketplace — RLS would empty
--   tools                  tenant B's tool registry (909 catalog rows are
--   external_agents       meant to be shared). Future: global-flag + custom
--   external_skills       policy when per-org catalogs exist.
--   skill_sources
--   resource_access_policies  sharing ACLs ARE the isolation mechanism;
--   resource_shares            org RLS could hide a grant's target from its
--   resource_registry          grantee path. Service layer enforces org
--   chat_shares                bounds; future custom policy:
--                              (org_id = current_org OR shared_with_user = jwt.sub)

DO $$
DECLARE
    t text;
BEGIN
    FOREACH t IN ARRAY ARRAY[
        'users',
        'refresh_tokens',
        'revoked_tokens',
        'analytics_events',
        'audit_logs',
        'audit_events',
        'response_cache_entries',
        'q_sql_examples',
        'semantic_guidelines',
        'report_recipes',
        'removed_curated_urls'
    ]
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
        RAISE NOTICE 'RLS batch4 enabled on %', t;
    END LOOP;
END
$$;

-- Verify: SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class
-- WHERE relname IN (...) — all must be true/true.
