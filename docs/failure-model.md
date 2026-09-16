# Failure model

| Failure | Expected behavior | Evidence |
| --- | --- | --- |
| Worker dies before start | Assignment expires and retries | Automated test |
| Worker killed during execution | SUSPECT/DEAD; expiry; another attempt succeeds | Process experiment |
| Worker slow past deadline | Timeout; retry budget; dead letter | Executor tests + process experiment |
| Worker returns after expiry | Old token is fenced | Automated tests |
| Renewal races recovery | Job lock + expiry recheck prevent invalid ownership | Database tests |
| Coordinator killed/restarted | Workers retry transport; database recovery continues | Process experiment |
| PostgreSQL stops temporarily | 503; worker/recovery loops retry; processing resumes | Native outage experiment |
| Two coordinators claim | Shared locks/indexes keep one active owner | Process experiment |
| Submit response lost | Same idempotency key returns same job | Concurrent deduplication test |
| Effect committed, acknowledgement lost | Execution may repeat; sink must deduplicate | Two real child processes + independent sink |
| Queue saturation | Admission 429; slots remain bounded; queue drains | Process experiment |
| Malformed payload | 422 or 413; no arbitrary code | Validation/API tests |

Lease expiry can permit overlapping physical execution after a partition; fencing protects GreyQueue updates, not arbitrary external effects. A database outage sacrifices availability to preserve authoritative ownership. Once storage returns, progress resumes subject to remaining retries. This is the relevant consistency/availability tradeoff, not a claim that CAP means choosing two arbitrary features.

The failure experiments kill only their owned process trees and use disposable schemas. Native database outage testing verifies the helper-owned loopback cluster before stopping it. Expected and actual outcomes are recorded in `results/experiments.json`.
