# Security and deployment

The client bearer token controls job/operations access. A distinct worker bootstrap token authorizes enrollment. Each worker creates a random session credential, sends it on registration, and authenticates later requests with X-Worker-Session; only its hash is stored. Reusing an existing ID with a different credential is rejected. Treat bootstrap credentials as privileged and admit only trusted workers.

Requests are capped at 128 KiB with a ten-second body-read deadline. Task names/arguments, output size, metadata, retries, priorities, capacity and timeouts are bounded. SQLAlchemy parameters avoid SQL interpolation of client data. Runtime tasks cannot select executable code, arbitrary URLs or file paths. The external-sink demo is a developer-run simulation, not an API capability.

Admission rate/queue limits are global across coordinators. Authenticated read endpoints and enrollment also need an edge proxy rate policy for an internet-facing deployment; no public multi-tenant service is claimed. Metrics and results require client auth. Dashboard tokens stay in page memory. Same-origin script policy, no-store and nosniff headers apply; Swagger docs use a separate policy for its bundled CDN assets.

Compose binds the API to loopback and keeps PostgreSQL private. Workers receive only their bootstrap credential, not database/client credentials. Coordinator/worker containers run as a non-root user with read-only roots, tmpfs scratch, dropped capabilities, no-new-privileges, PID/memory/CPU limits. Default subprocess execution is killable; process isolation is not a hostile-code sandbox.

For remote deployment terminate TLS at a trusted proxy or use Uvicorn certificate/key options, set REQUIRE_TLS=true and configure trusted proxy addresses deliberately. Never trust arbitrary forwarded scheme headers. Rotate secrets through the deployment secret store, restrict database roles/network access, back up PostgreSQL and validate restore procedures. Repository `.env` and `.runtime` are ignored; CI uses explicit nonproduction credentials.
