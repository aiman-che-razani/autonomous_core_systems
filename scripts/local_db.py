"""Manage only this repository's isolated development PostgreSQL cluster."""

import argparse
import os
import secrets
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / ".runtime"
DATA = RUNTIME / "postgres"
BIN = Path(os.environ.get("POSTGRES_BIN", r"C:\Program Files\PostgreSQL\18\bin"))


def run(*args):
    subprocess.run(
        [str(BIN / args[0]), *args[1:]],
        check=True,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["start", "stop"])
    args = parser.parse_args()
    if args.action == "stop":
        run("pg_ctl", "-D", str(DATA), "stop", "-m", "fast")
        return
    RUNTIME.mkdir(exist_ok=True)
    if not DATA.exists():
        if (ROOT / ".env").exists():
            raise SystemExit(
                "Existing .env found; move it aside before initializing a new local cluster"
            )
        password = secrets.token_urlsafe(32)
        password_file = RUNTIME / "init-password"
        password_file.write_text(password, encoding="utf-8")
        try:
            run(
                "initdb",
                "-D",
                str(DATA),
                "-U",
                "greyqueue",
                "--pwfile",
                str(password_file),
                "--auth=scram-sha-256",
                "--encoding=UTF8",
                "--locale=C",
            )
        finally:
            password_file.unlink(missing_ok=True)
        (ROOT / ".env").write_text(
            f"DATABASE_URL=postgresql+psycopg://greyqueue:{password}@127.0.0.1:55441/postgres\n"
            f"POSTGRES_PASSWORD={password}\nCLIENT_TOKEN={secrets.token_urlsafe(32)}\n"
            f"WORKER_TOKEN={secrets.token_urlsafe(32)}\n"
            "COORDINATOR_URL=http://127.0.0.1:8810\n",
            encoding="utf-8",
        )
    run(
        "pg_ctl",
        "-D",
        str(DATA),
        "-l",
        str(RUNTIME / "postgres.log"),
        "-o",
        "-h 127.0.0.1 -p 55441",
        "start",
    )


if __name__ == "__main__":
    main()
