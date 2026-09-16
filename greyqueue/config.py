from functools import lru_cache
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class WorkerSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    worker_token: str = Field(min_length=16)
    coordinator_url: str = "http://127.0.0.1:8810"
    worker_id: str = ""
    poll_interval: float = Field(default=0.2, ge=0.05, le=30)

    capacity: int = Field(default=2, ge=1, le=32)
    capabilities: list[str] = ["sleep", "calculate_pi", "hash_text", "flaky"]
    executor: Literal["subprocess", "thread", "process", "hybrid"] = "subprocess"


class Settings(WorkerSettings):
    database_url: str
    client_token: str = Field(min_length=16)
    scheduler: Literal["fifo", "priority", "capacity"] = "priority"
    heartbeat_interval: float = Field(default=2, ge=0.2, le=60)
    suspect_after: float = Field(default=6, ge=0.5)
    dead_after: float = Field(default=12, ge=1)
    lease_seconds: float = Field(default=10, ge=1, le=300)
    maintenance_interval: float = Field(default=0.5, ge=0.1, le=30)
    queue_limit: int = Field(default=10000, ge=1, le=1000000)
    submissions_per_minute: int = Field(default=20000, ge=1)
    require_tls: bool = False

    @model_validator(mode="after")
    def timings(self):
        if self.client_token == self.worker_token:
            raise ValueError("Client and worker credentials must differ")
        if not self.heartbeat_interval < self.suspect_after < self.dead_after:
            raise ValueError("Require heartbeat_interval < suspect_after < dead_after")
        return self


@lru_cache
def settings() -> Settings:
    return Settings()
