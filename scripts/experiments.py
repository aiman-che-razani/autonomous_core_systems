"""Real-process demonstrations; emits evidence only after assertions pass."""

import argparse
import json
import subprocess
import sys
import time

from sqlalchemy import text

from scripts.harness import ROOT, Cluster, free_port, wait_for


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--database-outage",
        action="store_true",
        help="Stop/start ONLY scripts/local_db.py's project cluster",
    )
    args = parser.parse_args()
    evidence = []
    with Cluster() as cluster:
        workers = [cluster.worker(f"demo-worker-{i}") for i in range(3)]
        started = time.monotonic()
        ids = [cluster.submit(priority=i % 4) for i in range(100)]
        cluster.complete(ids)
        with cluster.engine.connect() as db:
            distribution = dict(
                db.execute(text("SELECT worker_id,count(*) FROM attempts GROUP BY worker_id")).all()
            )
        assert len(distribution) == 3
        evidence.append(
            {
                "demo": "distributed_execution",
                "jobs": 100,
                "distribution": distribution,
                "seconds": round(time.monotonic() - started, 3),
                "passed": True,
            }
        )
        print("PASS: 100 jobs across three workers", flush=True)

        # Stop existing workers, then guarantee the victim owns the long job.
        for w in workers:
            cluster.kill(w)
        victim = cluster.worker("victim", CAPACITY=1)
        job = cluster.submit(args={"seconds": 4.0}, retry_delay=0.1)
        wait_for(lambda: cluster.get(f"/jobs/{job}")["status"] == "RUNNING")
        cluster.kill(victim)
        cluster.worker("survivor")
        cluster.complete([job])
        attempts = cluster.get(f"/jobs/{job}/attempts")
        assert attempts[0]["outcome"] == "LEASE_EXPIRED" and len(attempts) == 2
        wait_for(
            lambda: (
                next(w for w in cluster.get("/workers") if w["id"] == "victim")["state"] == "DEAD"
            )
        )
        evidence.append(
            {
                "demo": "worker_killed_during_execution",
                "attempts": [a["outcome"] for a in attempts],
                "passed": True,
            }
        )
        print("PASS: killed worker detected; lease recovered", flush=True)

        job = cluster.submit(args={"seconds": 4.0}, retry_delay=0.1)
        wait_for(lambda: cluster.get(f"/jobs/{job}")["status"] == "RUNNING")
        cluster.kill(cluster.coordinator)
        time.sleep(2.6)
        cluster.coordinator = cluster.start_coordinator()
        cluster.complete([job])
        attempts = cluster.get(f"/jobs/{job}/attempts")
        assert len(attempts) >= 2 and attempts[-1]["outcome"] == "SUCCEEDED"
        evidence.append(
            {
                "demo": "coordinator_killed_and_restarted",
                "attempts": [a["outcome"] for a in attempts],
                "passed": True,
            }
        )
        print("PASS: coordinator restart reconstructs and recovers work", flush=True)

        # Both coordinators accept requests and run recovery against one database.
        port = free_port()
        second = cluster.start_coordinator(port)
        w2 = cluster.worker("second-coordinator-worker", COORDINATOR_URL=f"http://127.0.0.1:{port}")
        ids = [cluster.submit() for _ in range(30)]
        cluster.complete(ids)
        with cluster.engine.connect() as db:
            duplicate = db.scalar(
                text(
                    "SELECT count(*) FROM (SELECT job_id FROM attempts WHERE outcome='SUCCEEDED' GROUP BY job_id HAVING count(*)>1) x"
                )
            )
        assert duplicate == 0
        evidence.append(
            {
                "demo": "two_active_coordinators",
                "jobs": 30,
                "duplicate_successes": duplicate,
                "passed": True,
            }
        )
        print("PASS: two coordinators share exclusive claims", flush=True)
        cluster.kill(w2)
        cluster.kill(second)

        retry = cluster.submit("flaky", {"failures": 2}, retry_delay=0.05, max_retries=3)
        cluster.complete([retry])
        assert len(cluster.get(f"/jobs/{retry}/attempts")) == 3
        timeout = cluster.submit(args={"seconds": 1.0}, timeout=0.05, max_retries=1, retry_delay=0)
        wait_for(lambda: cluster.get(f"/jobs/{timeout}")["status"] == "DEAD_LETTER")
        evidence.append(
            {
                "demo": "retry_and_timeout",
                "retry_attempts": 3,
                "timeout_terminal": "DEAD_LETTER",
                "passed": True,
            }
        )
        print("PASS: transient failures retry; timeouts reach dead letters", flush=True)

        if args.database_outage:
            # Verify the configured server is precisely the helper-owned cluster.
            from sqlalchemy.engine import make_url

            from greyqueue.config import settings

            url = make_url(settings().database_url)
            assert url.host == "127.0.0.1" and url.port == 55441
            assert (ROOT / ".runtime/postgres/PG_VERSION").exists()
            job = cluster.submit(args={"seconds": 4.0}, retry_delay=0.1)
            wait_for(lambda: cluster.get(f"/jobs/{job}")["status"] == "RUNNING")
            subprocess.run([sys.executable, "scripts/local_db.py", "stop"], cwd=ROOT, check=True)
            try:
                assert cluster.client.get("/health").status_code == 503
                time.sleep(2.5)
            finally:
                subprocess.run(
                    [sys.executable, "scripts/local_db.py", "start"], cwd=ROOT, check=True
                )
            wait_for(lambda: cluster.client.get("/health").status_code == 200)
            cluster.complete([job])
            evidence.append({"demo": "postgresql_outage", "passed": True})
            print("PASS: temporary PostgreSQL outage returns 503 and recovers", flush=True)

    with Cluster(QUEUE_LIMIT=8) as cluster:
        ids = [cluster.submit(args={"seconds": 0.4}) for _ in range(8)]
        response = cluster.client.post("/jobs", json={"task": "sleep", "args": {"seconds": 0.1}})
        assert response.status_code == 429
        cluster.worker("bounded", CAPACITY=2)
        ops = cluster.get("/operations")
        assert ops["queue_depth"] > 0
        assert all(w["running"] <= w["capacity"] for w in ops["workers"])
        cluster.complete(ids)
        assert cluster.get("/operations")["queue_depth"] == 0
        evidence.append(
            {
                "demo": "backpressure",
                "admitted": 8,
                "overflow_status": 429,
                "capacity": 2,
                "passed": True,
            }
        )
        print("PASS: admission and worker backpressure drain successfully", flush=True)
    output = ROOT / "docs/results/experiments.json"
    output.parent.mkdir(exist_ok=True)
    output.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
    print(f"Saved {len(evidence)} passing experiments", flush=True)


if __name__ == "__main__":
    main()
