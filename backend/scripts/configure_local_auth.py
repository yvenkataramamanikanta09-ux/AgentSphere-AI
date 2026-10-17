"""Append explicit loopback HTTP development settings without replacing existing values."""

import secrets
from pathlib import Path

from dotenv import dotenv_values


def main():
    path = Path(__file__).resolve().parents[1] / ".env"
    if not path.is_file():
        raise SystemExit("Configure the local database and .env first.")
    keys = {"ENVIRONMENT", "APP_ORIGIN", "ALLOW_INSECURE_LOCAL_HTTP", "AUTH_RATE_LIMIT_KEY"}
    if keys.intersection(dotenv_values(path)):
        raise SystemExit(
            "Authentication settings already exist. Nothing changed; review .env manually."
        )
    # Append only; never print the generated secret or read out database credentials.
    with path.open("a", encoding="utf-8") as file:
        file.write(
            "\n# Explicit local-only HTTP development; use HTTPS outside loopback.\n"
            "ENVIRONMENT=development\nAPP_ORIGIN=http://127.0.0.1:8000\n"
            "ALLOW_INSECURE_LOCAL_HTTP=true\n"
            f"AUTH_RATE_LIMIT_KEY={secrets.token_urlsafe(48)}\n"
        )
    print("Appended local authentication settings to ignored .env. No credentials printed.")


if __name__ == "__main__":
    main()
