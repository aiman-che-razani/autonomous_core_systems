# Worker lifecycle

Registration creates HEALTHY with capacity, capabilities, credential hash and timestamps. A heartbeat updates last_seen. Configurable thresholds move HEALTHY to SUSPECT, then DEAD. A returning authenticated worker can become HEALTHY again, but cannot resurrect expired attempt tokens. DRAINING stops new claims and keeps renewing/finishing current jobs. The normal worker exits when all local slots drain.

Each live process must have a unique worker ID. A different credential cannot take over an existing ID. Registration replay with the same session token is safe after an ambiguous response. Bootstrap credentials are shared by trusted workers; session credentials isolate ownership but are not a multi-tenant identity provider.

Heartbeats do not prove failure: pauses, overload and partitions can look like death. Work is recovered by lease expiry, not merely a suspicion event. A blocked executor cannot renew indefinitely: renewals are capped at task timeout plus five seconds from start. Linux SIGTERM/SIGINT requests a drain; abrupt kill is handled by lease recovery. Windows forced termination is also a crash, not graceful draining.
