-- Zhanlu PostgreSQL bootstrap
-- Creates the database (handled by POSTGRES_DB env var in compose)
-- and required extensions.

-- Enable UUID generation
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- Enable full-text search for message/artifact search
CREATE EXTENSION IF NOT EXISTS "pg_trgm";

-- Ensure the database uses UTF-8 (default on alpine images)
-- No additional schema here — Alembic migrations handle table creation.
