-- Least-privilege runtime role for the coordinator. Run by the Compose `db-roles` service as
-- the database owner after every migration, in a single transaction (psql -1), so a running
-- coordinator never sees a half-applied grant set. Idempotent: it also upgrades old volumes.
-- The password arrives as the session setting greyqueue.app_password (PGOPTIONS), never as
-- a command-line argument or a psql variable. The owner role and database are both named
-- `greyqueue` (compose.yaml: POSTGRES_USER / POSTGRES_DB).
DO $$
DECLARE
    secret text := current_setting('greyqueue.app_password');
BEGIN
    IF length(secret) < 16 THEN
        RAISE EXCEPTION 'APP_DB_PASSWORD must be at least 16 characters';
    END IF;
    -- It is also embedded in a URL and in PGOPTIONS, where these characters are safe.
    IF secret !~ '^[A-Za-z0-9_-]+$' THEN
        RAISE EXCEPTION 'APP_DB_PASSWORD may only contain letters, digits, "_" and "-"';
    END IF;
    BEGIN
        IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'greyqueue_app') THEN
            EXECUTE format('CREATE ROLE greyqueue_app LOGIN PASSWORD %L', secret);
        ELSE
            EXECUTE format('ALTER ROLE greyqueue_app WITH LOGIN PASSWORD %L', secret);
        END IF;
    EXCEPTION WHEN OTHERS THEN
        -- The default error CONTEXT would quote the statement, password included, into
        -- container logs (and CI artifacts). Report only the SQLSTATE.
        RAISE EXCEPTION 'could not set the greyqueue_app password (SQLSTATE %)', SQLSTATE;
    END;
END $$;

ALTER ROLE greyqueue_app NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;

-- Nobody but the owner and the app may connect or create objects.
REVOKE ALL ON DATABASE greyqueue FROM PUBLIC;
GRANT CONNECT ON DATABASE greyqueue TO greyqueue_app;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO greyqueue_app;

-- The application never deletes rows or changes the schema. Mutable tables get UPDATE;
-- the history tables are append-only, so a compromised coordinator cannot rewrite them.
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM greyqueue_app;
GRANT SELECT, INSERT, UPDATE ON jobs, attempts, workers TO greyqueue_app;
GRANT SELECT, INSERT ON results, events, system_events TO greyqueue_app;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO greyqueue_app;

-- Tables created by future migrations (run as the owner) get read/append/update; narrow
-- them here if they are append-only.
ALTER DEFAULT PRIVILEGES FOR ROLE greyqueue IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE ON TABLES TO greyqueue_app;
ALTER DEFAULT PRIVILEGES FOR ROLE greyqueue IN SCHEMA public
    GRANT USAGE ON SEQUENCES TO greyqueue_app;
