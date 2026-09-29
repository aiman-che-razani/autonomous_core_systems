# Persistence

PostgreSQL is the authority for definitions, states, attempts, leases, results, identities and events. Workers are replaceable; coordinator restarts require no in-memory state reconstruction beyond reading the database. SQLAlchemy sessions are short-lived. Alembic owns schema changes; the application never creates production tables implicitly. Migrations run with a 10-second lock timeout so a blocked `ALTER TABLE` fails instead of queueing every application query behind it. `alembic check` compares server defaults as well as columns and indexes; CHECK constraints are verified by the migration test because Alembic does not compare them.

The v1 migration preserves v0.1 jobs/results, initializes historical attempt counts and adds nullable session hashes. Stop/drain v0.1 workers before upgrading; v1 workers register fresh identities. Unfinished v0.1 assignments expire under the new recovery policy. Back up the database before an upgrade. Downgrade refuses while any attempt is still active, while RETRY_WAIT/DEAD_LETTER jobs exist, or while workers hold concurrent slots, rather than silently changing their meaning. It does deliberately drop the columns and tables that v0.1 cannot represent: priorities, retry settings, idempotency keys, metadata, schedules, dependencies, attempt outcome/output/error, and all of `system_events`.

WAL records committed changes so PostgreSQL can recover after a crash. Durability depends on PostgreSQL's storage/fsync configuration; GreyQueue does not replace database backups or replication. Compose uses a named data volume. `docker compose down` preserves it; removing that volume destroys the stored history.

Jobs, results, attempts, events, system events and DEAD worker rows have no automatic retention deletion. Event ids are 64-bit, so the tables will not overflow their keys. The runtime role has no DELETE privilege (ADR 008), so any retention job needs a deliberate grant change in `docker/db-roles.sql`. Meanwhile, long-running installations must choose archival/retention and monitor database growth; this release does not silently erase history.

## Migrations that need downtime
`7d2e9a41c0b8` rewrites `events` (`bigint` ids) under an ACCESS EXCLUSIVE lock and builds indexes inside the same transaction, so its locks are held until it commits. Stop the coordinators before running it; the time grows with history. `b8f1c3a7d952` adds a column with a constant default (metadata-only) and two indexes. Building `ix_jobs_waiting_children` scans all of `jobs` and blocks its writes until the migration commits, so on a large history either stop the coordinators or build it first without blocking, then run the migration, which skips an index that already exists:

```sql
CREATE INDEX CONCURRENTLY ix_jobs_waiting_children ON jobs (depends_on)
  WHERE depends_on IS NOT NULL AND status IN ('QUEUED','RETRY_WAIT');
```

`c4e9d2a1f7b3` adds two small partial indexes (live workers; RETRY_WAIT events) and can be pre-built the same way (definitions in the migration). Every downgrade refuses to lose data it cannot represent: `7d2e` refuses event ids beyond integer range, and `b8f1` refuses while a SUSPECT or DEAD worker has a pending drain, which only DRAINING could represent without the column.

## Backup and restore (Compose)
The dump is written and read inside the container and copied with `docker compose cp`, so no shell redirection touches the binary file. (PowerShell has no `<` redirection, and Windows PowerShell 5.1 re-encodes `>` output as UTF-16, which corrupts a `-Fc` dump.) These commands work unchanged in PowerShell, cmd and POSIX shells; in Git Bash, set `MSYS_NO_PATHCONV=1` first so `/tmp/...` is not rewritten to a Windows path. Drain workers before backing up.

```
docker compose exec -T postgres pg_dump -U greyqueue -Fc -f /tmp/greyqueue.dump greyqueue
docker compose cp postgres:/tmp/greyqueue.dump ./greyqueue.dump
```

Restore into an empty volume. The simplest is a separate project, which gets its own volume and leaves the current one untouched (stop the old stack first, since both publish port 8810). `docker compose down -v` also gives an empty volume, but only run it after checking the dump, because it deletes the current data.

```
docker compose -p greyqueue-restored up -d --wait postgres
docker compose -p greyqueue-restored cp ./greyqueue.dump postgres:/tmp/greyqueue.dump
docker compose -p greyqueue-restored exec -T postgres pg_restore -U greyqueue -d greyqueue --no-owner --no-privileges --single-transaction --exit-on-error /tmp/greyqueue.dump
docker compose -p greyqueue-restored up -d
```

`--single-transaction --exit-on-error` makes a failed restore leave the database empty rather than half-restored with the migration then running on top of it. The final `up` finds the schema current, and `db-roles` re-creates the `greyqueue_app` role and its grants, which a dump does not contain. This procedure was run end to end on 2026-09-29 (12 jobs, schema head `c4e9d2a1f7b3`, and `check_roles.sh` passing on the restored project).

`POSTGRES_PASSWORD` is only applied when the volume is first created, and it is embedded in database URLs, so keep it to `A-Z a-z 0-9 _ -` (`configure.py` generates such values). Changing it in `.env` for an existing volume makes the migration fail to authenticate; change the role's password inside the database first, without putting it on a command line: `docker compose exec postgres psql -U greyqueue -c '\password greyqueue'` prompts for it. `APP_DB_PASSWORD` can be changed freely: the next `up` re-runs `db-roles`, which sets it.
