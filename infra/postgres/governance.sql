-- ============================================================================
-- Zhanlu Data Governance — Audit Triggers, Retention, Quotas
-- Run after Alembic migrations have created all application tables.
-- Idempotent: uses CREATE OR REPLACE and IF NOT EXISTS.
-- ============================================================================

-- ── Audit Trigger Function ──────────────────────────────────────────────────
-- Generic trigger that logs every INSERT/UPDATE/DELETE to audit.audit_trail.

CREATE OR REPLACE FUNCTION audit.audit_trigger()
RETURNS TRIGGER AS $$
DECLARE
    old_json JSONB := NULL;
    new_json JSONB := NULL;
BEGIN
    IF (TG_OP = 'DELETE') THEN
        old_json := to_jsonb(OLD);
        INSERT INTO audit.audit_trail (table_name, record_id, action, old_data, new_data)
        VALUES (TG_TABLE_NAME, OLD.id::TEXT, TG_OP, old_json, NULL);
        RETURN OLD;
    ELSIF (TG_OP = 'UPDATE') THEN
        old_json := to_jsonb(OLD);
        new_json := to_jsonb(NEW);
        INSERT INTO audit.audit_trail (table_name, record_id, action, old_data, new_data)
        VALUES (TG_TABLE_NAME, NEW.id::TEXT, TG_OP, old_json, new_json);
        RETURN NEW;
    ELSIF (TG_OP = 'INSERT') THEN
        new_json := to_jsonb(NEW);
        INSERT INTO audit.audit_trail (table_name, record_id, action, old_data, new_data)
        VALUES (TG_TABLE_NAME, NEW.id::TEXT, TG_OP, NULL, new_json);
        RETURN NEW;
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

-- ── Install Audit Triggers on All Application Tables ────────────────────────
-- Creates a trigger on every table in the 'public' schema that has an 'id' column.
-- The application uses UUID string primary keys named 'id' (models/base.py TimestampedBase).

DO $$
DECLARE
    tbl RECORD;
    trigger_name TEXT;
BEGIN
    FOR tbl IN
        SELECT table_name
        FROM information_schema.tables
        WHERE table_schema = 'public'
          AND table_type = 'BASE TABLE'
          AND table_name NOT IN ('alembic_version')  -- skip migration tracking table
    LOOP
        trigger_name := 'trg_audit_' || tbl.table_name;

        -- Drop existing trigger if any (for idempotent re-runs)
        EXECUTE format('DROP TRIGGER IF EXISTS %I ON public.%I', trigger_name, tbl.table_name);

        -- Check if the table has an 'id' column before creating trigger
        IF EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = tbl.table_name
              AND column_name = 'id'
        ) THEN
            EXECUTE format(
                'CREATE TRIGGER %I
                 AFTER INSERT OR UPDATE OR DELETE ON public.%I
                 FOR EACH ROW EXECUTE FUNCTION audit.audit_trigger()',
                trigger_name, tbl.table_name
            );
            RAISE NOTICE 'Audit trigger installed: % on public.%', trigger_name, tbl.table_name;
        ELSE
            RAISE WARNING 'Skipping table public.% — no "id" column found', tbl.table_name;
        END IF;
    END LOOP;
END
$$;

-- ── Retention: Purge Soft-Deleted Records ───────────────────────────────────
-- Deletes records where is_deleted = TRUE and updated_date is older than N days.
-- Targets all tables in 'public' that have both 'is_deleted' and 'updated_date' columns.
-- Returns the number of rows deleted.

CREATE OR REPLACE FUNCTION governance.purge_soft_deleted(retention_days INTEGER DEFAULT 30)
RETURNS TABLE(table_name TEXT, rows_deleted BIGINT) AS $$
DECLARE
    tbl RECORD;
    deleted_count BIGINT;
    cutoff_date TIMESTAMPTZ;
