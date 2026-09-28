-- Least-privilege runtime role for the coordinator. Run by the Compose `db-roles` service as
-- the database owner after every migration; idempotent, so it also upgrades old volumes.
-- The password arrives as the session setting greyqueue.app_password (PGOPTIONS), never as
-- a command-line argument or a psql variable.
DO $$
DECLARE
    secret text := current_setting('greyqueue.app_password');
BEGIN
    IF length(secret) < 16 THEN
        RAISE EXCEPTION 'APP_DB_PASSWORD must be at least 16 characters';
    END IF;
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'greyqueue_app') THEN
        EXECUTE format('CREATE ROLE greyqueue_app LOGIN PASSWORD %L', secret);
    ELSE
        EXECUTE format('ALTER ROLE greyqueue_app WITH LOGIN PASSWORD %L', secret);
    END IF;
END $$;

ALTER ROLE greyqueue_app NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;

-- Nobody but the owner and the app may connect or create objects.
REVOKE ALL ON DATABASE greyqueue FROM PUBLIC;
GRANT CONNECT ON DATABASE greyqueue TO greyqueue_app;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO greyqueue_app;

-- The application reads, inserts and updates rows; it never deletes or changes the schema.
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM greyqueue_app;
GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA public TO greyqueue_app;
REVOKE ALL ON alembic_version FROM greyqueue_app;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO greyqueue_app;

-- Tables created by future migrations (run as the owner) get the same grants.
ALTER DEFAULT PRIVILEGES FOR ROLE greyqueue IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE ON TABLES TO greyqueue_app;
ALTER DEFAULT PRIVILEGES FOR ROLE greyqueue IN SCHEMA public
    GRANT USAGE ON SEQUENCES TO greyqueue_app;
