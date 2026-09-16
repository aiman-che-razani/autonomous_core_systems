from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    database_url: str
    client_token: str = Field(min_length=16)
    worker_token: str = Field(min_length=16)
    coordinator_url: str = "http://127.0.0.1:8810"
    worker_id: str = ""
    poll_interval: float = Field(default=0.2, ge=0.05, le=30)


@lru_cache
def settings() -> Settings:
    return Settings()