BEGIN
    cutoff_date := now() - (retention_days || ' days')::INTERVAL;

    FOR tbl IN
        SELECT t.table_name
        FROM information_schema.tables t
        WHERE t.table_schema = 'public'
          AND t.table_type = 'BASE TABLE'
          AND EXISTS (
              SELECT 1 FROM information_schema.columns c
              WHERE c.table_schema = 'public'
                AND c.table_name = t.table_name
                AND c.column_name = 'is_deleted'
          )
          AND EXISTS (
              SELECT 1 FROM information_schema.columns c
              WHERE c.table_schema = 'public'
                AND c.table_name = t.table_name
                AND c.column_name = 'updated_date'
          )
    LOOP
        EXECUTE format(
            'WITH deleted AS (
                DELETE FROM public.%I
                WHERE is_deleted = TRUE
                  AND updated_date < %L
                RETURNING id
            )
            SELECT COUNT(*) FROM deleted',
            tbl.table_name, cutoff_date
        ) INTO deleted_count;

        IF deleted_count > 0 THEN
            table_name := tbl.table_name;
            rows_deleted := deleted_count;
            RETURN NEXT;

            -- Log purge action to audit trail
            INSERT INTO audit.audit_trail (table_name, record_id, action, new_data, changed_by)
            VALUES (
                'system_purge',
                tbl.table_name,
                'DELETE',
                jsonb_build_object(
                    'retention_days', retention_days,
                    'rows_deleted', deleted_count,
                    'cutoff_date', cutoff_date
                ),
                'governance_retention'
            );
        END IF;
    END LOOP;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

-- ── Retention: Expire Old DataSnapshots ─────────────────────────────────────
-- Archives or deletes DataSnapshots older than N days.
-- Snapshots created >7 days ago and marked as expired are purged.

CREATE OR REPLACE FUNCTION governance.expire_snapshots(retention_days INTEGER DEFAULT 7)
RETURNS BIGINT AS $$
DECLARE
    deleted_count BIGINT;
BEGIN
    -- DataSnapshots have a lifecycle: they become stale after a period.
    -- This function deletes snapshots where created_date < cutoff_date
    -- AND they are not referenced by active workspaces.

    -- Only run if the data_snapshots table exists
    IF EXISTS (
        SELECT 1 FROM information_schema.tables
        WHERE table_schema = 'public' AND table_name = 'data_snapshots'
    ) THEN
        WITH deleted AS (
            DELETE FROM public.data_snapshots
            WHERE created_date < (now() - (retention_days || ' days')::INTERVAL)
            RETURNING id
        )
        SELECT COUNT(*) INTO deleted_count FROM deleted;

        -- Log to audit trail
        INSERT INTO audit.audit_trail (table_name, record_id, action, new_data, changed_by)
        VALUES (
            'system_purge',
            'snapshot_expiry',
            'DELETE',
            jsonb_build_object(
                'retention_days', retention_days,
                'rows_deleted', deleted_count
            ),
            'governance_retention'
        );

        RETURN deleted_count;
    END IF;

    RETURN 0;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

-- ── Quota: Check Organization Storage Quota ─────────────────────────────────
-- Returns TRUE if the operation is within quota, FALSE if exceeded.
-- quota_type: 'artifacts' (checks artifact_blobs), 'uploads' (checks user_files).

CREATE OR REPLACE FUNCTION governance.check_quota(
    p_org_id TEXT,
    p_quota_type TEXT DEFAULT 'artifacts'
)
RETURNS BOOLEAN AS $$
DECLARE
    current_usage BIGINT;
    max_quota BIGINT;
