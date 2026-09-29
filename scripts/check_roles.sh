#!/usr/bin/env bash
# Verify the least-privilege runtime role inside a running Compose project (ADR 008).
# Usage: bash scripts/check_roles.sh <compose-project> [env-file]
# Read-only apart from a no-op UPDATE; every denied statement must fail.
set -euo pipefail
project="${1:?usage: check_roles.sh <compose-project> [env-file]}"
env_file="${2:-.env}"
PGPASSWORD="$(grep -E '^APP_DB_PASSWORD=' "$env_file" | cut -d= -f2-)"
export PGPASSWORD  # passed by name to `exec -e`, so it never appears on a command line

as_app() {
  docker compose -p "$project" --env-file "$env_file" exec -T -e PGPASSWORD postgres \
    psql -h 127.0.0.1 -U greyqueue_app -d greyqueue -v ON_ERROR_STOP=1 -tAc "$1"
}

failures=0
expect() {  # expect allow|deny "<sql>"
  if as_app "$2" >/dev/null 2>&1; then outcome=allow; else outcome=deny; fi
  if [ "$outcome" = "$1" ]; then
    echo "ok    $1  $2"
  else
    echo "FAIL  expected $1, got $outcome: $2"
    failures=$((failures + 1))
  fi
}

expect allow "SELECT count(*) FROM jobs"
expect allow "UPDATE workers SET state = state"
expect allow "SELECT count(*) FROM events"
[ "$(as_app "SELECT rolsuper FROM pg_roles WHERE rolname = current_user")" = "f" ] \
  && echo "ok    greyqueue_app is not a superuser" \
  || { echo "FAIL  greyqueue_app is a superuser"; failures=$((failures + 1)); }
expect deny "CREATE TABLE audit_probe (i int)"
expect deny "DELETE FROM jobs"
expect deny "TRUNCATE events"
expect deny "UPDATE events SET state = state"
expect deny "UPDATE results SET error = error"
expect deny "SELECT * FROM alembic_version"

if [ "$failures" -ne 0 ]; then
  echo "$failures privilege check(s) failed" >&2
  exit 1
fi
echo "PASS: greyqueue_app has exactly the intended privileges"
