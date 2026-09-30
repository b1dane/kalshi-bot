"""Environment-based configuration loader.

Secrets are resolved in this order (first non-empty wins):
  1. Real environment variables
  2. ./.env next to this file (kalshi-bot's own .env)
"""
import os

_HERE = os.path.dirname(os.path.abspath(__file__))
_LOCAL_ENV = os.path.join(_HERE, ".env")


def _load_dotenv(path: str) -> dict[str, str]:
    """Load KEY=value pairs from a .env file (supports `export KEY=value`)."""
    env: dict[str, str] = {}
    if not os.path.isfile(path):
        return env
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.startswith("export "):
                line = line[len("export "):]
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip("\"'")
    return env


class Config:
    """Read-only configuration populated from environment variables."""

    # ── Kalshi (public, no auth) ────────────────────────────────────────────
    KALSHI_BASE_URL: str = "https://api.elections.kalshi.com/trade-api/v2"
    KALSHI_SERIES: str = "KXBTC15M"

    # ── Jev (typesafe.ai) ──────────────────────────────────────────────────
    JEV_API_URL: str = "https://api.typesafe.ai/v1/systemone"
    JEV_API_KEY: str = ""

    def __init__(self):
        local = _load_dotenv(_LOCAL_ENV)

        self.JEV_API_KEY = (
            os.environ.get("TYPESAFE_API_KEY") or local.get("TYPESAFE_API_KEY", "")
        )

        self.JEV_API_URL = os.environ.get("JEV_API_URL") or self.JEV_API_URL
        self.KALSHI_BASE_URL = os.environ.get("KALSHI_BASE_URL") or self.KALSHI_BASE_URL

    @property
    def jev_configured(self) -> bool:
        return bool(self.JEV_API_KEY)

    def __repr__(self) -> str:
        return (
            f"Config(kalshi={'✓' if self.KALSHI_BASE_URL else '✗'}, "
            f"jev={'✓' if self.jev_configured else '✗'})"
        )


# Singleton
config = Config()
