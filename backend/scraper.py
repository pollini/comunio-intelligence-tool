"""
Fetch league and transfer data from Comunio via REST API.
Uses https://www.comunio.de/api (login → Bearer token, then /api/, standings, offers/history).
Transfer/news data is cached in data/cache/news_transfer.json (TTL from config.NEWS_CACHE_TTL_SECONDS).
Cut-off 4:00 MEZ (CUTOFF_HOUR_MEZ): before that hour, the effective day is still yesterday.
"""
import json
import logging
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

from backend.comunio_api import (
    get_community_details,
    get_community_members,
    get_exchangemarket,
    get_news_balance_deltas_since,
    get_news_transfer_deltas_since,
    get_offers_history,
    get_root,
    get_squad,
    get_standings,
    login,
    parse_budget_from_user_info,
    parse_community_rules,
)
from backend.config import (
    DEBUG_SQUAD_COMPARE,
    SEASON_START,
    SEED_SQUADS_PATH,
    START_BUDGET,
    effective_today,
)
from backend.salary_from_squads import (
    _default_quote_cache_path,
    _load_quote_cache,
    _save_quote_cache,
    compute_salaries_from_historical_squads,
    get_player_count_today,
)
from backend.models import LeagueData, ManagerLeagueRow, TransferData, TransferEntry

logger = logging.getLogger(__name__)

def _news_cache_path() -> Path:
    return Path(__file__).resolve().parent.parent / "data" / "cache" / "news_transfer.json"


def _news_cache_ttl_seconds() -> int:
    from backend.config import NEWS_CACHE_TTL_SECONDS
    return NEWS_CACHE_TTL_SECONDS


def _load_news_cache(news_url: str, since_date: date, ttl: int) -> dict[str, Any] | None:
    """Load cache from data/cache/news_transfer.json. Returns None on miss or expiry."""
    path = _news_cache_path()
    if not path.exists():
        return None
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except Exception:
        return None
    if raw.get("news_url") != news_url or raw.get("since_date") != since_date.isoformat():
        return None
    ts = raw.get("ts")
    if ts is None or (time.time() - float(ts)) > ttl:
        return None
    return raw


def _save_news_cache(
    news_url: str,
    since_date: date,
    transfer_deltas: list[tuple[int, int]],
    transfer_events: list[tuple[datetime, int, int, int]],
    salary_active_dates: set[date],
    balance_deltas: list[tuple[int, int]] | None,
) -> None:
    path = _news_cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "news_url": news_url,
        "since_date": since_date.isoformat(),
        "ts": time.time(),
        "transfer_deltas": list(transfer_deltas),
        "transfer_events": [[dt.isoformat(), a, b, c] for dt, a, b, c in transfer_events],
        "salary_active_dates": [d.isoformat() for d in salary_active_dates],
        "balance_deltas": list(balance_deltas) if balance_deltas is not None else None,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)


def _get_cached_news_data(
    news_url: str,
    since_date: date,
    access_token: str,
    use_salaries_from_squads: bool,
) -> tuple[list[tuple[int, int]], list[tuple[date, int, int, int]], set[date], list[tuple[int, int]] | None]:
    """
    Returns (transfer_deltas, transfer_events, salary_active_dates, balance_deltas_or_None).
    Reads/writes data/cache/news_transfer.json. On cache hit and valid TTL, no API calls.
    """
    from backend.comunio_api import (
        get_news_balance_deltas_since,
        get_news_salary_active_dates_since,
        get_news_transfer_events_since,
    )
    ttl = _news_cache_ttl_seconds()
    entry = _load_news_cache(news_url, since_date, ttl)
    if entry is not None:
        logger.info("News cache hit (data/cache/news_transfer.json, TTL %s s)", ttl)
        transfer_events = []
        for row in entry["transfer_events"]:
            dt_str = row[0]
            try:
                if "T" in dt_str or " " in dt_str:
                    dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
                    if dt.tzinfo:
                        dt = dt.replace(tzinfo=None)
                else:
                    dt = datetime.combine(date.fromisoformat(dt_str), datetime.min.time())
            except (ValueError, TypeError):
                continue
            transfer_events.append((dt, int(row[1]), int(row[2]), int(row[3])))
        salary_dates = {date.fromisoformat(s) for s in entry["salary_active_dates"]}
        balance_deltas = entry.get("balance_deltas")
        if balance_deltas is not None:
            balance_deltas = [tuple(x) for x in balance_deltas]
        return (
            [tuple(x) for x in entry["transfer_deltas"]],
            transfer_events,
            salary_dates,
            balance_deltas,
        )
    logger.info("News cache miss or expired, loading transfer/salary data from API …")
    transfer_deltas = get_news_transfer_deltas_since(access_token, news_url, since_date)
    transfer_events = get_news_transfer_events_since(access_token, news_url, since_date)
    salary_active_dates = get_news_salary_active_dates_since(access_token, news_url, since_date)
    balance_deltas: list[tuple[int, int]] | None = None
    if not use_salaries_from_squads:
        balance_deltas = get_news_balance_deltas_since(access_token, news_url, since_date)
    _save_news_cache(
        news_url, since_date,
        transfer_deltas, transfer_events, salary_active_dates, balance_deltas,
    )
    return transfer_deltas, transfer_events, salary_active_dates, balance_deltas


