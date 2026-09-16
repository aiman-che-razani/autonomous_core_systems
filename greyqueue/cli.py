import argparse
import json
from uuid import uuid4

import httpx

from greyqueue.config import settings
from greyqueue.tasks import REGISTRY


def main():
    parser = argparse.ArgumentParser(description="GreyQueue client")
    sub = parser.add_subparsers(dest="command", required=True)
    submit = sub.add_parser("submit")
    submit.add_argument("task", choices=REGISTRY)
    submit.add_argument("--args", required=True, help="JSON object")
    submit.add_argument("--priority", type=int, default=0)
    submit.add_argument("--timeout", type=float, default=15)
    submit.add_argument("--max-retries", type=int, default=3)
    submit.add_argument("--idempotency-key", default=None)
    submit.add_argument("--scheduled-at", default=None, help="ISO timestamp with timezone")
    submit.add_argument("--depends-on", default=None, help="Parent job UUID")
    for name in ("get", "cancel", "attempts", "drain"):
        sub.add_parser(name).add_argument("id")
    jobs = sub.add_parser("jobs")
    jobs.add_argument("--state", default=None)
    sub.add_parser("workers")
    sub.add_parser("operations")
    args = parser.parse_args()
    config = settings()
    with httpx.Client(
        base_url=config.coordinator_url,
        timeout=15,
        headers={"Authorization": f"Bearer {config.client_token}"},
    ) as client:
        if args.command == "submit":
            payload = {
                "task": args.task,
                "args": json.loads(args.args),
                "priority": args.priority,
                "timeout": args.timeout,
                "max_retries": args.max_retries,
                "idempotency_key": args.idempotency_key or str(uuid4()),
                "scheduled_at": args.scheduled_at,
                "depends_on": args.depends_on,
            }
            response = client.post("/jobs", json=payload)
        elif args.command == "cancel":
            response = client.delete(f"/jobs/{args.id}")
        elif args.command == "drain":
            response = client.post(f"/workers/{args.id}/drain")
        elif args.command == "attempts":
            response = client.get(f"/jobs/{args.id}/attempts")
        elif args.command == "jobs":
            response = client.get("/jobs", params={"state": args.state} if args.state else {})
        else:
            response = client.get(
                f"/jobs/{args.id}" if args.command == "get" else f"/{args.command}"
            )
        if response.is_error:
            parser.exit(1, f"HTTP {response.status_code}: {response.text}\n")
        print(json.dumps(response.json(), indent=2))


if __name__ == "__main__":
    main()