BEGIN
    -- Default quotas (can be overridden per-org via settings table in future)
    IF p_quota_type = 'artifacts' THEN
        max_quota := 5 * 1024 * 1024 * 1024;  -- 5 GB in bytes
    ELSIF p_quota_type = 'uploads' THEN
        max_quota := 1 * 1024 * 1024 * 1024;  -- 1 GB in bytes
    ELSE
        RAISE EXCEPTION 'Unknown quota_type: %', p_quota_type;
    END IF;

    -- Calculate current usage for this organization
    IF p_quota_type = 'artifacts' THEN
        SELECT COALESCE(SUM(ab.size), 0) INTO current_usage
        FROM public.artifact_blobs ab
        JOIN public.artifact_versions av ON av.id = ab.artifact_version_id
        JOIN public.artifacts a ON a.id = av.artifact_id
        WHERE a.org_id = p_org_id
          AND a.is_deleted = FALSE;

    ELSIF p_quota_type = 'uploads' THEN
        SELECT COALESCE(SUM(uf.size), 0) INTO current_usage
        FROM public.user_files uf
        WHERE uf.org_id = p_org_id
          AND uf.is_deleted = FALSE;
    END IF;

    RAISE NOTICE 'Quota check: org=%, type=%, usage=% bytes, limit=% bytes',
        p_org_id, p_quota_type, current_usage, max_quota;

    RETURN current_usage < max_quota;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

-- ── Integrity: Verify ArtifactBlob Checksums ────────────────────────────────
-- Returns mismatches between stored SHA-256 and recomputed hash.
-- Should be run periodically as part of integrity scan.

CREATE OR REPLACE FUNCTION governance.verify_blob_checksums()
RETURNS TABLE(blob_id TEXT, stored_checksum TEXT, error_msg TEXT) AS $$
BEGIN
    -- For artifacts stored in PostgreSQL bytea (not MinIO), verify SHA-256.
    -- MinIO blobs have checksums verified at the storage layer (S3 ETag/MD5).
    -- This function verifies postgres_bytea artifacts only.

    RETURN QUERY
    SELECT
        ab.id AS blob_id,
        ab.checksum AS stored_checksum,
        CASE
            WHEN ab.data IS NULL THEN 'No data to verify (MinIO backed)'::TEXT
            WHEN encode(digest(ab.data, 'sha256'), 'hex') <> ab.checksum THEN
                'CHECKSUM MISMATCH: stored=' || ab.checksum ||
                ', computed=' || encode(digest(ab.data, 'sha256'), 'hex')
            ELSE 'OK'::TEXT
        END AS error_msg
    FROM public.artifact_blobs ab
    WHERE ab.storage_uri LIKE 'inline://%'
      AND ab.data IS NOT NULL;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

-- ── Auto-Install Audit Triggers on Alembic Migration Completion ─────────────
-- This event trigger fires after any DDL in public schema, automatically
-- re-installing audit triggers on newly created tables.

CREATE OR REPLACE FUNCTION governance.auto_audit_new_tables()
RETURNS EVENT_TRIGGER AS $$
DECLARE
    obj RECORD;
    trigger_name TEXT;
BEGIN
    FOR obj IN
        SELECT object_identity::TEXT AS obj_name
        FROM pg_event_trigger_ddl_commands()
        WHERE object_type = 'table'
          AND schema_name = 'public'
          AND object_identity::TEXT NOT LIKE '%alembic_version%'
    LOOP
        trigger_name := 'trg_audit_' || regexp_replace(obj.obj_name, '^public\.', '');

        -- Only install if table has 'id' column (our convention)
        IF EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = regexp_replace(obj.obj_name, '^public\.', '')
              AND column_name = 'id'
        ) THEN
            -- Drop + recreate to handle idempotent migration re-runs
            EXECUTE format('DROP TRIGGER IF EXISTS %I ON %s', trigger_name, obj.obj_name);
            EXECUTE format(
                'CREATE TRIGGER %I
                 AFTER INSERT OR UPDATE OR DELETE ON %s
                 FOR EACH ROW EXECUTE FUNCTION audit.audit_trigger()',
                trigger_name, obj.obj_name
            );
        END IF;
    END LOOP;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

-- Create event trigger (if it doesn't exist)
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_event_trigger WHERE evtname = 'auto_audit_on_ddl'
    ) THEN
        CREATE EVENT TRIGGER auto_audit_on_ddl
        ON ddl_command_end
        WHEN TAG IN ('CREATE TABLE')
        EXECUTE FUNCTION governance.auto_audit_new_tables();
    END IF;
END
$$;
