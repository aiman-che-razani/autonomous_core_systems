"""Own a disposable schema and process tree for reproducible demonstrations."""

import os
import secrets
import socket
import subprocess
import sys
import time
import uuid
from contextlib import AbstractContextManager
from pathlib import Path

import httpx
import psutil
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from greyqueue.config import settings

ROOT = Path(__file__).resolve().parents[1]


def wait_for(predicate, timeout=45, label="condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(0.1)
    raise TimeoutError(label)


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Cluster(AbstractContextManager):
    def __init__(self, **overrides):
        self.schema = "experiment_" + uuid.uuid4().hex
        self.runtime = ROOT / ".runtime" / self.schema
        self.runtime.mkdir(parents=True)
        self.processes, self.logs = [], []
        self.admin = create_engine(settings().database_url, pool_pre_ping=True)
        with self.admin.begin() as db:
            db.execute(text(f'CREATE SCHEMA "{self.schema}"'))
        url = make_url(settings().database_url).update_query_dict(
            {"options": f"-csearch_path={self.schema}"}
        )
        self.url = url.render_as_string(hide_password=False)
        self.port = free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        self.env = {
            **os.environ,
            "DATABASE_URL": self.url,
            "CLIENT_TOKEN": secrets.token_urlsafe(32),
            "WORKER_TOKEN": secrets.token_urlsafe(32),
            "COORDINATOR_URL": self.base,
            "HEARTBEAT_INTERVAL": "0.4",
            "SUSPECT_AFTER": "1.2",
            "DEAD_AFTER": "2.4",
            "LEASE_SECONDS": "2",
            "MAINTENANCE_INTERVAL": "0.2",
            "CAPACITY": "2",
            "EXECUTOR": "hybrid",
            "POLL_INTERVAL": "0.05",
            "QUEUE_LIMIT": "20000",
            "SUBMISSIONS_PER_MINUTE": "100000",
            **{k: str(v) for k, v in overrides.items()},
        }
        self.client = httpx.Client(
            base_url=self.base,
            timeout=10,
            headers={"Authorization": f"Bearer {self.env['CLIENT_TOKEN']}"},
        )
        self.engine = create_engine(self.url, pool_pre_ping=True)
        migration = self.launch("migration", ["-m", "alembic", "upgrade", "head"])
        if migration.wait(timeout=30):
            raise RuntimeError(f"Migration failed; inspect {self.runtime}")
        self.coordinator = self.start_coordinator()

    def launch(self, name, command, env=None):
        log = (self.runtime / f"{name}-{len(self.logs)}.log").open("w", encoding="utf-8")
        self.logs.append(log)
        process = subprocess.Popen(
            [sys.executable, *command],
            cwd=ROOT,
            env={**self.env, **(env or {})},
            stdout=log,
            stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        self.processes.append(process)
        return process

    def start_coordinator(self, port=None):
        port = port or self.port
        process = self.launch(
            "coordinator",
            [
                "-m",
                "uvicorn",
                "greyqueue.api:create_app",
                "--factory",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--no-access-log",
            ],
        )

        def ready():
            if process.poll() is not None:
                raise RuntimeError(f"Coordinator exited; inspect {self.runtime}")
            try:
                return httpx.get(f"http://127.0.0.1:{port}/health", timeout=1).status_code == 200
            except httpx.TransportError:
                return False

        wait_for(ready, label="coordinator startup")
        return process

    def worker(self, name, **env):
        process = self.launch(
            name,
            ["-m", "greyqueue.worker"],
            {"WORKER_ID": name, **{k: str(v) for k, v in env.items()}},
        )
        wait_for(
            lambda: any(w["id"] == name for w in self.get("/workers")), label="worker registration"
        )
        return process

    def get(self, path):
        response = self.client.get(path)
        response.raise_for_status()
        return response.json()

    def submit(self, task="sleep", args=None, **kwargs):
        response = self.client.post(
            "/jobs", json={"task": task, "args": args or {"seconds": 0.1}, **kwargs}
        )
        response.raise_for_status()
        return response.json()["id"]

    def complete(self, ids, timeout=60):
        def finished():
            jobs = [self.get(f"/jobs/{job_id}") for job_id in ids]
            if any(j["status"] in {"FAILED", "DEAD_LETTER", "CANCELLED"} for j in jobs):
                raise AssertionError(jobs)
            return jobs if all(j["status"] == "SUCCEEDED" for j in jobs) else False

        return wait_for(finished, timeout=timeout, label="job completion")

    def kill(self, process):
        if process.poll() is None:
            try:
                children = psutil.Process(process.pid).children(recursive=True)
            except psutil.NoSuchProcess:
                children = []
            process.kill()
            process.wait(timeout=10)
            for child in children:
                try:
                    child.kill()
                except psutil.NoSuchProcess:
                    pass
            psutil.wait_procs(children, timeout=5)

    def __exit__(self, *exc):
        for process in reversed(self.processes):
            self.kill(process)
        for log in self.logs:
            log.close()
        self.client.close()
        self.engine.dispose()
        with self.admin.begin() as db:
            db.execute(text(f'DROP SCHEMA "{self.schema}" CASCADE'))
        self.admin.dispose()
