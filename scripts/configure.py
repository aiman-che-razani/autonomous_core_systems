"""Generate local Compose credentials without overwriting real configuration.

A new .env gets independent random secrets. In an existing .env, a secret that is absent,
empty or still the example's CHANGE_ME placeholder gets a fresh random value, in place;
every other line is kept byte for byte, so this is safe to re-run.
"""

import os
import secrets
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLACEHOLDERS = {"", "CHANGE_ME"}
# Independent secrets can be generated at any time. POSTGRES_PASSWORD and DATABASE_URL must
# match an existing cluster, so they are only filled while still placeholders.
INDEPENDENT = ("APP_DB_PASSWORD", "CLIENT_TOKEN", "WORKER_TOKEN")


def private(path: Path) -> None:
    """Owner-only permissions: .env holds every secret. (Windows ignores all but read-only.)"""
    if os.name != "nt":
        path.chmod(0o600)


def write_private(path: Path, content: str) -> None:
    # Created 0600 and swapped in, so the secrets never sit at umask permissions.
    staging = path.with_name(path.name + ".tmp")
    staging.unlink(missing_ok=True)
    descriptor = os.open(staging, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as file:
        file.write(content)
    os.replace(staging, path)
    private(path)


def value(line: str) -> tuple[str, str] | None:
    if "=" not in line or line.lstrip().startswith("#"):
        return None
    key, _, raw = line.partition("=")
    return key.strip(), raw.strip().strip("\"'")


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
        write_private(path, "".join(f"{k}={v}\n" for k, v in required.items()))
        print("Created .env with independent random credentials")
        return
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    current = dict(filter(None, (value(line) for line in lines)))  # the last one wins
    fill = [k for k in INDEPENDENT if current.get(k, "") in PLACEHOLDERS]
    if current.get("POSTGRES_PASSWORD") in PLACEHOLDERS:  # present but never set
        fill.append("POSTGRES_PASSWORD")
        if "CHANGE_ME" in current.get("DATABASE_URL", "CHANGE_ME"):
            fill.append("DATABASE_URL")
    if not fill:
        private(path)
        print("Existing .env preserved")
        return
    replaced = set()
    for index, line in enumerate(lines):
        parsed = value(line)
        if parsed and parsed[0] in fill and parsed[0] not in replaced:
            ending = line[len(line.rstrip("\r\n")) :]
            lines[index] = f"{parsed[0]}={required[parsed[0]]}{ending}"
            replaced.add(parsed[0])
    appended = [k for k in fill if k not in replaced]
    prefix = "" if not lines or lines[-1].endswith("\n") else "\n"
    content = "".join(lines) + prefix + "".join(f"{k}={required[k]}\n" for k in appended)
    write_private(path, content)
    print("Existing .env kept; generated " + ", ".join(fill))
    if "POSTGRES_PASSWORD" in fill:
        print(
            "POSTGRES_PASSWORD was a placeholder. If a Compose volume or .runtime cluster was "
            "already created with it, change the role's password to match (docs/persistence.md)."
        )


if __name__ == "__main__":
    main()
