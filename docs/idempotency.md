# Idempotency

Clients can set `idempotency_key` (up to 128 characters). The coordinator stores a canonical digest of the validated task definition and options. The admission transaction serializes key lookup/insertion: equal key and definition return the existing job with 200 (a new job is 201); equal key with a different definition returns 409. Timezone-aware scheduled timestamps are normalized to UTC; numeric timeout/retry delay is normalized for stable hashes.

Use a stable business-operation key when retrying a submission after an ambiguous network response. Generating a new key creates a new job. Keys currently live as long as jobs; there is no automatic expiry.

Worker claim UUIDs make a lost claim response replayable, including after the worker has since become SUSPECT, DRAINING or DEAD (ADR 007), unless a new process has re-registered that ID meanwhile; the old session then gets 401. Identical finish payloads acknowledge an already recorded attempt without executing again or overwriting a later attempt. Stale lease completions are rejected.

The independent side-effect demo uses SQLite only as a mock external sink. GreyQueue itself still uses PostgreSQL. A child commits the effect and exits with code 17 before acknowledgement; recovery produces another attempt. Without sink deduplication there are two rows. With a unique receipt written in the same sink transaction there is one. See [measured evidence](results/side-effects.json).
