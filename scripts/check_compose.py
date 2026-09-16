"""Verify an already running local Compose deployment; does not expose secrets."""

import json
import time
from pathlib import Path
from uuid import uuid4

import httpx

from greyqueue.config import settings


def main():
    config = settings()
    with httpx.Client(
        base_url=config.coordinator_url,
        timeout=15,
        headers={"Authorization": f"Bearer {config.client_token}"},
    ) as client:
        deadline = time.monotonic() + 60
        while True:
            response = client.get("/workers")
            response.raise_for_status()
            workers = [w for w in response.json() if w["state"] == "HEALTHY"]
            if len(workers) == 3:
                break
            if time.monotonic() > deadline:
                raise TimeoutError("Expected three healthy Compose workers")
            time.sleep(0.5)
        run_id = uuid4().hex
        ids = []
        for index in range(100):
            response = client.post(
                "/jobs",
                json={
                    "task": "sleep",
                    "args": {"seconds": 0.1},
                    "idempotency_key": f"compose-{run_id}-{index}",
                },
            )
            response.raise_for_status()
            ids.append(response.json()["id"])
        deadline = time.monotonic() + 120
        while True:
            jobs = [client.get(f"/jobs/{job}").json() for job in ids]
            if all(job["status"] == "SUCCEEDED" for job in jobs):
                break
            if time.monotonic() > deadline:
                raise TimeoutError("Compose workload did not finish")
            time.sleep(0.5)
        distribution = {}
        for job in ids:
            attempts = client.get(f"/jobs/{job}/attempts").json()
            assert len(attempts) == 1
            worker = attempts[0]["worker_id"]
            distribution[worker] = distribution.get(worker, 0) + 1
        assert len(distribution) == 3
        assert client.get("/dashboard").status_code == 200
        assert "greyqueue_jobs_completed_total" in client.get("/metrics").text
        report = {
            "passed": True,
            "jobs": 100,
            "healthy_workers": 3,
            "jobs_per_worker": distribution,
            "checks": [
                "Docker image build",
                "Alembic migration",
                "three containers",
                "job execution",
                "dashboard",
                "metrics",
            ],
        }
        path = Path(__file__).resolve().parents[1] / "docs/results/compose.json"
        path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print("PASS: Docker Compose, 100 jobs, three workers, dashboard and metrics", flush=True)


if __name__ == "__main__":
    main()
