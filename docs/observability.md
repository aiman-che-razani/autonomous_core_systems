# Observability

`GET /operations` returns queue states, admission saturation, 60-second success throughput, execution duration average/P50/P95/P99, mean queue wait, bounded recent workers/events. `/metrics` emits Prometheus text under client authentication. Counters derive from durable events, so coordinator restart does not reset them. Removing history would reset derived totals and must be accounted for by operators.

Execution duration is finish minus start for completed attempts; queue wait is attempt creation minus original job creation and includes earlier retry history for later attempts. These are not identical to end-to-end latency, which the benchmark records separately. Recent worker lists are bounded at 200; global state counts are queried independently. At large history sizes, quantiles require aggregation work; export/scrape at a reasonable interval rather than every request.

Worker logs include job_id, worker_id, attempt_id and fence. Request logs include a generated request_id, method, path, status and duration; credentials and payloads are not logged. System events preserve worker suspicion/death, registration/drain and lease expiry. Job events preserve state transitions.

Dashboard: `/dashboard`. It uses same-origin fetch, textContent for untrusted values, no local/session storage of tokens, no external chart scripts, and a strict script policy. Desktop/mobile browser tests cover connection, submission, completion, inspection and layout.

Example Prometheus configuration and an importable Grafana dashboard are under `monitoring/`. Supply the client token through a local secret file; never commit it.
