"""Generate local Compose credentials without overwriting existing configuration."""

import secrets
from pathlib import Path

root = Path(__file__).resolve().parents[1]
path = root / ".env"
if path.exists():
    print("Existing .env preserved")
else:
    password = secrets.token_urlsafe(32)
    path.write_text(
        f"POSTGRES_PASSWORD={password}\n"
        f"DATABASE_URL=postgresql+psycopg://greyqueue:{password}@127.0.0.1:55441/greyqueue\n"
        f"CLIENT_TOKEN={secrets.token_urlsafe(32)}\nWORKER_TOKEN={secrets.token_urlsafe(32)}\n"
        "COORDINATOR_URL=http://127.0.0.1:8810\n",
        encoding="utf-8",
    )
    print("Created .env with independent random credentials")
