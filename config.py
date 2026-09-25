"""Environment-based configuration loader.

Sources:
  - Kalshi base URL (public, no auth needed)
  - Jev API key from TYPESAFE_API_KEY env var
  - Supabase creds from SUPABASE_URL / SUPABASE_SERVICE_KEY env vars,
    or auto-loaded from ~/workspace/leadflow/.env
"""
import os
import re


def _load_dotenv(path: str | None = None) -> dict[str, str]:
    """Load key=value pairs from a .env file, returning a dict."""
    if path is None:
        path = os.path.expanduser("~/workspace/leadflow/.env")
    env = {}
    if not os.path.isfile(path):
        return env
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
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

    # ── Supabase ────────────────────────────────────────────────────────────
    SUPABASE_URL: str = ""
    SUPABASE_SERVICE_KEY: str = ""

    def __init__(self):
        # Try local kalshi-bot/.env first, then LeadFlow's .env, then hermes .env
        local_dotenv = _load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
        dotenv = _load_dotenv()
        hermes_dotenv = _load_dotenv(os.path.expanduser("~/.hermes/.env"))

        self.JEV_API_KEY = (
            os.environ.get("TYPESAFE_API_KEY")
            or local_dotenv.get("TYPESAFE_API_KEY", "")
            or dotenv.get("TYPESAFE_API_KEY", "")
            or hermes_dotenv.get("TYPESAFE_API_KEY", "")
        )
        self.SUPABASE_URL = (
            os.environ.get("SUPABASE_URL")
            or local_dotenv.get("SUPABASE_URL", "")
            or dotenv.get("SUPABASE_URL", "")
        )
        self.SUPABASE_SERVICE_KEY = (
            os.environ.get("SUPABASE_SERVICE_KEY")
            or local_dotenv.get("SUPABASE_SERVICE_KEY", "")
            or dotenv.get("SUPABASE_SERVICE_KEY", "")
        )

        # Override JEV_API_URL from env if set
        self.JEV_API_URL = os.environ.get("JEV_API_URL") or self.JEV_API_URL
        self.KALSHI_BASE_URL = os.environ.get("KALSHI_BASE_URL") or self.KALSHI_BASE_URL

    @property
    def jev_configured(self) -> bool:
        return bool(self.JEV_API_KEY)

    @property
    def supabase_configured(self) -> bool:
        return bool(self.SUPABASE_URL) and bool(self.SUPABASE_SERVICE_KEY)

    def __repr__(self) -> str:
        return (
            f"Config(kalshi={'✓' if self.KALSHI_BASE_URL else '✗'}, "
            f"jev={'✓' if self.jev_configured else '✗'}, "
            f"supabase={'✓' if self.supabase_configured else '✗'})"
        )


# Singleton
config = Config()