# GreyQueue

A PostgreSQL-backed distributed job engine built to make transaction boundaries, concurrent claims and worker execution understandable.

**v0.1: Milestone 0 + working vertical slice.** Submit → queue → claim → execute → persist → retrieve. Includes a FastAPI coordinator, independent Python workers, controlled task registry, transactional results, Alembic migration, CLI and Docker Compose.

Read [the architecture](docs/architecture.md) for state machines, schema, race conditions, tradeoffs and roadmap. [Smoke evidence](docs/smoke-result.json) records the actual one-coordinator/three-worker demonstration.

## Quick start with Docker

Install Docker Compose and copy `.env.example` to `.env`. Replace all three placeholder secrets with independent random values (use URL-safe characters for POSTGRES_PASSWORD). Then:

```sh
docker compose up --build --scale worker=3
```

API: http://127.0.0.1:8810/docs. Authorize client operations with `Authorization: Bearer <CLIENT_TOKEN>`. Internal worker endpoints require WORKER_TOKEN. PostgreSQL is not published to the host. `docker compose down` keeps the database volume; do not use `-v` unless intentionally removing data.

```sh
curl -H "Authorization: Bearer <CLIENT_TOKEN>" -H "Content-Type: application/json" -d '{"task":"calculate_pi","args":{"iterations":10000}}' http://127.0.0.1:8810/jobs
curl -H "Authorization: Bearer <CLIENT_TOKEN>" http://127.0.0.1:8810/jobs/<id>
```

## Native Windows development

Requires Python 3.12+, uv and PostgreSQL 18 binaries. From this directory:

```powershell
uv sync --frozen
.\.venv\Scripts\python.exe scripts/local_db.py start
.\.venv\Scripts\alembic.exe upgrade head
.\.venv\Scripts\python.exe scripts/demo.py
```

The local database helper creates only `.runtime/postgres`, listens on loopback port 55441 and generates ignored `.env` credentials. Override POSTGRES_BIN if the binaries are installed elsewhere. It refuses to overwrite an existing .env when initializing. The demo owns and stops its coordinator and three worker processes; PostgreSQL remains available until:

```powershell
.\.venv\Scripts\python.exe scripts/local_db.py stop
```

For interactive use, run `uv run uvicorn greyqueue.api:create_app --factory --host 127.0.0.1 --port 8810` and `uv run greyqueue-worker` in three separate terminals. Let each worker generate a unique ID; do not share WORKER_ID between live processes. Use `uv run greyqueue submit calculate_pi --args '{"iterations":10000}'`, `greyqueue get <id>`, `greyqueue jobs` and `greyqueue workers`.

## Validation

```powershell
uv run ruff check .
uv run ruff format --check .
$env:TEST_DATABASE_URL = uv run python -c "from greyqueue.config import settings; print(settings().database_url)"
uv run pytest -q
uv run python scripts/demo.py
```

Integration tests create/drop uniquely named schemas in TEST_DATABASE_URL; point it at the isolated development database, never production. Without this variable, database tests explicitly skip. The migration is applied separately before the network demo. Tasks currently available: `sleep` (0–5 seconds), `calculate_pi` (1–1,000,000 iterations), `hash_text` (at most 10,000 characters). Each task executes in a subprocess with a 15-second timeout.

## Honest limits

- One in-flight task per worker; approximate FIFO under concurrent claims.
- LEASED is currently a nonexpiring assignment. Worker loss leaves work stuck. No recovery, retries of execution, dead letters or at-least-once guarantee yet.
- Transport retries repeat protocol messages; identical completion is accepted without a second result. Submissions are not deduplicated.
- Only queued jobs can be cancelled. Running cancellation, priorities, advanced scheduling and pool strategies are later work.
- Heartbeats are recorded, but stale workers are not detected or removed. HEALTHY is registration/last-observation metadata.
- Local bearer tokens are not multi-tenant security. Use only trusted workers and loopback access. Remote production deployment, TLS, secret rotation and per-worker identities are future hardening.
- Structured worker logs and a smoke report are included; full metrics and benchmarks are deferred.
