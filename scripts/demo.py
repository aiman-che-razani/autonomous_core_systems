"""Run a real coordinator and three workers; preserve a measured smoke report."""

import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import httpx
from sqlalchemy import func, select

from greyqueue.config import settings
from greyqueue.db import make_sessions
from greyqueue.models import Attempt

ROOT = Path(__file__).resolve().parents[1]


def main():
    config = settings()
    runtime = ROOT / ".runtime"
    runtime.mkdir(exist_ok=True)
    processes, logs = [], []
    run_id = uuid.uuid4().hex[:8]

    def launch(name, command, env=None):
        log = (runtime / f"{name}.log").open("w", encoding="utf-8")
        logs.append(log)
        process = subprocess.Popen(
            [sys.executable, *command],
            cwd=ROOT,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        processes.append(process)
        return process

    try:
        coordinator = launch(
            "coordinator",
            [
                "-m",
                "uvicorn",
                "greyqueue.api:create_app",
                "--factory",
                "--host",
                "127.0.0.1",
                "--port",
                "8810",
            ],
        )
        with httpx.Client(
            base_url=config.coordinator_url,
            timeout=5,
            headers={"Authorization": f"Bearer {config.client_token}"},
        ) as client:
            deadline = time.monotonic() + 30
            while True:
                if coordinator.poll() is not None:
                    raise RuntimeError("Coordinator exited; inspect .runtime/coordinator.log")
                try:
                    if client.get("/health").status_code == 200:
                        break
                except httpx.TransportError:
                    pass
                if time.monotonic() > deadline:
                    raise TimeoutError("Coordinator startup")
                time.sleep(0.2)
            workers = [f"demo-{run_id}-{i}" for i in range(3)]
            for worker in workers:
                launch(worker, ["-m", "greyqueue.worker"], {**os.environ, "WORKER_ID": worker})
            deadline = time.monotonic() + 30
            while not set(workers).issubset({w["id"] for w in client.get("/workers").json()}):
                if time.monotonic() > deadline:
                    raise TimeoutError("Worker registration")
                time.sleep(0.2)
            started = time.monotonic()
            ids = []
            for index in range(30):
                payload = (
                    {"task": "sleep", "args": {"seconds": 0.15}}
                    if index % 2 == 0
                    else {"task": "calculate_pi", "args": {"iterations": 10000}}
                )
                response = client.post("/jobs", json=payload)
                response.raise_for_status()
                ids.append(response.json()["id"])
            deadline = time.monotonic() + 90
            while True:
                jobs = [client.get(f"/jobs/{job_id}").json() for job_id in ids]
                if all(job["status"] == "SUCCEEDED" for job in jobs):
                    break
                if any(job["status"] == "FAILED" for job in jobs):
                    raise AssertionError("Task failed")
                if time.monotonic() > deadline:
                    raise TimeoutError("Jobs did not complete")
                time.sleep(0.2)
            engine, sessions = make_sessions()
            with sessions() as db:
                distribution = dict(
                    db.execute(
                        select(Attempt.worker_id, func.count())
                        .where(Attempt.worker_id.in_(workers))
                        .group_by(Attempt.worker_id)
                    ).all()
                )
            engine.dispose()
            assert len(distribution) == 3 and sum(distribution.values()) == 30, distribution
            assert all(job["result"] is not None for job in jobs)
            report = {
                "version": "0.1.0",
                "jobs": 30,
                "succeeded": 30,
                "coordinator_processes": 1,
                "worker_processes": 3,
                "elapsed_seconds": round(time.monotonic() - started, 3),
                "jobs_per_worker": distribution,
                "database": "PostgreSQL",
                "note": "Local smoke test, not a throughput benchmark",
            }
            (ROOT / "docs" / "smoke-result.json").write_text(json.dumps(report, indent=2) + "\n")
            print(json.dumps(report, indent=2))
    finally:
        for process in reversed(processes):
            if process.poll() is None:
                process.terminate()
        for process in processes:
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        for log in logs:
            log.close()


if __name__ == "__main__":
    main()