def _parse_int(val: Any) -> int:
    if isinstance(val, int):
        return val
    if isinstance(val, str):
        s = "".join(c for c in val if c in "0123456789-")
        if not s:
            return 0
        try:
            return int(s)
        except ValueError:
            return 0
    return 0


def _parse_standings_entry(entry: dict[str, Any]) -> Optional[ManagerLeagueRow]:
    """
    Map one API standings entry to ManagerLeagueRow.
    Supports: 1) items format (totalPoints, _embedded.user.name, _embedded.teamInfo.teamValue)
              2) flat format (user/member, points, teamValue, budget).
    """
    embedded = entry.get("_embedded") or {}
    user = embedded.get("user") or entry.get("user") or entry.get("member") or {}
    if isinstance(user, str):
        name = user
    else:
        name = (
            (user.get("name") or user.get("firstName") or "")
            or (entry.get("name") or entry.get("userName") or "")
        ).strip()
    if not name:
        return None
    team_info = embedded.get("teamInfo") or {}
    points = _parse_int(
        entry.get("totalPoints")
        or entry.get("points")
        or entry.get("teamPoints")
        or user.get("points")
    )
    team_value = _parse_int(
        team_info.get("teamValue")
        or entry.get("teamValue")
        or entry.get("team_value")
        or user.get("teamValue")
        or user.get("team_value")
    )
    budget_raw = entry.get("budget") or user.get("budget")
    balance_from_api = _parse_int(budget_raw) if budget_raw is not None else None
    raw_id = user.get("id") if isinstance(user, dict) else None
    user_id = int(raw_id) if raw_id is not None else None
    return ManagerLeagueRow(
        name=name,
        points=points,
        team_value=team_value,
        balance_from_api=balance_from_api,
        user_id=user_id,
    )


def _parse_offers_history_entry(entry: dict[str, Any]) -> list[TransferEntry]:
    """
    Map one API offer/history entry to one or two TransferEntry (buyer + seller).
    Assumes entry has date, amount, and buyer/seller or fromUser/toUser.
    """
    out: list[TransferEntry] = []
    amount = _parse_int(entry.get("amount") or entry.get("price"))
    if amount <= 0:
        return out

    raw_date = entry.get("date") or entry.get("created") or entry.get("timestamp") or ""
    transfer_date: Optional[date] = None
    if raw_date:
        try:
            if "T" in str(raw_date):
                transfer_date = datetime.fromisoformat(str(raw_date).replace("Z", "+00:00")).date()
            else:
                transfer_date = datetime.strptime(str(raw_date)[:10], "%Y-%m-%d").date()
        except Exception:
            pass
    if not transfer_date:
        return out

    def _user_name(obj: Any) -> str:
        if isinstance(obj, str):
            return obj.strip()
        if isinstance(obj, dict):
            return (obj.get("name") or obj.get("firstName") or "").strip()
        return ""

    buyer_name = _user_name(entry.get("buyer") or entry.get("toUser") or entry.get("bidder"))
    seller_name = _user_name(entry.get("seller") or entry.get("fromUser") or entry.get("owner"))

    if buyer_name:
        out.append(
            TransferEntry(
                manager_name=buyer_name,
                transfer_date=transfer_date,
                amount=amount,
                is_purchase=True,
            )
        )
    if seller_name:
        out.append(
            TransferEntry(
                manager_name=seller_name,
                transfer_date=transfer_date,
                amount=amount,
                is_purchase=False,
            )
        )
    return out


