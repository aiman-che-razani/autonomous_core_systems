"""Operational scripts that are safe to run in a temporary directory."""

import os
from pathlib import Path

from scripts import configure

KEYS = ["APP_DB_PASSWORD", "CLIENT_TOKEN", "WORKER_TOKEN"]


def keys(path):
    return [line.split("=", 1)[0] for line in path.read_text(encoding="utf-8").splitlines()]


def test_configure_appends_only_missing_secrets(tmp_path, monkeypatch):
    monkeypatch.setattr(configure, "ROOT", tmp_path)
    env = tmp_path / ".env"
    env.write_text("POSTGRES_PASSWORD=kept", encoding="utf-8")  # no trailing newline
    configure.main()
    assert env.read_text(encoding="utf-8").splitlines()[0] == "POSTGRES_PASSWORD=kept"
    assert keys(env) == ["POSTGRES_PASSWORD", *KEYS]
    before = env.read_bytes()
    configure.main()
    assert env.read_bytes() == before  # idempotent
    if os.name != "nt":
        assert env.stat().st_mode & 0o777 == 0o600


def test_configure_fills_a_copied_example(tmp_path, monkeypatch):
    # A copied .env.example has empty secrets and CHANGE_ME placeholders; configure.py
    # used to call those keys present and leave them, so Compose could never start.
    monkeypatch.setattr(configure, "ROOT", tmp_path)
    env = tmp_path / ".env"
    example = (Path(__file__).resolve().parents[1] / ".env.example").read_text(encoding="utf-8")
    env.write_text(example, encoding="utf-8")
    configure.main()
    values = dict(
        line.split("=", 1)
        for line in env.read_text(encoding="utf-8").splitlines()
        if "=" in line and not line.startswith("#")
    )
    for key in ["POSTGRES_PASSWORD", *KEYS]:
        assert len(values[key]) >= 32, key  # generated in place, not left empty
    assert not any("CHANGE_ME" in v for v in values.values())
    assert values["POSTGRES_PASSWORD"] in values["DATABASE_URL"]
    assert keys(env).count("CLIENT_TOKEN") == 1  # replaced, not appended twice
    assert values["COORDINATOR_URL"] == "http://127.0.0.1:8810"  # other lines untouched
    before = env.read_bytes()
    configure.main()
    assert env.read_bytes() == before


def test_configure_creates_a_private_env(tmp_path, monkeypatch):
    monkeypatch.setattr(configure, "ROOT", tmp_path)
    configure.main()
    env = tmp_path / ".env"
    assert set(KEYS) <= set(keys(env)) and "POSTGRES_PASSWORD" in keys(env)
    if os.name != "nt":
        assert env.stat().st_mode & 0o777 == 0o600
