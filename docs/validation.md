# Release validation

## Automated checks

38 tests passed against PostgreSQL and Python 3.12.14 on Windows. Coverage includes concurrent claims, capacity, priority/capability filtering, scheduled dependencies, deduplication, retry/backoff, expiry/fencing, worker identity, draining, timeouts across four executors, HTTP bounds/TLS enforcement and v0.1 migration preservation with downgrade/re-upgrade.

Ruff lint and formatting pass. Alembic reports no schema drift. Two upstream TestClient deprecation warnings remain visible; they do not fail tests.

## Phase evidence

| Phase | Runnable demonstration | Recorded evidence |
| --- | --- | --- |
| v0.2 scheduling/concurrency | `python -m scripts.experiments` and benchmark matrix | [100 distributed jobs](results/experiments.json), [12 benchmark cases](results/benchmark-100-matrix.json) |
| v0.3 recovery | `python -m scripts.experiments --database-outage` | [Worker/coordinator/database failures](results/experiments.json) |
| v0.4 retries/idempotency | `python -m scripts.side_effects` | [Two executions, one deduplicated effect](results/side-effects.json) |
| v0.5 operations | `python -m scripts.check_dashboard` | [Browser checks](results/dashboard-check.json), desktop/mobile screenshots |
| v0.6 measurements | `python -m benchmarks.run --jobs 10000 --strategy hybrid --workload light` | [10,000 jobs](results/benchmark-10000-hybrid-light.json), [1,000 jobs](results/benchmark-1000-hybrid-mixed.json), [read optimization](results/read-optimization.json) |
| v1.0 hardening/extensions | Tests + two-coordinator experiment + Compose check | [Process experiments](results/experiments.json), [Docker verification](results/compose.json), security/migration tests |

All seven native process experiments passed, including an actual temporary outage of the isolated project PostgreSQL cluster. Browser checks passed with zero JavaScript errors and no horizontal overflow at a 390-pixel viewport. Both side-effect experiments used real child process exits after independent sink commits.

The benchmark matrix and larger runs completed with zero execution failures/retries. These are single-machine observations with full environment metadata, not production capacity claims. Historical v0.1 evidence is preserved separately in [v0.1-smoke.json](results/v0.1-smoke.json).

CI repeats PostgreSQL tests, migration checks, process/browser demonstrations and an image build. Its remote outcome is separate from these local results.

Docker Compose was built and run locally with PostgreSQL and three Linux worker containers. All 100 submitted jobs succeeded, distributed 34 / 33 / 33, with working dashboard and metrics endpoints. Verification containers were stopped afterwards; their named database volume is preserved.
