"""Measure the removed per-row result-query pattern against batch loading."""

import json
import time

from sqlalchemy import event, select
from sqlalchemy.orm import sessionmaker

from greyqueue import service
from greyqueue.models import Job, Result, Worker
from scripts.harness import ROOT, Cluster


def main():
    with Cluster() as cluster:
        sessions = sessionmaker(cluster.engine, expire_on_commit=False)
        with sessions.begin() as db:
            db.add(Worker(id="profile"))
            for _ in range(100):
                service.submit(db, "hash_text", {"text": "profile"})
                job, attempt = service.claim(db, "profile")
                service.start(db, job.id, "profile", attempt.id)
                service.finish(db, job.id, "profile", attempt.id, {"value": 1}, None)
        report = {}
        outputs = []
        for batch in (False, True):
            queries = []

            def count(*args, queries=queries):
                queries.append(1)

            event.listen(cluster.engine, "before_cursor_execute", count)
            start = time.perf_counter()
            with sessions() as db:
                jobs = list(db.scalars(select(Job).limit(100)))
                results = (
                    {
                        r.job_id: r
                        for r in db.scalars(
                            select(Result).where(Result.job_id.in_([j.id for j in jobs]))
                        )
                    }
                    if batch
                    else None
                )
                outputs.append([service.serialize(db, job, results) for job in jobs])
            elapsed = time.perf_counter() - start
            event.remove(cluster.engine, "before_cursor_execute", count)
            report["batch" if batch else "per_row"] = {
                "sql_statements": len(queries),
                "seconds": elapsed,
            }
        assert outputs[0] == outputs[1]
        assert report["batch"]["sql_statements"] == 2 and report["per_row"]["sql_statements"] == 101
        report["note"] = (
            "Same 100 results; local single-run query-count comparison, not an end-to-end throughput claim"
        )
        output = ROOT / "docs/results/read-optimization.json"
        output.parent.mkdir(exist_ok=True)
        output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print("PASS: identical 100-job response; 101 SQL statements reduced to 2", flush=True)


if __name__ == "__main__":
    main()
