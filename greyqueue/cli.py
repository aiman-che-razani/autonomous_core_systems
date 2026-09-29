"""GreyQueue client CLI.

Submissions without --idempotency-key get a random key, so re-running a submit after an
ambiguous timeout creates a second job; pass a stable key when retrying.
"""

import argparse
import json
from typing import Any
from uuid import uuid4

import httpx

from greyqueue.config import settings
from greyqueue.tasks import REGISTRY


def json_object(value: str) -> dict[str, Any]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise argparse.ArgumentTypeError(f"invalid JSON: {exc.msg}") from exc
    if not isinstance(parsed, dict):
        raise argparse.ArgumentTypeError("expected a JSON object")
    return parsed


def main():
    parser = argparse.ArgumentParser(description="GreyQueue client")
    sub = parser.add_subparsers(dest="command", required=True)
    submit = sub.add_parser("submit")
    submit.add_argument("task", choices=REGISTRY)
    submit.add_argument("--args", required=True, type=json_object, help="JSON object")
    submit.add_argument("--priority", type=int, default=0)
    submit.add_argument("--timeout", type=float, default=15)
    submit.add_argument("--max-retries", type=int, default=3)
    submit.add_argument("--retry-delay", type=float, default=1)
    submit.add_argument("--no-jitter", action="store_true", help="Disable retry jitter")
    submit.add_argument("--metadata", type=json_object, default={}, help="JSON object")
    submit.add_argument("--idempotency-key", default=None, help="Stable key for safe retries")
    submit.add_argument("--scheduled-at", default=None, help="ISO timestamp with timezone")
    submit.add_argument("--depends-on", default=None, help="Parent job UUID")
    for name in ("get", "cancel", "attempts", "drain"):
        sub.add_parser(name).add_argument("id")
    jobs = sub.add_parser("jobs")
    jobs.add_argument("--state", default=None)
    jobs.add_argument("--limit", type=int, default=50, help="1-200")
    jobs.add_argument("--offset", type=int, default=0)
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
                "args": args.args,
                "priority": args.priority,
                "timeout": args.timeout,
                "max_retries": args.max_retries,
                "retry_delay": args.retry_delay,
                "retry_jitter": not args.no_jitter,
                "metadata": args.metadata,
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
            params = {"limit": args.limit, "offset": args.offset}
            if args.state:
                params["state"] = args.state
            response = client.get("/jobs", params=params)
        else:
            response = client.get(
                f"/jobs/{args.id}" if args.command == "get" else f"/{args.command}"
            )
        if response.is_error:
            parser.exit(1, f"HTTP {response.status_code}: {response.text}\n")
        print(json.dumps(response.json(), indent=2))


if __name__ == "__main__":
    main()
