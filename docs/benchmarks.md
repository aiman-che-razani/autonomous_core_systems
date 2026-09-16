# Benchmarks

`python -m benchmarks.run` starts a disposable PostgreSQL schema, one coordinator and three workers. `--matrix` compares thread, process, hybrid and subprocess execution for CPU, I/O and mixed workloads. Job count, strategy, workload and per-worker concurrency are configurable; supported job counts span 1–10,000.

CPU work computes pi with 300,000 iterations; I/O work sleeps 50 ms; mixed alternates the two. The light workload hashes a short fixed string. Twelve concurrent HTTP submitters generate load. Each result records throughput, mean/P50/P95/P99 end-to-end latency, queue wait, execution duration, queue depth, CPU, RSS memory, utilization, failures and retries. Resource samples cover coordinator/worker process trees, excluding PostgreSQL and the load generator. Sum CPU may exceed 100% because 100% means one logical core. Short-lived child CPU/RSS can be missed by sampling; do not interpret it as full system accounting.

Environment details and timestamps accompany JSON results in `results/benchmark-*.json`; each report has a PNG chart. Runs include cold execution pools but exclude cluster startup. These are single local observations, not statistically controlled production capacity measurements. Do not compare unlike workload/concurrency/count settings as if they were equal.

The read-path optimization batches result lookup for a page. `python -m scripts.profile_reads` asserts identical responses and measures the reduction from 101 SQL statements to 2 for 100 completed jobs. Queue/status/creation and active-attempt indexes support the transactional paths. There is no invented speedup claim: see `results/read-optimization.json` for the actual query-count and local timing evidence.

## Recorded local results

- 100-job matrix: all 12 strategy/workload cases completed without execution failures or retries.
- 1,000 mixed jobs, hybrid, 3 workers x 2 slots: 44.29 jobs/s; P95 end-to-end latency 7.714 seconds.
- 10,000 light jobs, hybrid, 3 workers x 2 slots: 44.99 jobs/s; P95 38.165 seconds; peak queue depth 2,041; zero execution/submission retries and failures.
- Hardware: Windows 11, 8 physical / 16 logical CPUs, 31.31 GiB RAM; full CPU identifier and timestamps are in each JSON artifact.
- 100-result page: 101 SQL statements reduced to 2 with identical output. The recorded local timings were about 36.4 ms and 4.8 ms; query count is the stronger reproducible result.

These figures describe different workloads and queue depths. The larger run's higher latency reflects waiting behind queued work; it does not imply a slower individual hash task.
