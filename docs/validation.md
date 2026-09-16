# Validation record

Validated locally on Windows with Python 3.12.14 and an isolated PostgreSQL 18 cluster.

- 13 automated tests: task bounds, concurrent single-winner claims, independent claims, capacity, ownership, illegal transitions, completion replay, result rollback, terminal failure, cancellation race, HTTP authentication, request bounds and coordinator-instance persistence.
- Ruff lint and formatting passed.
- Initial Alembic migration applied successfully; `alembic check` detected no schema drift.
- One real HTTP coordinator plus three worker processes completed all 30 submitted jobs. Distribution: 10 / 11 / 9 jobs. Measured end-to-end time: 3.672 seconds. This includes HTTP submission/polling and is a smoke result, not a benchmark. See smoke-result.json.
- Docker Compose configuration validated. Container build/runtime was not tested locally because the Docker daemon was stopped.
- CI configuration repeats PostgreSQL migration, lint, tests and the three-worker demo on Linux. Remote CI outcome must be checked after pushing; local results do not imply CI passed.

The upstream TestClient currently emits two deprecation warnings; tests pass without suppressing them.
