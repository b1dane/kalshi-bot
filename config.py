"""Environment-based configuration loader.

Secrets are resolved in this order (first non-empty wins):
  1. Real environment variables
  2. ./.env next to this file (kalshi-bot's own .env)
  3. Fallback .env files from OTHER projects (compat only; a notice is logged
     whenever a value is taken from one, because that couples this bot to
     another project's credentials, e.g. paper trades landing in LeadFlow's
     Supabase project).
"""
import os

_HERE = os.path.dirname(os.path.abspath(__file__))
_LOCAL_ENV = os.path.join(_HERE, ".env")
_LEADFLOW_ENV = os.path.expanduser("~/workspace/leadflow/.env")
_HERMES_ENV = os.path.expanduser("~/.hermes/.env")


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

    # ── Supabase ────────────────────────────────────────────────────────────
    SUPABASE_URL: str = ""
    SUPABASE_SERVICE_KEY: str = ""

    def __init__(self):
        self.notices: list[str] = []
        local = _load_dotenv(_LOCAL_ENV)
        leadflow = _load_dotenv(_LEADFLOW_ENV)
        hermes = _load_dotenv(_HERMES_ENV)

        self.JEV_API_KEY = self._resolve(
            "TYPESAFE_API_KEY", local, [("LeadFlow", leadflow), ("Hermes", hermes)]
        )
        self.SUPABASE_URL = self._resolve(
            "SUPABASE_URL", local, [("LeadFlow", leadflow)]
        )
        self.SUPABASE_SERVICE_KEY = self._resolve(
            "SUPABASE_SERVICE_KEY", local, [("LeadFlow", leadflow)]
        )

        self.JEV_API_URL = os.environ.get("JEV_API_URL") or self.JEV_API_URL
        self.KALSHI_BASE_URL = os.environ.get("KALSHI_BASE_URL") or self.KALSHI_BASE_URL

    def _resolve(
        self,
        name: str,
        local: dict[str, str],
        fallbacks: list[tuple[str, dict[str, str]]],
    ) -> str:
        value = os.environ.get(name) or local.get(name, "")
        if value:
            return value
        for label, env in fallbacks:
            if env.get(name):
                self.notices.append(
                    f"{name} was loaded from {label}'s .env, not kalshi-bot's own .env. "
                    f"Put it in {_LOCAL_ENV} to decouple this bot."
                )
                return env[name]
        return ""

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
