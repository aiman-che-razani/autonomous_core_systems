# Transactions and correctness

| Operation | Locks and atomic work |
| --- | --- |
| Submit | Admission advisory lock; compare idempotency digest; enforce capacity/rate; insert job and two events |
| Claim | Worker row then eligible job row; slot check; create attempt; increment fence; set LEASED |
| Start/renew/finish | Job row; verify token, worker, fence and expiry; mutate attempt/job/result/events together |
| Recovery | Advisory transaction mutex; lock expired jobs with SKIP LOCKED; recheck expiry; record retry/dead letter |
| Drain/heartbeat | Worker row; refresh cached ORM state after lock acquisition |

READ COMMITTED is intentional: each statement sees committed state, and ownership decisions serialize on row locks. A fresh database `clock_timestamp()` is read after acquiring the lock; transaction-start `now()` could accept an expired lease after a long lock wait. See [PostgreSQL time functions](https://www.postgresql.org/docs/18/functions-datetime.html).

Recovery's worker detection is a separate transaction from job recovery. Claims skip locked job rows instead of waiting while holding a worker lock. Finished results and state transitions roll back together. The success response occurs after commit; a lost response is handled with claim IDs, idempotency keys and identical completion replay.

Unique partial indexes provide a second defense against duplicate active jobs/slots. API reads refresh locked workers so a previously loaded HEALTHY object cannot override a concurrent drain. A regression test covers this race. Database connection, pool, statement and lock timeouts bound resource waits; transient database errors return 503.

[PostgreSQL locking reference](https://www.postgresql.org/docs/18/explicit-locking.html).
