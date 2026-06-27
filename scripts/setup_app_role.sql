-- Non-superuser application role so Row-Level Security is ACTUALLY enforced (T1).
--
-- WHY THIS EXISTS
-- ---------------
-- The migrations create RLS policies with FORCE ROW LEVEL SECURITY, which subjects the
-- table OWNER to the policy. But a Postgres SUPERUSER (and any role with BYPASSRLS)
-- bypasses RLS unconditionally — FORCE does not change that. The docker-compose
-- POSTGRES_USER ('crm') is a superuser, so if the app connects as 'crm' the policies are
-- a no-op and cross-tenant isolation is NOT enforced.
--
-- Fix: the application (and the polling workers) must connect as a NOSUPERUSER,
-- NOBYPASSRLS role. This script creates that role and grants it exactly the DML it needs.
-- Point DATABASE_URL at this role:
--   DATABASE_URL=postgresql+psycopg://crm_app:crm_app@localhost:5432/crm
--
-- This runs automatically on first `docker compose up` (mounted into
-- /docker-entrypoint-initdb.d/), BEFORE migrations exist. The role + ALTER DEFAULT
-- PRIVILEGES below are what matter at init time: every table the 'crm' owner later
-- creates via migrations inherits the crm_app grants automatically. The GRANT-ON-ALL
-- statements are a no-op at init (no tables yet) but make the script also correct to
-- re-run by hand against an already-migrated DB:
--   docker exec -i personal-crm-db-1 psql -U crm -d crm -f - < scripts/setup_app_role.sql
-- Idempotent: safe to re-run.

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'crm_app') THEN
        CREATE ROLE crm_app LOGIN PASSWORD 'crm_app'
            NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;
    END IF;
END
$$;

GRANT USAGE ON SCHEMA public TO crm_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO crm_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO crm_app;

-- Future tables (later migrations) inherit the same grants automatically.
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO crm_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO crm_app;
