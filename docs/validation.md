# Release validation

## Automated checks

56 tests passed against PostgreSQL and Python 3.12.14 on Windows (31 run against a real PostgreSQL schema; 25 need no database). Coverage includes concurrent claims, capacity, priority/capability filtering, scheduled dependencies, deduplication, retry/backoff, expiry/fencing, claim replay after a worker changes state, renewal only after start, worker identity and DEAD-ID re-registration, draining, timeouts across four executors, oversized/unstorable task results, the real worker loop against a mock coordinator (rejected results, revoked sessions), process pools that start every process up front, idempotent-replay status codes, uniform 422 bodies, HTTP bounds, host-header and malformed-credential rejection, TLS-required (426) rejection, Prometheus type lines, and v0.1 migration preservation with CHECK-constraint verification, downgrade and re-upgrade.

Ruff lint and formatting pass. Alembic reports no schema drift, with server-default comparison enabled. Two upstream TestClient deprecation warnings remain visible; they do not fail tests.

## Phase evidence

| Phase | Runnable demonstration | Recorded evidence |
| --- | --- | --- |
| v0.2 scheduling/concurrency | `python -m scripts.experiments` and benchmark matrix | [100 distributed jobs](results/experiments.json), [12 benchmark cases](results/benchmark-100-matrix.json) |
| v0.3 recovery | `python -m scripts.experiments --database-outage` | [Worker/coordinator/database failures](results/experiments.json) |
| v0.4 retries/idempotency | `python -m scripts.side_effects` | [Two executions, one deduplicated effect](results/side-effects.json) |
| v0.5 operations | `python -m scripts.check_dashboard` | [Browser checks](results/dashboard-check.json), desktop/mobile screenshots |
| v0.6 measurements | `python -m benchmarks.run --jobs 10000 --strategy hybrid --workload light` | [10,000 jobs](results/benchmark-10000-hybrid-light.json), [1,000 jobs](results/benchmark-1000-hybrid-mixed.json), [read optimization](results/read-optimization.json) |
| v1.0 hardening/extensions | Tests + two-coordinator experiment + Compose check | [Process experiments](results/experiments.json), [Docker verification](results/compose.json), security/migration tests |

Apart from `compose.json`, the files in `results/` were recorded at the v1.0 release. The post-release audit fixes (claim replay, renewal, worker error handling, queue indexes, dashboard states, container hardening) were verified by the automated tests above, a local multi-process smoke run and the Compose run below; the recorded process, browser and benchmark evidence has not yet been regenerated for them. Re-run the demonstrations above to refresh it.

At v1.0, all seven native process experiments passed, including an actual temporary outage of the isolated project PostgreSQL cluster. Browser checks passed with zero JavaScript errors and no horizontal overflow at a 390-pixel viewport. Both side-effect experiments used real child process exits after independent sink commits.

The benchmark matrix and larger runs completed with zero execution failures/retries. These are single-machine observations with full environment metadata, not production capacity claims. Historical v0.1 evidence is preserved separately in [v0.1-smoke.json](results/v0.1-smoke.json).

CI repeats PostgreSQL tests, migration checks, process/browser demonstrations, an image build and the full Compose check. It does not run the database-outage experiment or the benchmarks. Its remote outcome is separate from these local results.

Docker Compose was rebuilt and run locally after the audit fixes, from digest-pinned images, with PostgreSQL, the migration, the least-privilege role step and three Linux worker containers. All 100 submitted jobs succeeded, distributed 34 / 33 / 33, with working dashboard and metrics endpoints ([compose.json](results/compose.json)). In the same run the coordinator was confirmed to connect as the non-superuser `greyqueue_app`, which could read and update rows but was refused CREATE TABLE, DELETE, TRUNCATE and `alembic_version`; a volume with existing history but no role was upgraded by the next `up` (run twice, both idempotent); and a rebinding Host header got 400. Those role and upgrade checks were run by hand and are not part of `compose.json`. The stack was run under a separate Compose project and removed afterwards.
