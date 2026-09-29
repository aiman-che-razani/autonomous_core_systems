import json
from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from greyqueue.tasks import REGISTRY

WORKER_ID = r"^[a-zA-Z0-9_-]+$"
METADATA_LIMIT = 8000  # JSON characters
OUTPUT_LIMIT = 64000  # JSON characters of a task result
TEXT_LIMIT = 131072  # other client text: bounded by the request cap anyway
STATUSES = (
    "QUEUED",
    "LEASED",
    "RUNNING",
    "SUCCEEDED",
    "FAILED",
    "CANCELLED",
    "RETRY_WAIT",
    "DEAD_LETTER",
)


def storable(value: Any, limit: int, label: str) -> str:
    """Canonical JSON within limit; PostgreSQL JSONB/text reject NUL characters."""
    encoded = json.dumps(value, allow_nan=False)
    if len(encoded) > limit:
        raise ValueError(f"{label} too large")
    if "\\u0000" in encoded:
        raise ValueError(f"{label} must not contain NUL characters")
    return encoded


class Submit(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    task: str = Field(max_length=64)
    args: dict[str, Any]
    priority: int = Field(default=0, ge=-100, le=100)
    timeout: float = Field(default=15, ge=0.05, le=300)
    max_retries: int = Field(default=3, ge=0, le=10)
    retry_delay: float = Field(default=1, ge=0, le=300)
    retry_jitter: bool = True
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=128)
    metadata: dict[str, Any] = Field(default_factory=dict)
    scheduled_at: AwareDatetime | None = None
    depends_on: UUID | None = None

    @model_validator(mode="after")
    def bounded_metadata(self) -> "Submit":
        storable(self.metadata, METADATA_LIMIT, "Metadata")
        # Sizes of args/keys are bounded by the task models and Field limits; only NULs here.
        storable(self.args, TEXT_LIMIT, "Arguments")
        storable(self.idempotency_key, TEXT_LIMIT, "Idempotency key")
        return self


class Identity(BaseModel):
    model_config = ConfigDict(extra="forbid")
    worker_id: str = Field(min_length=1, max_length=100, pattern=WORKER_ID)


class Registration(Identity):
    capacity: int = Field(default=1, ge=1, le=32)
    capabilities: list[str] = Field(default_factory=lambda: list(REGISTRY), max_length=32)
    session_token: str = Field(min_length=32, max_length=128)

    @model_validator(mode="after")
    def known_capabilities(self) -> "Registration":
        if not self.capabilities or not set(self.capabilities) <= REGISTRY.keys():
            raise ValueError("Unknown or empty task capabilities")
        return self


class Claim(Identity):
    slot: int = Field(default=0, ge=0, le=31)
    claim_id: UUID


class Assignment(Identity):
    token: UUID


class JobOut(BaseModel):
    """Response shape of service.serialize; clients (dashboard, CLI, harness) read these keys."""

    id: str
    task: str
    args: dict[str, Any]
    status: str
    priority: int
    timeout: float
    max_retries: int
    attempt_count: int
    metadata: dict[str, Any]
    available_at: str
    depends_on: str | None
    created_at: str
    updated_at: str
    result: dict[str, Any] | None
    error: str | None


class AttemptOut(BaseModel):
    id: str
    worker_id: str
    fence: int
    outcome: str | None
    error: str | None
    output: dict[str, Any] | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    expires_at: datetime


class WorkerOut(BaseModel):
    id: str
    state: str
    capacity: int
    running: int
    capabilities: list[str]
    last_seen: str
    heartbeat_age_seconds: float


class AssignmentOut(BaseModel):
    job: JobOut
    token: str
    fence: int
    expires_at: datetime


class RegisteredOut(BaseModel):
    worker_id: str
    lease_seconds: float
    heartbeat_interval: float


class RenewedOut(BaseModel):
    expires_at: datetime


class StateOut(BaseModel):
    state: str


class StatusOut(BaseModel):
    status: str


class ErrorOut(BaseModel):
    detail: str


class Completion(Assignment):
    output: dict[str, Any] | None = None
    error: str | None = Field(default=None, min_length=1, max_length=4000)
    retryable: bool = False

    @model_validator(mode="after")
    def result_shape(self) -> "Completion":
        if (self.output is None) == (self.error is None):
            raise ValueError("Provide exactly one of output or error")
        storable(self.output, OUTPUT_LIMIT, "Output")
        storable(self.error, TEXT_LIMIT, "Error")
        return self
