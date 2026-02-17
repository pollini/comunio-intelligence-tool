"""Load configuration from environment."""
import os
from datetime import date, datetime, timedelta, time
from pathlib import Path

from dotenv import load_dotenv

# Load .env from project root (parent of backend/)
_env_path = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(_env_path)

def _env(key: str, default: str = "") -> str:
    val = os.getenv(key, default).strip()
    if len(val) >= 2 and val[0] == val[-1] == '"':
        val = val[1:-1]
    return val


def _int_env(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, str(default)))
    except ValueError:
        return default


def _bool_env(key: str, default: bool = False) -> bool:
    return os.getenv(key, "").lower() in ("1", "true", "yes")


# --- Required ---
COMUNIO_USER = _env("COMUNIO_USER")
COMUNIO_PASSWORD = _env("COMUNIO_PASSWORD")

# --- League & balance ---
START_BUDGET = _int_env("START_BUDGET", 40_000_000)

_SEASON_START = _env("SEASON_START")
try:
    SEASON_START: date | None = date.fromisoformat(_SEASON_START) if _SEASON_START else None
except ValueError:
    SEASON_START = None

SALARIES_ENABLED = _bool_env("SALARIES_ENABLED", False)

# Points payout per point (Comunio: 30k with salaries, 10k without)
POINTS_PAYOUT_WITH_SALARIES = _int_env("POINTS_PAYOUT_WITH_SALARIES", 30_000)
POINTS_PAYOUT_WITHOUT_SALARIES = _int_env("POINTS_PAYOUT_WITHOUT_SALARIES", 10_000)

# --- Seed for squad reconstruction ---
_seed_path = _env("SEED_SQUADS_PATH")
if _seed_path:
    _p = Path(_seed_path)
    if not _p.is_absolute():
        _p = Path(__file__).resolve().parent.parent / _p
    SEED_SQUADS_PATH: str | None = str(_p)
else:
    SEED_SQUADS_PATH = None

# --- API ---
COMUNIO_BASE_URL = os.getenv("COMUNIO_BASE_URL", "https://www.comunio.de")

# --- Cache (seconds) ---
CACHE_TTL_SECONDS = _int_env("CACHE_TTL_SECONDS", 60)
NEWS_CACHE_TTL_SECONDS = _int_env("NEWS_CACHE_TTL_SECONDS", 900)

# --- Transfer cut-off hour (MEZ/CEST): before this hour = previous day ---
CUTOFF_HOUR_MEZ = _int_env("CUTOFF_HOUR_MEZ", 4)
SALARY_CUTOFF_TIME = time(CUTOFF_HOUR_MEZ, 0)

# --- Frontend default language (de | en) ---
DEFAULT_LANGUAGE = _env("DEFAULT_LANGUAGE", "de").lower()[:2]
if DEFAULT_LANGUAGE not in ("de", "en"):
    DEFAULT_LANGUAGE = "de"

# --- Debug ---
DEBUG_PLAYER_COUNT = _bool_env("DEBUG_PLAYER_COUNT")
DEBUG_SQUAD_COMPARE = _bool_env("DEBUG_SQUAD_COMPARE")


def transfer_settlement_date(dt: datetime) -> date:
    """Map transfer timestamp to settlement day: before CUTOFF_HOUR_MEZ → previous day."""
    if dt.hour < CUTOFF_HOUR_MEZ:
        return dt.date() - timedelta(days=1)
    return dt.date()


def effective_today() -> date:
    """Current effective date (MEZ): before cut-off = yesterday, else today."""
    now = datetime.now()
    if now.hour >= CUTOFF_HOUR_MEZ:
        return date.today()
    return date.today() - timedelta(days=1)
