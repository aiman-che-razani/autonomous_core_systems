"""Reproducible network benchmarks, with explicit environment and measured samples."""

import argparse
import asyncio
import json
import os
import platform
import statistics
import time
from datetime import UTC, datetime
from uuid import uuid4

import httpx
import psutil
from sqlalchemy import text

from scripts.harness import ROOT, Cluster


def percentile(values, fraction):
    values = sorted(values)
    if not values:
        return 0.0
    index = (len(values) - 1) * fraction
    lo = int(index)
    hi = min(lo + 1, len(values) - 1)
    return values[lo] + (values[hi] - values[lo]) * (index - lo)


def workload(kind, index):
    if kind == "io" or (kind == "mixed" and index % 2):
        return {"task": "sleep", "args": {"seconds": 0.05}}
    if kind == "light":
        return {"task": "hash_text", "args": {"text": "greyqueue"}}
    return {"task": "calculate_pi", "args": {"iterations": 300000}}


async def measure(cluster, count, kind):
    samples = []
    submission_retries = 0
    finished = asyncio.Event()
    processes = {}

    def sample():
        all_processes = []
        for root in cluster.processes:
            if root.poll() is None:
                try:
                    proc = psutil.Process(root.pid)
                    all_processes.extend([proc, *proc.children(recursive=True)])
                except psutil.NoSuchProcess:
                    pass
        cpu, rss = 0.0, 0
        for proc in {p.pid: p for p in all_processes}.values():
            try:
                tracked = processes.setdefault(proc.pid, proc)
                cpu += tracked.cpu_percent()
                rss += tracked.memory_info().rss
            except psutil.NoSuchProcess:
                pass
        with cluster.engine.connect() as db:
            queue = db.scalar(
                text("SELECT count(*) FROM jobs WHERE status IN ('QUEUED','RETRY_WAIT')")
            )
            active = db.scalar(text("SELECT count(*) FROM attempts WHERE finished_at IS NULL"))
            terminal = db.scalar(
                text(
                    "SELECT count(*) FROM jobs WHERE status IN ('SUCCEEDED','FAILED','DEAD_LETTER','CANCELLED')"
                )
            )
        return {
            "cpu_percent": cpu,
            "rss_bytes": rss,
            "queue_depth": queue,
            "active_slots": active,
            "terminal": terminal,
        }

    async def monitor():
        while not finished.is_set():
            samples.append(await asyncio.to_thread(sample))
            await asyncio.sleep(0.25)

    task = asyncio.create_task(monitor())
    start = time.monotonic()
    semaphore = asyncio.Semaphore(12)
    async with httpx.AsyncClient(
        base_url=cluster.base,
        timeout=30,
        headers={"Authorization": f"Bearer {cluster.env['CLIENT_TOKEN']}"},
    ) as client:

        async def submit(index):
            nonlocal submission_retries
            payload = {
                **workload(kind, index),
                "max_retries": 1,
                "retry_delay": 0.1,
                "idempotency_key": str(uuid4()),
            }
            async with semaphore:
                for attempt in range(6):
                    try:
                        response = await client.post("/jobs", json=payload)
                        if response.status_code not in {429, 503}:
                            response.raise_for_status()
                            return
                    except httpx.TransportError:
                        pass
                    submission_retries += 1
                    await asyncio.sleep(min(0.2 * 2**attempt, 3))
                raise RuntimeError("Submission unavailable after six attempts")

        try:
            await asyncio.gather(*(submit(i) for i in range(count)))
            deadline = time.monotonic() + 1200
            while not samples or samples[-1]["terminal"] < count:
                if time.monotonic() > deadline:
                    raise TimeoutError("Benchmark did not finish")
                await asyncio.sleep(0.25)
        finally:
            finished.set()
            await task
    elapsed = time.monotonic() - start
    with cluster.engine.connect() as db:
        jobs = db.execute(
            text("SELECT status,extract(epoch FROM updated_at-created_at) FROM jobs")
        ).all()
        attempts = db.execute(
            text(
                "SELECT extract(epoch FROM a.created_at-j.created_at), extract(epoch FROM a.finished_at-a.started_at) FROM attempts a JOIN jobs j ON j.id=a.job_id WHERE a.finished_at IS NOT NULL"
            )
        ).all()
    latencies = [float(row[1]) for row in jobs]
    waits = [float(row[0]) for row in attempts]
    durations = [float(row[1]) for row in attempts if row[1] is not None]
    successes = sum(row[0] == "SUCCEEDED" for row in jobs)
    assert successes == count, (successes, count)
    return {
        "jobs": count,
        "elapsed_seconds": elapsed,
        "jobs_per_second": count / elapsed,
        "latency_seconds": {
            "average": statistics.mean(latencies),
            "p50": percentile(latencies, 0.5),
            "p95": percentile(latencies, 0.95),
            "p99": percentile(latencies, 0.99),
        },
        "average_queue_wait_seconds": statistics.mean(waits),
        "average_execution_seconds": statistics.mean(durations),
        "peak_queue_depth": max(s["queue_depth"] for s in samples),
        "average_cpu_percent": statistics.mean(s["cpu_percent"] for s in samples),
        "peak_rss_mb": max(s["rss_bytes"] for s in samples) / 1024**2,
        "average_worker_utilization": statistics.mean(s["active_slots"] for s in samples)
        / (int(cluster.env["CAPACITY"]) * 3),
        "failures": count - successes,
        "retries": len(attempts) - count,
        "samples": len(samples),
        "submission_retries": submission_retries,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--jobs", type=int, default=100)
    parser.add_argument(
        "--strategy", choices=["thread", "process", "hybrid", "subprocess"], default="hybrid"
    )
    parser.add_argument("--workload", choices=["cpu", "io", "mixed", "light"], default="mixed")
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--matrix", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.jobs <= 10000 or not 1 <= args.concurrency <= 32:
        parser.error("jobs 1..10000 and concurrency 1..32 required")
    cases = (
        [
            (s, w)
            for s in ["thread", "process", "hybrid", "subprocess"]
            for w in ["cpu", "io", "mixed"]
        ]
        if args.matrix
        else [(args.strategy, args.workload)]
    )
    results = []
    for strategy, kind in cases:
        with Cluster(
            EXECUTOR=strategy,
            CAPACITY=args.concurrency,
            LEASE_SECONDS=15,
            SUSPECT_AFTER=10,
            DEAD_AFTER=20,
        ) as cluster:
            for index in range(3):
                cluster.worker(f"benchmark-{index}")
            result = asyncio.run(measure(cluster, args.jobs, kind))
            result.update(
                {
                    "strategy": strategy,
                    "workload": kind,
                    "workers": 3,
                    "concurrency": args.concurrency,
                }
            )
            results.append(result)
            print(
                f"PASS {strategy}/{kind}: {args.jobs} jobs, {result['jobs_per_second']:.2f} jobs/s, P95 {result['latency_seconds']['p95']:.3f}s",
                flush=True,
            )
    environment = {
        "timestamp": datetime.now(UTC).isoformat(),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "cpu": platform.processor(),
        "logical_cpus": os.cpu_count(),
        "physical_cpus": psutil.cpu_count(logical=False),
        "ram_gb": round(psutil.virtual_memory().total / 1024**3, 2),
        "database": "PostgreSQL 18 on loopback",
        "transport": "HTTP loopback",
        "submission_concurrency": 12,
        "resource_scope": "Coordinator and worker process trees; excludes PostgreSQL, load generator and Docker",
        "cpu_unit": "Sum of per-process CPU percentages; 100% is one logical core",
        "notes": "Single local runs including cold task pools; exclude cluster startup; no cross-host or production claim",
    }
    output = ROOT / "docs/results"
    output.mkdir(exist_ok=True)
    name = f"benchmark-{args.jobs}-" + (
        "matrix" if args.matrix else args.strategy + "-" + args.workload
    )
    (output / (name + ".json")).write_text(
        json.dumps({"environment": environment, "results": results}, indent=2) + "\n",
        encoding="utf-8",
    )
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, axes = plt.subplots(1, 2, figsize=(max(10, len(results)), 4.5), layout="constrained")
    labels = [r["strategy"] + "\n" + r["workload"] for r in results]
    axes[0].bar(labels, [r["jobs_per_second"] for r in results], color="#287955")
    axes[0].set_ylabel("Jobs / second")
    axes[1].bar(labels, [r["latency_seconds"]["p95"] for r in results], color="#486c95")
    axes[1].set_ylabel("P95 end-to-end latency (seconds)")
    for ax in axes:
        ax.tick_params(axis="x", labelsize=8, rotation=35)
    fig.suptitle(
        f"GreyQueue · {args.jobs} jobs / case · 3 workers × {args.concurrency} slots · local single runs"
    )
    fig.savefig(output / (name + ".png"), dpi=160)
    plt.close(fig)


if __name__ == "__main__":
    main()
