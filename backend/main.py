"""
FastAPI app: GET /api/league returns combined league overview with derived balance and last transfer activity.
"""
import logging
import time as time_module
from datetime import date, datetime, time as dt_time
from pathlib import Path

from fastapi import FastAPI, HTTPException

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s [%(name)s] %(message)s",
)
logging.getLogger("backend").setLevel(logging.INFO)
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from backend.balance import derived_balance, is_in_minus
from backend.config import (
    CACHE_TTL_SECONDS,
    DEBUG_PLAYER_COUNT,
    DEFAULT_LANGUAGE,
    POINTS_PAYOUT_WITH_SALARIES,
    POINTS_PAYOUT_WITHOUT_SALARIES,
    COMUNIO_PASSWORD,
    COMUNIO_USER,
    SEASON_START,
    START_BUDGET,
)
from backend.models import ManagerOverview
from backend.scraper import (
    fetch_all,
    get_last_transfer_dates,
)

_cached: list[ManagerOverview] | None = None
_cached_meta: dict | None = None
_cache_time: float = 0


app = FastAPI(title="Comunio Intelligence Tool", version="0.1.0")


def _build_league_overview() -> tuple[list[ManagerOverview], dict]:
    """Fetch from Comunio, compute balances and last activity; return (managers, league_meta)."""
    (
        league_data,
        transfer_data,
        last_action_by_id,
        balance_by_user_id,
        league_meta,
        salary_today_by_user_id,
        player_count_by_user_id,
        transfer_market_by_user_id,
    ) = fetch_all()
    last_dates = get_last_transfer_dates(transfer_data)

    points_payout = POINTS_PAYOUT_WITH_SALARIES if league_meta.get("salaries_enabled") else POINTS_PAYOUT_WITHOUT_SALARIES
    credit_disabled = league_meta.get("credit_factor_disabled", True)
    credit_factor = league_meta.get("creditfactor")

    result: list[ManagerOverview] = []
    for m in league_data.managers:
        if m.user_id is not None and m.user_id in balance_by_user_id:
            balance = balance_by_user_id[m.user_id]
        else:
            balance = derived_balance(
                m,
                transfer_data,
                start_budget=START_BUDGET,
                season_start=SEASON_START,
            )
        statement_user_id = league_meta.get("statement_user_id")
        if statement_user_id is None or m.user_id != statement_user_id:
            balance += m.points * points_payout
        if credit_disabled:
            max_minus = 0
        else:
            base_credit: int | None = None
            if credit_factor == "dynamic":
                base_credit = m.team_value // 4
            elif credit_factor is not None:
                try:
                    base_credit = int(credit_factor)
                except (TypeError, ValueError):
                    pass
            if base_credit is not None:
                max_minus = base_credit + (balance if balance < 0 else 0)
            else:
                max_minus = None
        last_activity = (
            last_action_by_id.get(m.user_id)
            if m.user_id is not None
            else last_dates.get(m.name)
        )
        if isinstance(last_activity, date) and not isinstance(last_activity, datetime):
            last_activity = datetime.combine(last_activity, dt_time.min) if last_activity else None
        player_count = player_count_by_user_id.get(m.user_id) if m.user_id is not None else None
        if player_count is not None and m.team_value is not None:
            salary_today = (player_count * 500) + int(m.team_value * 0.001)
        else:
            salary_today = None
        raw_tm = transfer_market_by_user_id.get(m.user_id) if m.user_id is not None else None
        transfer_market_players = (
            [{"name": p["name"], "market_value": p["market_value"], "listed_since": p.get("listed_since")} for p in (raw_tm or [])]
        ) if raw_tm else None
        result.append(
            ManagerOverview(
                name=m.name,
                user_id=m.user_id,
                points=m.points,
                team_value=m.team_value,
                balance=balance,
                last_activity=last_activity,
                in_minus=is_in_minus(balance),
                max_minus_allowed=max_minus,
                salary_today=salary_today,
                player_count=player_count,
                transfer_market_players=transfer_market_players,
            )
        )

    if DEBUG_PLAYER_COUNT:
        logger = logging.getLogger("backend.main")
        logger.info("=== Debug player count (compare with Comunio website) ===")
        for r in result:
            uid = r.user_id if r.user_id is not None else "—"
            cnt = r.player_count if r.player_count is not None else "—"
            logger.info("  %s | user_id=%s | player_count=%s", r.name, uid, cnt)
        logger.info("=== End debug player count ===")

    return result, league_meta


@app.get("/api/check-env")
def check_env():
    """Debug: indicates whether .env was loaded (only if set, no values)."""
    return {
        "user_set": bool(COMUNIO_USER),
        "password_set": bool(COMUNIO_PASSWORD),
    }


@app.get("/api/league")
def get_league():
    """
    Return league overview: managers, league_meta (salaries_enabled, creditfactor, credit_factor_disabled).
    Cached for CACHE_TTL_SECONDS.
    """
    global _cached, _cached_meta, _cache_time
    now = time_module.monotonic()
    if _cached is not None and _cached_meta is not None and (now - _cache_time) < CACHE_TTL_SECONDS:
        out = {"managers": [m.model_dump(mode="json") for m in _cached], "league_meta": _cached_meta}
    else:
        try:
            _cached, _cached_meta = _build_league_overview()
            _cache_time = now
            out = {"managers": [m.model_dump(mode="json") for m in _cached], "league_meta": _cached_meta}
        except ValueError as e:
            import logging
            logging.warning("GET /api/league ValueError: %s", e)
            raise HTTPException(status_code=400, detail=str(e))
        except RuntimeError as e:
            raise HTTPException(status_code=502, detail=str(e))
        except Exception as e:
            import logging
            logging.exception("GET /api/league error")
            raise HTTPException(status_code=500, detail=str(e))
    if DEBUG_PLAYER_COUNT:
        out["debug_player_count"] = [
            {"name": m.name, "user_id": m.user_id, "player_count": m.player_count}
            for m in _cached
        ]
    out["default_language"] = DEFAULT_LANGUAGE
    return out


# Serve frontend: static files from frontend/ or project root
_frontend = Path(__file__).resolve().parent.parent / "frontend"
if _frontend.exists():
    app.mount("/static", StaticFiles(directory=str(_frontend)), name="static")

    @app.get("/")
    def index():
        return FileResponse(_frontend / "index.html")
else:
    @app.get("/")
    def index():
        return {"message": "Comunio Intelligence Tool API", "league": "/api/league"}
