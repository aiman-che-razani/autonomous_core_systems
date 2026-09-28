"""Generate local Compose credentials without overwriting existing configuration.

A new .env gets independent random secrets. An existing .env is never rewritten; any
required key it lacks (for example APP_DB_PASSWORD, added with the least-privilege
database role) is appended with a fresh random value.
"""

import secrets
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    path = ROOT / ".env"
    # token_urlsafe output is safe inside a database URL without escaping.
    password = secrets.token_urlsafe(32)
    required = {
        "POSTGRES_PASSWORD": password,
        "APP_DB_PASSWORD": secrets.token_urlsafe(32),
        "DATABASE_URL": f"postgresql+psycopg://greyqueue:{password}@127.0.0.1:55441/greyqueue",
        "CLIENT_TOKEN": secrets.token_urlsafe(32),
        "WORKER_TOKEN": secrets.token_urlsafe(32),
        "COORDINATOR_URL": "http://127.0.0.1:8810",
    }
    if not path.exists():
        path.write_text("".join(f"{k}={v}\n" for k, v in required.items()), encoding="utf-8")
        print("Created .env with independent random credentials")
        return
    text = path.read_text(encoding="utf-8")
    present = {line.split("=", 1)[0].strip() for line in text.splitlines() if "=" in line}
    # POSTGRES_PASSWORD/DATABASE_URL must stay paired with an existing cluster; only add
    # keys that are independent secrets.
    missing = [k for k in ("APP_DB_PASSWORD", "CLIENT_TOKEN", "WORKER_TOKEN") if k not in present]
    if not missing:
        print("Existing .env preserved")
        return
    prefix = "" if text.endswith("\n") or not text else "\n"
    with path.open("a", encoding="utf-8") as env:
        env.write(prefix + "".join(f"{k}={required[k]}\n" for k in missing))
    print("Existing .env preserved; added " + ", ".join(missing))


if __name__ == "__main__":
    main()
