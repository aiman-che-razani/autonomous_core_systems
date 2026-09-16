"""Controlled external-sink simulation: crash after commit, before job acknowledgement.

SQLite is a separate demonstration sink, never GreyQueue's queue or state store.
"""

import argparse
import json
import os
import secrets
import sqlite3
from uuid import uuid4

import httpx

from greyqueue.config import settings
from scripts.harness import ROOT, Cluster, wait_for


def execute_once(crash: bool, deduplicate: bool):
    config = settings()
    worker_id = "sink-" + uuid4().hex
    credential = secrets.token_urlsafe(32)
    with httpx.Client(
        base_url=config.coordinator_url,
        headers={"Authorization": f"Bearer {config.worker_token}", "X-Worker-Session": credential},
        timeout=10,
    ) as client:

        def post(path, payload):
            response = client.post(path, json=payload)
            response.raise_for_status()
            return response.json()

        post(
            "/internal/workers/register",
            {"worker_id": worker_id, "session_token": credential, "capabilities": ["hash_text"]},
        )
        item = post("/internal/claim", {"worker_id": worker_id, "claim_id": str(uuid4())})
        assert item is not None
        job = item["job"]
        ownership = {"worker_id": worker_id, "token": item["token"]}
        post(f"/internal/jobs/{job['id']}/start", ownership)
        with sqlite3.connect(os.environ["DEMO_LEDGER"]) as sink:
            sink.execute(
                "CREATE TABLE IF NOT EXISTS effects (id INTEGER PRIMARY KEY, operation TEXT)"
            )
            sink.execute("CREATE TABLE IF NOT EXISTS receipts (operation TEXT PRIMARY KEY)")
            apply = True
            if deduplicate:
                apply = (
                    sink.execute("INSERT OR IGNORE INTO receipts VALUES (?)", (job["id"],)).rowcount
                    == 1
                )
            if apply:
                sink.execute("INSERT INTO effects(operation) VALUES (?)", (job["id"],))
        if crash:
            os._exit(17)  # real process exits after the independent sink committed
        post(
            f"/internal/jobs/{job['id']}/finish",
            {**ownership, "output": {"side_effect_recorded": True}},
        )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--crash", action="store_true")
    parser.add_argument("--deduplicate", action="store_true")
    args = parser.parse_args()
    if args.once:
        execute_once(args.crash, args.deduplicate)
        return
    results = []
    for deduplicate in (False, True):
        with Cluster() as cluster:
            ledger = cluster.runtime / "external-sink.sqlite"
            job = cluster.submit(
                "hash_text", {"text": "controlled side-effect simulation"}, retry_delay=0
            )
            command = ["-m", "scripts.side_effects", "--once"] + (
                ["--deduplicate"] if deduplicate else []
            )
            first = cluster.launch(
                "crash-before-ack", [*command, "--crash"], {"DEMO_LEDGER": str(ledger)}
            )
            assert first.wait(timeout=20) == 17
            wait_for(lambda job=job: cluster.get(f"/jobs/{job}")["status"] == "RETRY_WAIT")
            second = cluster.launch("retry", command, {"DEMO_LEDGER": str(ledger)})
            assert second.wait(timeout=20) == 0
            cluster.complete([job])
            with sqlite3.connect(ledger) as sink:
                effects = sink.execute("SELECT count(*) FROM effects").fetchone()[0]
            attempts = cluster.get(f"/jobs/{job}/attempts")
            assert effects == (1 if deduplicate else 2) and len(attempts) == 2
            results.append(
                {
                    "idempotent_sink": deduplicate,
                    "executions": 2,
                    "side_effects": effects,
                    "passed": True,
                }
            )
            print(
                f"PASS: executions=2, effects={effects}, idempotent sink={deduplicate}", flush=True
            )
    output = ROOT / "docs/results/side-effects.json"
    output.parent.mkdir(exist_ok=True)
    output.write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
