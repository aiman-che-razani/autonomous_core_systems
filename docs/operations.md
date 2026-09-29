# Operations

How the Compose stack is built, started, checked and backed up. Security properties are in [security](security.md); schema, grants and migrations in [persistence](persistence.md).

## Start order

`postgres` (healthy) → `migrate` (`alembic upgrade head` as the owner) → `db-roles` (the owner runs `docker/db-roles.sql` in one transaction, on every `up`) → `coordinator` (healthy) → `worker` × 3. Each step waits for the previous one to be healthy or to finish successfully, so a failed migration or grant stops the stack before the coordinator starts.

After a Docker daemon restart, the `restart: unless-stopped` services (postgres, coordinator, workers) come back without that ordering. This is harmless: grants persist in the volume, the coordinator retries the database (503 until it answers), and workers retry the coordinator. The one-shot `migrate` and `db-roles` only run on the next `up`.

## Configuration

`python scripts/configure.py` creates `.env` with random secrets, owner-only on POSIX. On an existing `.env`, including a copy of `.env.example`, it fills secrets that are missing, empty or `CHANGE_ME`, in place, and leaves every other line alone; it is safe to re-run. Compose refuses to start while `POSTGRES_PASSWORD`, `APP_DB_PASSWORD`, `CLIENT_TOKEN` or `WORKER_TOKEN` is empty. No service uses `env_file`: each receives an explicit list, so a new setting must be added to `compose.yaml`.

| Setting | Default | Notes |
| --- | --- | --- |
| `WORKER_REPLICAS` | 3 | A plain `docker compose up -d` keeps this size; `--scale worker=N` overrides it for that run. |
| `CAPACITY`, `EXECUTOR`, `POLL_INTERVAL` | 2, subprocess, 0.2 | Per worker. |
| `SCHEDULER`, `LEASE_SECONDS`, `HEARTBEAT_INTERVAL`, `SUSPECT_AFTER`, `DEAD_AFTER` | priority, 10, 2, 6, 12 | Coordinator. |
| `ALLOWED_HOSTS` | 127.0.0.1, localhost, coordinator | Any other hostname clients use gets 400. |
| `REQUIRE_TLS` | false | With true, plain-HTTP requests get 426. Compose workers use plain HTTP inside the network and treat 426 as fatal, so enable it only behind a TLS proxy the workers also use. |

## Networks

`backend` is internal (no route out) and holds postgres, migrate, db-roles and the coordinator. `frontend` holds the coordinator and the workers. Workers therefore reach the API but cannot resolve or connect to the database. Only `127.0.0.1:8810` is published.

## Verifying a change

Use a separate project name, so your own volume is never touched, and remove it afterwards:

```
docker compose -p greyqueue-verify up -d --build --wait --wait-timeout 240
bash scripts/check_roles.sh greyqueue-verify
docker compose -p greyqueue-verify down -v
```

`python -m scripts.check_compose` additionally submits 100 jobs and checks their spread over three workers, the dashboard and the metrics; it rewrites `docs/results/compose.json`. CI runs all of this on every push.

## Backup and restore

See [persistence](persistence.md#backup-and-restore-compose). The commands avoid shell redirection, so they work in PowerShell (whose `>` would corrupt the binary dump) as well as POSIX shells, and restore in one transaction.

## Monitoring

`monitoring/prometheus.yml` and `monitoring/grafana-dashboard.json` are examples; Compose runs neither. Scrape one coordinator (all report the same totals) with the client token from a secret file, and add the scrape hostname to `ALLOWED_HOSTS`. Metric names are listed in [observability](observability.md).