def _parse_offers_by_user(raw_offers: list[dict[str, Any]]) -> dict[int, list[dict[str, Any]]]:
    """Group transfer-market offers by seller (user_id). Each entry: {name, market_value, listed_since}."""
    by_user: dict[int, list[dict[str, Any]]] = {}
    for offer in raw_offers or []:
        if not isinstance(offer, dict):
            continue
        emb = offer.get("_embedded") or {}
        # Seller: owner (exchangemarket) or from, seller, fromUser
        from_user = (
            emb.get("owner")
            or offer.get("from") or offer.get("seller") or offer.get("fromUser")
            or emb.get("from") or emb.get("seller") or emb.get("user") or {}
        )
        if isinstance(from_user, dict):
            uid = from_user.get("id")
        else:
            uid = offer.get("userId") or offer.get("user_id") or offer.get("fromUserId") or offer.get("sellerId")
        if uid is None:
            continue
        try:
            uid = int(uid)
        except (TypeError, ValueError):
            continue
        # Player: player (exchangemarket) or tradable
        tradable = emb.get("player") or offer.get("tradable") or offer.get("player") or emb.get("tradable") or {}
        if not isinstance(tradable, dict):
            tradable = {}
        name = (
            (tradable.get("name") or offer.get("tradableName") or offer.get("playerName") or "").strip()
            or f"ID {tradable.get('id', offer.get('tradableId', '?'))}"
        )
        price = (
            tradable.get("quotedPrice") or tradable.get("quotedprice") or tradable.get("marketValue")
            or offer.get("quotedPrice") or offer.get("quotedprice") or offer.get("price") or offer.get("amount")
        )
        try:
            market_value = int(price) if price is not None else 0
        except (TypeError, ValueError):
            market_value = 0
        # When listed on TM (exchangemarket returns "date": "2026-02-13T04:33:05+0100")
        listed_since = offer.get("date") or offer.get("created") or offer.get("timestamp")
        if listed_since is not None:
            listed_since = str(listed_since).strip() or None
        by_user.setdefault(uid, []).append({
            "name": name,
            "market_value": market_value,
            "listed_since": listed_since,
        })
    return by_user


_LASTACTION_DISPLAY_TZ = ZoneInfo("Europe/Berlin")


def _last_action_by_id_from_members(members: list[dict[str, Any]]) -> dict[int, datetime]:
    """Build dict user_id -> datetime from members response (lastaction per user).
    Comunio sends time as local time (CET/CEST), often marked with Z.
    We interpret as Berlin time and only attach timezone (no conversion).
    """
    out: dict[int, datetime] = {}
    for m in members:
        uid = m.get("id")
        raw = m.get("lastaction")
        if uid is None or not raw:
            continue
        try:
            s = str(raw).replace("Z", "").strip()
            if "T" in s:
                # With or without Z: Comunio = local time (Berlin), only attach TZ
                dt = datetime.fromisoformat(s)
                if dt.tzinfo is not None:
                    dt = dt.replace(tzinfo=None)
                out[int(uid)] = dt.replace(tzinfo=_LASTACTION_DISPLAY_TZ)
            else:
                d = datetime.strptime(s[:10], "%Y-%m-%d").replace(
                    hour=0, minute=0, second=0, microsecond=0
                )
                out[int(uid)] = d.replace(tzinfo=_LASTACTION_DISPLAY_TZ)
        except Exception:
            pass
    return out


