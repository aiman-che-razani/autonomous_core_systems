# Failure model

| Failure | Expected behavior | Evidence |
| --- | --- | --- |
| Worker dies before start | Assignment expires and retries | Automated test |
| Worker killed during execution | SUSPECT/DEAD; expiry; another attempt succeeds | Process experiment |
| Worker slow past deadline | Timeout; retry budget; dead letter | Executor tests + process experiment |
| Worker returns after expiry | Old token is fenced | Automated tests |
| Renewal races recovery | Job lock + expiry recheck prevent invalid ownership | Database tests |
| Coordinator killed/restarted | Workers retry transport (by design); after restart the job completes on a later attempt | Process experiment (asserts completion, not the retries themselves) |
| Claim response lost, worker then SUSPECT/DRAINING | Replaying the claim ID returns the same attempt | Database test |
| Task output the coordinator cannot store | Recorded as a permanent failure; the worker keeps running | Executor unit test + worker-loop test against a mock coordinator (413 and 422) |
| Worker session revoked (heartbeat 401) | Worker stops its slots and exits with the error | Worker-loop test against a mock coordinator |
| Task process prints invalid output, or the executor raises | Recorded as a failed attempt; the worker keeps running | Worker-loop and executor unit tests |
| Drained worker goes DEAD, then returns | It is told to drain again, not given new work (ADR 009) | Database test |
| Old process claims while its ID is taken over | Its session is checked under the worker row lock and refused | Concurrent database test |
| PostgreSQL stops temporarily | 503; worker/recovery loops retry; processing resumes | Native outage experiment |
| Two coordinators claim | Shared locks/indexes keep one active owner | Process experiment |
| Submit response lost | Same idempotency key returns same job | Concurrent deduplication test |
| Effect committed, acknowledgement lost | Execution may repeat; sink must deduplicate | Two real child processes + independent sink |
| Queue saturation | Admission 429; slots remain bounded; queue drains | Process experiment |
| Malformed payload | 422 or 413; no arbitrary code | Validation/API tests |

Lease expiry can permit overlapping physical execution after a partition; fencing protects GreyQueue updates, not arbitrary external effects. A database outage sacrifices availability to preserve authoritative ownership. Once storage returns, progress resumes subject to remaining retries. This is the relevant consistency/availability tradeoff, not a claim that CAP means choosing two arbitrary features.

The failure experiments kill only their owned process trees and use disposable schemas. Native database outage testing verifies the helper-owned loopback cluster before stopping it. Each experiment asserts its expected outcome in `scripts/experiments.py`; the passing results are recorded in `results/experiments.json`.
