# Persistence

PostgreSQL is the authority for definitions, states, attempts, leases, results, identities and events. Workers are replaceable; coordinator restarts require no in-memory state reconstruction beyond reading the database. SQLAlchemy sessions are short-lived. Alembic owns schema changes; the application never creates production tables implicitly.

The v1 migration preserves v0.1 jobs/results, initializes historical attempt counts and adds nullable session hashes. Stop/drain v0.1 workers before upgrading; v1 workers register fresh identities. Unfinished v0.1 assignments expire under the new recovery policy. Back up the database before an upgrade. Downgrade refuses incompatible new states or concurrent active slots rather than silently discarding their meaning.

WAL records committed changes so PostgreSQL can recover after a crash. Durability depends on PostgreSQL's storage/fsync configuration; GreyQueue does not replace database backups or replication. Compose uses a named data volume. `docker compose down` preserves it; removing that volume destroys the stored history.

Results and events have no automatic retention deletion. Long-running installations must choose archival/retention and monitor database growth; this release does not silently erase history.
