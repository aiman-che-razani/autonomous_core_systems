# Transactions and correctness

| Operation | Locks and atomic work |
| --- | --- |
| Submit | Admission advisory lock; compare idempotency digest; count active jobs (indexed) and the rolling-minute rate; check the dependency parent exists and has not failed; insert job and two events |
| Claim | Worker row (the session is checked under this lock); slot check (a slot beyond capacity is 422); replay an existing claim ID or occupied slot (before the health check); if HEALTHY, walk the queue-head index, filtering availability/capability/dependency, and lock the first eligible job with SKIP LOCKED; create attempt; increment fence; set LEASED |
| Start/renew/finish | Worker row (session check), then job row; verify token, worker, fence and expiry; renew also requires a started attempt; mutate attempt/job/result/events together |
| Recovery | Advisory transaction mutex; lock expired jobs with SKIP LOCKED; recheck expiry; record retry/dead letter; cancel waiting children of failed, dead-lettered or cancelled parents |
| Drain/heartbeat/register | Worker row; refresh cached ORM state after lock acquisition; drain intent is a separate flag |

READ COMMITTED is intentional: each statement sees committed state, and ownership decisions serialize on row locks. A fresh database `clock_timestamp()` is read after acquiring the lock; transaction-start `now()` could accept an expired lease after a long lock wait. Submitted jobs take their `created_at` from the same clock, so the admission rate window compares like with like. See [PostgreSQL time functions](https://www.postgresql.org/docs/18/functions-datetime.html).

Recovery's worker detection is a separate transaction from job recovery, and runs on every coordinator. Claims skip locked job rows instead of waiting while holding a worker lock. Finished results and state transitions roll back together. The success response occurs after commit; a lost response is handled with claim IDs, idempotency keys and identical completion replay.

Unique partial indexes provide a second defense against duplicate active jobs/slots. API reads refresh locked workers so a previously loaded HEALTHY object cannot override a concurrent drain. A regression test covers this race. Connection, pool, statement (10 s), lock (5 s) and idle-in-transaction (15 s) timeouts bound resource waits, so a stalled handler cannot hold the admission lock indefinitely; transient database errors return 503, and values PostgreSQL cannot store return 422.

[PostgreSQL locking reference](https://www.postgresql.org/docs/18/explicit-locking.html).
