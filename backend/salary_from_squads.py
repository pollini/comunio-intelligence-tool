"""
Salary calculation from historical squads + market values (quote history).

Rules:
- Transfers: Before 4:00 (excl.) count as previous day; from 4:00 as current day.
- Salary: Debit in news ~4:13 on day d+1 = salary for day d.
  Squad = state at 4:00 on day d+1 (debit day). Market values = day d (salary day), not d+1.
- Balance = START_BUDGET + Σ transfers − Σ salary debits.
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from backend.comunio_api import (
    get_news_salary_active_dates_since,
    get_news_transfer_events_since,
    get_quote_history,
)
from backend.config import SALARY_CUTOFF_TIME
from backend.historical_squads import load_seed_squads, reconstruct_squads_at_date

logger = logging.getLogger(__name__)

SALARY_BASE = 500
SALARY_PCT = 0.001
COMPUTER_USER_ID = 1


def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _default_quote_cache_path() -> Path:
    return _project_root() / "data" / "cache" / "quote_history.json"


def _load_quote_cache(cache_path: Path) -> dict[int, dict[str, int]]:
    """cache[tradable_id][ "YYYY-MM-DD" ] = price."""
    if not cache_path.exists():
        logger.debug("Quote cache: file not found (%s)", cache_path)
        return {}
    try:
        with open(cache_path, encoding="utf-8") as f:
            raw = json.load(f)
        out: dict[int, dict[str, int]] = {}
        for k, v in (raw or {}).items():
            try:
                tid = int(k)
            except (ValueError, TypeError):
                continue
            if isinstance(v, dict):
                out[tid] = {str(d): int(p) for d, p in v.items() if str(d) and isinstance(p, (int, float))}
            else:
                out[tid] = {}
        logger.info("Quote cache: %s players loaded from %s", len(out), cache_path.name)
        return out
    except Exception as e:
        logger.warning("Quote cache could not be loaded (%s): %s", cache_path, e)
        return {}


def _save_quote_cache(cache_path: Path, cache: dict[int, dict[str, int]]) -> None:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    with open(cache_path, "w", encoding="utf-8") as f:
        json.dump({str(tid): data for tid, data in cache.items()}, f, ensure_ascii=False)


def _get_value_for_date(
    access_token: str,
    tradable_id: int,
    d: date,
    cache: dict[int, dict[str, int]],
    cache_path: Path | None,
) -> int:
    key = d.isoformat()
    if tradable_id in cache:
        if key in cache[tradable_id]:
            return cache[tradable_id][key]
        # Exact d not in cache (e.g. new day): fallback = latest known MW on or before d
        best_date: date | None = None
        for iso in cache[tradable_id]:
            try:
                cdate = date.fromisoformat(iso)
            except ValueError:
                continue
            if cdate <= d and (best_date is None or cdate > best_date):
                best_date = cdate
        if best_date is not None:
            return cache[tradable_id][best_date.isoformat()]
    # Player not in cache or no date <= d → call API now
    logger.debug("Quote-history API for player %s (not in cache or no matching date)", tradable_id)
    hist = get_quote_history(access_token, tradable_id)
    if tradable_id not in cache:
        cache[tradable_id] = {}
    for h_date, price in hist:
        cache[tradable_id][h_date.isoformat()] = price
    val = cache[tradable_id].get(key)
    if val is not None:
        return val
    best_date = None
    for iso in cache[tradable_id]:
        try:
            cdate = date.fromisoformat(iso)
        except ValueError:
            continue
        if cdate <= d and (best_date is None or cdate > best_date):
            best_date = cdate
    if best_date is not None:
        return cache[tradable_id][best_date.isoformat()]
    return 0


def compute_salaries_from_historical_squads(
    access_token: str,
    news_url: str,
    seed_path: Path | str,
    since_date: date,
    *,
    quote_cache_path: Path | None = None,
    quote_cache: dict[int, dict[str, int]] | None = None,
    transfer_events: list[tuple[datetime, int, int, int]] | None = None,
    salary_active_dates: set[date] | None = None,
) -> dict[int, int]:
    """
    Compute cumulative salary debits per user since since_date from reconstructed squads
    and quote history (500 € + 0.1% MW per player/day). Only days on which salaries were
    debited per news count. Returns dict[user_id, total_salary_deduction] (computer 1 skipped).
    If transfer_events or salary_active_dates are passed, no news API calls are made for them.
    If quote_cache is passed, data/cache/quote_history.json is neither loaded nor saved
    (caller loads/saves once per request).
    """
    seed_path = Path(seed_path)
    if not seed_path.is_absolute():
        seed_path = _project_root() / seed_path
    if not seed_path.exists():
        logger.warning("Seed file not found: %s – salaries from squads skipped", seed_path)
        return {}

    cache_path = quote_cache_path or _default_quote_cache_path()
    if quote_cache is not None:
        cache = quote_cache
    else:
        cache = _load_quote_cache(cache_path)

    seed_date, seed_squads = load_seed_squads(seed_path)
    if transfer_events is None:
        transfer_events = get_news_transfer_events_since(access_token, news_url, since_date)
    if salary_active_dates is None:
        salary_active_dates = get_news_salary_active_dates_since(access_token, news_url, since_date)
    events = transfer_events
    salary_dates = salary_active_dates  # Debit dates (when debited ~4:30)
    if not salary_dates:
        logger.info("No salary days since %s – salary from squads yields 0", since_date)
        return {}

    # Debit on day d (news date) = salary for day d-1. Squad = 4:00 on day d, MW = day d-1 (salary day).
    salary_by_user: dict[int, int] = {}
    all_tradable_ids: set[int] = set()
    for d in salary_dates:
        squads_at_d = reconstruct_squads_at_date(
            seed_date, seed_squads, events, d, cutoff_time=SALARY_CUTOFF_TIME
        )
        for uid, squad in squads_at_d.items():
            all_tradable_ids.update(squad)

    logger.info(
        "Salary from squads: %s debit days (squad 4:00 on debit day, MW on salary day d−1), %s player IDs, cache: %s",
        len(salary_dates), len(all_tradable_ids), cache_path.name,
    )
    for d in sorted(salary_dates):
        squads_at_d = reconstruct_squads_at_date(
            seed_date, seed_squads, events, d, cutoff_time=SALARY_CUTOFF_TIME
        )
        salary_for_date = d - timedelta(days=1)  # Salary for this day → MW on this date
        for uid, squad in squads_at_d.items():
            if uid == COMPUTER_USER_ID:
                continue
            day_salary = 0
            for tid in squad:
                mv = _get_value_for_date(access_token, tid, salary_for_date, cache, cache_path)
                day_salary += SALARY_BASE + int(mv * SALARY_PCT)
            salary_by_user[uid] = salary_by_user.get(uid, 0) + day_salary

    if quote_cache is None:
        _save_quote_cache(cache_path, cache)
    logger.info(
        "Salary from squads: %s users with salary debits, total sum %s €",
        len(salary_by_user), sum(salary_by_user.values()),
    )
    return salary_by_user


def get_player_count_today(
    seed_path: Path | str,
    target_date: date,
    since_date: date,
    *,
    transfer_events: list[tuple[datetime, int, int, int]] | None = None,
    access_token: str | None = None,
    news_url: str | None = None,
    return_squads: bool = False,
) -> dict[int, int] | tuple[dict[int, int], dict[int, set[int]]]:
    """
    Player count per user at end of target_date (squad = seed + all transfers until end of target_date).
    For "salary today" main computes amount from team value (API) + this count.
    Returns dict[user_id, player_count] (computer 1 skipped). If return_squads=True: (player_count_by_user_id, squads_today) with squads_today[user_id] = set(tradable_id).
    """
    seed_path = Path(seed_path)
    if not seed_path.is_absolute():
        seed_path = _project_root() / seed_path
    if not seed_path.exists():
        return ({}, {}) if return_squads else {}
    seed_date, seed_squads = load_seed_squads(seed_path)
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug(
            "get_player_count_today: seed_date=%s, user IDs in seed: %s",
            seed_date, sorted(seed_squads.keys()),
        )
    if transfer_events is None and access_token and news_url:
        transfer_events = get_news_transfer_events_since(access_token, news_url, since_date)
    events = transfer_events or []
    squads_today = reconstruct_squads_at_date(seed_date, seed_squads, events, target_date)
    out = {
        uid: len(squad)
        for uid, squad in squads_today.items()
        if uid != COMPUTER_USER_ID
    }
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug("get_player_count_today: player count per user (excluding computer): %s", out)
    if return_squads:
        squads_filtered = {uid: s for uid, s in squads_today.items() if uid != COMPUTER_USER_ID}
        return (out, squads_filtered)
    return out


def compute_salary_today(
    access_token: str,
    news_url: str,
    seed_path: Path | str,
    target_date: date,
    since_date: date,
    *,
    quote_cache_path: Path | None = None,
    quote_cache: dict[int, dict[str, int]] | None = None,
    transfer_events: list[tuple[datetime, int, int, int]] | None = None,
) -> tuple[dict[int, int], dict[int, int]]:
    """
    Compute daily salary per user for "salary today" display (current at request time).
    Squad = state at end of target_date (all transfers until end of today). Formula: per player 500 € + 0.1% MW at target_date.
    Returns (salary_by_user, player_count_by_user) (computer 1 skipped). Note: "salary today" in main.py uses team value from API + get_player_count_today.
    """
    seed_path = Path(seed_path)
    if not seed_path.is_absolute():
        seed_path = _project_root() / seed_path
    if not seed_path.exists():
        return {}, {}

    cache_path = quote_cache_path or _default_quote_cache_path()
    if quote_cache is not None:
        cache = quote_cache
    else:
        cache = _load_quote_cache(cache_path)
    seed_date, seed_squads = load_seed_squads(seed_path)
    if transfer_events is None:
        transfer_events = get_news_transfer_events_since(access_token, news_url, since_date)
    events = transfer_events
    # "Current" display: squad at end of target_date + market value at target_date
    squads_today = reconstruct_squads_at_date(seed_date, seed_squads, events, target_date)

    salary_today_by_user: dict[int, int] = {}
    player_count_by_user: dict[int, int] = {}
    for uid, squad in squads_today.items():
        if uid == COMPUTER_USER_ID:
            continue
        player_count_by_user[uid] = len(squad)
        day_salary = 0
        for tid in squad:
            mv = _get_value_for_date(access_token, tid, target_date, cache, cache_path)
            day_salary += SALARY_BASE + int(mv * SALARY_PCT)
        salary_today_by_user[uid] = day_salary

    if quote_cache is None:
        _save_quote_cache(cache_path, cache)
    return salary_today_by_user, player_count_by_user
