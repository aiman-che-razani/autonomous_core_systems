import argparse
import json

import httpx

from greyqueue.config import settings


def main():
    parser = argparse.ArgumentParser(description="GreyQueue local client")
    sub = parser.add_subparsers(dest="command", required=True)
    submit = sub.add_parser("submit")
    submit.add_argument("task", choices=["sleep", "calculate_pi", "hash_text"])
    submit.add_argument("--args", required=True, help="JSON object")
    for name in ("get", "cancel"):
        sub.add_parser(name).add_argument("id")
    sub.add_parser("jobs")
    sub.add_parser("workers")
    args = parser.parse_args()
    config = settings()
    with httpx.Client(
        base_url=config.coordinator_url,
        timeout=15,
        headers={"Authorization": f"Bearer {config.client_token}"},
    ) as client:
        if args.command == "submit":
            response = client.post("/jobs", json={"task": args.task, "args": json.loads(args.args)})
        elif args.command == "cancel":
            response = client.delete(f"/jobs/{args.id}")
        else:
            response = client.get(
                f"/jobs/{args.id}" if args.command == "get" else f"/{args.command}"
            )
        response.raise_for_status()
        print(json.dumps(response.json(), indent=2))


if __name__ == "__main__":
    main()
