-- 002_app_user.sql
-- Create a restricted application user for the MCP server.
--
-- The app only needs DML on pois/category_stats/pipeline_runs and SELECT on
-- admin_boundaries. It should NOT connect as the postgres superuser.
--
-- This script runs automatically on first postgres container start (initdb.d).
-- For an existing deployment, run it manually:
--   docker exec srilanka-geo-postgres psql -U postgres -d srilanka_geo -f /docker-entrypoint-initdb.d/002_app_user.sql
--
-- After applying, update DATABASE_URL in .env:
--   DATABASE_URL=postgresql://srilanka_app:<password>@postgres:5432/srilanka_geo

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'srilanka_app') THEN
        -- Password is set via ALTER ROLE after creation so it doesn't appear in pg_stat_activity
        CREATE ROLE srilanka_app LOGIN;
    END IF;
END
$$;

-- Set password separately (replace with a strong password in production)
-- In a real deploy, pass this via an env var or secrets manager, not hardcoded here.
-- ALTER ROLE srilanka_app PASSWORD 'changeme_app';

GRANT CONNECT ON DATABASE srilanka_geo TO srilanka_app;
GRANT USAGE ON SCHEMA public TO srilanka_app;

-- pois: SELECT (all tools) + UPDATE (soft deletes, qdrant_id sync)
GRANT SELECT, UPDATE ON TABLE pois TO srilanka_app;

-- admin_boundaries: SELECT only (loaded once, never modified by the server)
GRANT SELECT ON TABLE admin_boundaries TO srilanka_app;

-- category_stats: full DML (refreshed by pipeline after each ingest)
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE category_stats TO srilanka_app;

-- pipeline_runs: INSERT + UPDATE (server records ingest runs)
GRANT SELECT, INSERT, UPDATE ON TABLE pipeline_runs TO srilanka_app;

-- Sequences (needed for SERIAL primary keys in category_stats, pipeline_runs)
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO srilanka_app;

-- Future tables: grant on new objects automatically
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE ON TABLES TO srilanka_app;