def fetch_all(
    client: Optional[Any] = None,
) -> tuple[LeagueData, TransferData, dict[int, date], dict[int, int], dict[str, Any], dict[int, int], dict[int, int], dict[int, list[dict[str, Any]]]]:
    """
    Login via API, fetch standings, members, news, offers (Transfermarkt), community rules.
    Returns (LeagueData, TransferData, last_action_by_id, balance_by_user_id, league_meta,
             salary_today_by_user_id, player_count_by_user_id, transfer_market_by_user_id).
    """
    token_response = login()
    access_token = token_response["access_token"]

    root = get_root(access_token)
    links = root.get("_links") or {}
    standings_link = links.get("game:standings") or {}
    standings_url = standings_link.get("href") if isinstance(standings_link, dict) else None
    history_link = links.get("game:readOffersHistory") or {}
    history_url = history_link.get("href") if isinstance(history_link, dict) else None
    news_link = links.get("game:news") or {}
    news_url = news_link.get("href") if isinstance(news_link, dict) else None
    community = root.get("community") or {}
    community_id = community.get("id") or ""
    members_link = links.get("game:community:members") or {}
    members_href = members_link.get("href") if isinstance(members_link, dict) else None
    members_url = members_href.replace(":communityId", community_id) if (members_href and community_id) else None

    if not standings_url:
        raise RuntimeError("Comunio API: no game:standings link in root")

    raw_standings = get_standings(access_token, standings_url)
    managers: list[ManagerLeagueRow] = []
    for entry in raw_standings:
        row = _parse_standings_entry(entry)
        if row:
            managers.append(row)

    # Fallback: empty standings → league from community members (no points/teamValue)
    if not managers and members_url:
        raw_members = get_community_members(access_token, members_url)
        for entry in raw_members:
            row = _parse_standings_entry(entry)
            if row:
                managers.append(row)

    # Community details: league name, salaries enabled?, credit factor (how much negative allowed)
    league_meta: dict[str, Any] = {"salaries_enabled": False, "creditfactor": None, "credit_factor_disabled": True, "league_name": ""}
    if community_id:
        details = get_community_details(access_token, community_id)
        league_meta = parse_community_rules(details)
        league_meta["league_name"] = (details or {}).get("name") or (community or {}).get("name") or ""

    # Members for "Last activity" (lastaction = when last online)
    last_action_by_id: dict[int, date] = {}
    if members_url:
        raw_members = get_community_members(access_token, members_url)
        last_action_by_id = _last_action_by_id_from_members(raw_members)

    # Other users' balance = START_BUDGET − purchases + sales − Σ salary per salary-day
    # (salary day y = team_MW_y * 0.001 + 500 * player_count_y; with seed from squads+quote history)
    # Transfer/news data cached (TTL 15 min, 5 min between 0:00 and 4:00)
    # Quote cache (market values): load once, use for cumulative + daily salary, save once
    balance_by_user_id: dict[int, int] = {}
    transfer_events_cached: list[tuple[datetime, int, int, int]] | None = None
    salary_active_dates_cached: set[date] | None = None
    quote_cache: dict[int, dict[str, int]] | None = None
    quote_cache_path: Path | None = None
    if not news_url:
        logger.warning("Balance from news: no game:news URL in _links")
    elif not SEASON_START:
        logger.warning("Balance from news: SEASON_START not set (.env) – using derived balance")
    else:
        seed_path = Path(SEED_SQUADS_PATH) if SEED_SQUADS_PATH else None
        use_salaries_from_squads = seed_path and seed_path.exists()
        transfer_deltas, transfer_events_cached, salary_active_dates_cached, balance_deltas = _get_cached_news_data(
            news_url, SEASON_START, access_token, use_salaries_from_squads
        )
        if use_salaries_from_squads:
            quote_cache_path = _default_quote_cache_path()
            quote_cache = _load_quote_cache(quote_cache_path)
            salary_by_user = compute_salaries_from_historical_squads(
                access_token, news_url, str(seed_path), SEASON_START,
                quote_cache=quote_cache,
                transfer_events=transfer_events_cached,
                salary_active_dates=salary_active_dates_cached,
            )
            _save_quote_cache(quote_cache_path, quote_cache)
            for uid, delta in transfer_deltas:
                balance_by_user_id[uid] = balance_by_user_id.get(uid, START_BUDGET) + delta
            for uid, total_salary in salary_by_user.items():
                balance_by_user_id[uid] = balance_by_user_id.get(uid, START_BUDGET) - total_salary
            logger.info(
                "Balance: START_BUDGET + Transfers - Salaries (from squads), %s users",
                len(balance_by_user_id),
            )
        else:
            if balance_deltas is not None:
                for uid, delta in balance_deltas:
                    balance_by_user_id[uid] = balance_by_user_id.get(uid, START_BUDGET) + delta
            logger.info(
                "Balance from news (Transfers + Salaries): START_BUDGET=%s, %s users",
                START_BUDGET, len(balance_by_user_id),
            )
        logger.info(
            "Examples (user_id=balance): %s",
            dict(list(balance_by_user_id.items())[:5]),
        )

    # Player count today (squad end of today). Salary today = computed in main from team value (API) + count
    salary_today_by_user_id: dict[int, int] = {}
    player_count_by_user_id: dict[int, int] = {}
    if news_url and SEASON_START:
        seed_path = Path(SEED_SQUADS_PATH) if SEED_SQUADS_PATH else None
        if seed_path and seed_path.exists():
            squads_reconstructed = None
            try:
                if DEBUG_SQUAD_COMPARE:
                    _counts, squads_reconstructed = get_player_count_today(
                        str(seed_path), effective_today(), SEASON_START,
                        access_token=access_token, news_url=news_url,
                        transfer_events=transfer_events_cached,
                        return_squads=True,
                    )
                    player_count_by_user_id = _counts
                else:
                    player_count_by_user_id = get_player_count_today(
                        str(seed_path), effective_today(), SEASON_START,
                        access_token=access_token, news_url=news_url,
                        transfer_events=transfer_events_cached,
                    )
                if logger.isEnabledFor(logging.DEBUG):
                    manager_uids = {m.user_id for m in managers if m.user_id is not None}
                    count_uids = set(player_count_by_user_id.keys())
                    missing = manager_uids - count_uids
                    if missing:
                        logger.debug(
                            "Player count: managers without entry (maybe not in seed or no transfers): user_ids=%s",
                            sorted(missing),
                        )
                    logger.debug(
                        "Player count today: user_id -> count for %s of %s managers",
                        len(count_uids), len(manager_uids),
                    )
                # Compare reconstructed squad with current API squad (debug)
                if DEBUG_SQUAD_COMPARE and squads_reconstructed is not None:
                    compare_list: list[dict[str, Any]] = []
                    for m in managers:
                        if m.user_id is None:
                            continue
                        recon = squads_reconstructed.get(m.user_id) or set()
                        api_players = get_squad(access_token, m.user_id)
                        api_ids = {p["id"] for p in api_players if p.get("id") is not None}
                        only_in_recon = sorted(recon - api_ids)
                        only_in_api = sorted(api_ids - recon)
                        match = len(only_in_recon) == 0 and len(only_in_api) == 0
                        compare_list.append({
                            "name": m.name,
                            "user_id": m.user_id,
                            "count_reconstructed": len(recon),
                            "count_api": len(api_ids),
                            "match": match,
                            "only_in_reconstruction": only_in_recon,
                            "only_in_api": only_in_api,
                        })
                        if not match:
                            logger.info(
                                "Squad mismatch: %s (user_id=%s) – reconstruction: %s, API: %s; only ours: %s; only in API: %s",
                                m.name, m.user_id, len(recon), len(api_ids),
                                only_in_recon[:5], only_in_api[:5],
                            )
                    league_meta["debug_squad_compare"] = compare_list
                    mismatches = sum(1 for c in compare_list if not c["match"])
                    logger.info(
                        "Squad compare (reconstruction vs API): %s of %s managers identical, %s mismatches",
                        len(compare_list) - mismatches, len(compare_list), mismatches,
                    )
            except Exception as e:
                logger.warning("Could not determine player count today: %s", e, exc_info=True)

    # Current transfer market offers: exchangemarket (works; /offers returns 500)
    transfer_market_by_user_id: dict[int, list[dict[str, Any]]] = {}
    current_user_id_for_api = root.get("user") or {}
    api_user_id = current_user_id_for_api.get("id") if isinstance(current_user_id_for_api, dict) else None
    if community_id and api_user_id is not None:
        try:
            raw_items = get_exchangemarket(access_token, community_id, api_user_id)
            transfer_market_by_user_id = _parse_offers_by_user(raw_items)
            total_offers = sum(len(v) for v in transfer_market_by_user_id.values())
            logger.info(
                "Transfer market (exchangemarket): %s offers from %s users loaded",
                total_offers, len(transfer_market_by_user_id),
            )
        except Exception as e:
            logger.warning("Transfer market offers not loaded: %s", e, exc_info=True)
    else:
        logger.debug("Transfer market: community_id or user_id missing for exchangemarket")

    # Logged-in user balance: as-is from root (GET /api/ → user.budget), no calculation
    current_user = root.get("user") or {}
    current_user_id = current_user.get("id")
    if current_user_id is not None:
        try:
            uid_int = int(current_user_id) if isinstance(current_user_id, (int, str)) else None
            if uid_int is not None:
                budget = parse_budget_from_user_info(current_user)
                if budget is not None:
                    balance_by_user_id[uid_int] = budget
                    league_meta["statement_user_id"] = uid_int  # Main does not add points for this user
                    logger.info(
                        "Balance for user %s from API (user.budget): %s",
                        uid_int, budget,
                    )
        except (ValueError, TypeError):
            pass

    transfers: list[TransferEntry] = []
    if history_url:
        try:
            raw_history = get_offers_history(access_token, history_url)
            for entry in raw_history:
                transfers.extend(_parse_offers_history_entry(entry))
        except Exception:
            pass

    league_data = LeagueData(managers=managers)
    transfer_data = TransferData(transfers=transfers)
    return (
        league_data,
        transfer_data,
        last_action_by_id,
        balance_by_user_id,
        league_meta,
        salary_today_by_user_id,
        player_count_by_user_id,
        transfer_market_by_user_id,
    )


def get_last_transfer_dates(transfer_data: TransferData) -> dict[str, date]:
    """From transfer list, compute last transfer date per manager (by name)."""
    by_manager: dict[str, date] = {}
    for t in transfer_data.transfers:
        name = t.manager_name.strip()
        if name and (name not in by_manager or t.transfer_date > by_manager[name]):
            by_manager[name] = t.transfer_date
    return by_manager
