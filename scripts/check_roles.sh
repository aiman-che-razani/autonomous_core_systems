#!/usr/bin/env bash
# Verify the least-privilege runtime role inside a running Compose project (ADR 008).
# Usage: bash scripts/check_roles.sh <compose-project> [env-file]
# Writes happen only inside rolled-back transactions or as no-op UPDATEs.
set -euo pipefail
project="${1:?usage: check_roles.sh <compose-project> [env-file]}"
env_file="${2:-.env}"
# Same precedence as Compose: an exported shell variable wins over the env file; the last
# assignment in the file wins, and surrounding quotes are not part of the value.
if [ -z "${APP_DB_PASSWORD:-}" ] && [ -f "$env_file" ]; then
  APP_DB_PASSWORD="$(grep -E '^APP_DB_PASSWORD=' "$env_file" | tail -n 1 | cut -d= -f2- | tr -d '\r"'"'")"
fi
if [ -z "${APP_DB_PASSWORD:-}" ]; then
  echo "APP_DB_PASSWORD is not set in the environment or $env_file" >&2
  exit 2
fi
PGPASSWORD="$APP_DB_PASSWORD"
export PGPASSWORD  # passed by name to `exec -e`, so it never appears on a command line

# -h postgres, not 127.0.0.1: the image's pg_hba trusts loopback, so a loopback connection
# would never check the password. The container's network address uses scram-sha-256.
as_app() {
  docker compose -p "$project" --env-file "$env_file" exec -T -e PGPASSWORD postgres \
    psql -h postgres -U greyqueue_app -d greyqueue -v ON_ERROR_STOP=1 -tAc "$1"
}

failures=0
fail() {
  echo "FAIL  $1"
  failures=$((failures + 1))
}
expect() {  # expect allow|deny "<sql>"
  local output outcome
  if output="$(as_app "$2" 2>&1)"; then
    outcome=allow
  elif grep -q "permission denied" <<<"$output"; then
    outcome=deny
  else
    # A typo, a missing column or a lost connection is not evidence of a denial.
    fail "error (not a permission denial): $2: $(head -n 1 <<<"$output")"
    return
  fi
  if [ "$outcome" = "$1" ]; then
    echo "ok    $1  $2"
  else
    fail "expected $1, got $outcome: $2"
  fi
}
rolled_back() { echo "BEGIN; $1; ROLLBACK"; }

# Password authentication is really enforced.
if refusal="$(PGPASSWORD="wrong-$APP_DB_PASSWORD" as_app "SELECT 1" 2>&1)"; then
  fail "a wrong password was accepted"
elif grep -q "password authentication failed" <<<"$refusal"; then
  echo "ok    a wrong password is refused"
else
  fail "wrong-password probe failed for another reason: $(head -n 1 <<<"$refusal")"
fi
[ "$(as_app "SELECT rolsuper FROM pg_roles WHERE rolname = current_user")" = "f" ] \
  && echo "ok    greyqueue_app is not a superuser" \
  || fail "greyqueue_app is a superuser"

# What the coordinator needs.
expect allow "SELECT count(*) FROM jobs"
expect allow "UPDATE jobs SET priority = priority WHERE false"
expect allow "UPDATE attempts SET fence = fence WHERE false"
expect allow "UPDATE workers SET state = state"
expect allow "$(rolled_back "INSERT INTO system_events(kind) VALUES ('role_probe')")"
expect allow "$(rolled_back "INSERT INTO events(job_id, state) SELECT id, 'PROBE' FROM jobs LIMIT 1")"
expect allow "$(rolled_back "INSERT INTO results(job_id) SELECT id FROM jobs WHERE false")"
expect allow "SELECT count(*) FROM events"

# Nothing more: no DDL, no deletes, append-only history.
expect deny "CREATE TABLE audit_probe (i int)"
expect deny "DELETE FROM jobs"
expect deny "TRUNCATE jobs"
expect deny "TRUNCATE events"
expect deny "DELETE FROM events"
expect deny "DELETE FROM results"
expect deny "DELETE FROM system_events"
expect deny "UPDATE events SET state = state"
expect deny "UPDATE results SET error = error"
expect deny "UPDATE system_events SET kind = kind"
expect deny "SELECT * FROM alembic_version"

# Every application table has an explicit grant (docker/db-roles.sql revokes everything
# else, so a table added by a migration but not to the GRANT lines would break the app).
ungranted="$(as_app "SELECT string_agg(tablename, ',') FROM pg_tables WHERE schemaname = 'public'
  AND tablename <> 'alembic_version'
  AND NOT has_table_privilege(format('%I.%I', schemaname, tablename), 'SELECT')")"
[ -z "$ungranted" ] && echo "ok    every application table is granted" \
  || fail "tables without a grant for greyqueue_app: $ungranted"
[ "$(as_app "SELECT count(*) FROM pg_default_acl WHERE defaclobjtype = 'r'
  AND array_to_string(defaclacl, ',') LIKE '%greyqueue_app=%'")" = "0" ] \
  && echo "ok    no default table privileges" \
  || fail "a default privilege still grants future tables to greyqueue_app"

if [ "$failures" -ne 0 ]; then
  echo "$failures privilege check(s) failed" >&2
  exit 1
fi
echo "PASS: greyqueue_app has exactly the intended privileges"
