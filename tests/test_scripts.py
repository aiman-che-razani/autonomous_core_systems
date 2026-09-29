"""Operational scripts that are safe to run in a temporary directory."""

import os

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


def test_configure_creates_a_private_env(tmp_path, monkeypatch):
    monkeypatch.setattr(configure, "ROOT", tmp_path)
    configure.main()
    env = tmp_path / ".env"
    assert set(KEYS) <= set(keys(env)) and "POSTGRES_PASSWORD" in keys(env)
    if os.name != "nt":
        assert env.stat().st_mode & 0o777 == 0o600
