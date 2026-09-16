"""All executable work is named here; inputs never select arbitrary code or IO."""

import hashlib
import json
import sys
import time
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class Arguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class SleepArgs(Arguments):
    seconds: float = Field(ge=0, le=5)


class PiArgs(Arguments):
    iterations: int = Field(ge=1, le=1_000_000)


class HashArgs(Arguments):
    text: str = Field(max_length=10000)


class FlakyArgs(Arguments):
    failures: int = Field(ge=0, le=10)


class RetryableTaskError(Exception):
    pass


REGISTRY = {"flaky": FlakyArgs, "sleep": SleepArgs, "calculate_pi": PiArgs, "hash_text": HashArgs}


def validate(task: str, args: dict[str, Any]) -> dict[str, Any]:
    if task not in REGISTRY:
        raise ValueError(f"Unknown task: {task}")
    return REGISTRY[task].model_validate(args).model_dump()


def execute(task: str, args: dict[str, Any], attempt: int = 1) -> dict[str, Any]:
    args = validate(task, args)
    match task:
        case "flaky":
            if attempt <= args["failures"]:
                raise RetryableTaskError("Demonstration transient failure")
            return {"attempt": attempt, "recovered": True}
        case "sleep":
            time.sleep(args["seconds"])
            return {"slept_seconds": args["seconds"]}
        case "calculate_pi":
            n = args["iterations"]
            return {
                "pi": 4 * sum((-1.0 if k % 2 else 1.0) / (2 * k + 1) for k in range(n)),
                "iterations": n,
            }
        case "hash_text":
            return {"sha256": hashlib.sha256(args["text"].encode()).hexdigest()}
    raise ValueError("Unsupported task")


if __name__ == "__main__":
    request = json.load(sys.stdin)
    try:
        result = {
            "output": execute(request["task"], request["args"], request.get("attempt_count", 1))
        }
    except RetryableTaskError as exc:
        result = {"error": str(exc), "retryable": True}
    except ValueError as exc:
        result = {"error": str(exc), "retryable": False}
    json.dump(result, sys.stdout, allow_nan=False)
