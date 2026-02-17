"""
Comunio REST API client.
Base: https://www.comunio.de/api
Login: POST /api/login → Bearer token; then GET /api/ for user, community, _links.
"""
import json
import logging
import re
from datetime import date, datetime
from typing import Any

import httpx

logger = logging.getLogger(__name__)

from backend.config import (
    COMUNIO_BASE_URL,
    COMUNIO_PASSWORD,
    COMUNIO_USER,
    transfer_settlement_date,
)

# Server returns application/hal+json – send both Accept types
JSON_HEADERS = {"Accept": "application/json, application/hal+json"}


def _parse_json(r: httpx.Response, context: str = "") -> Any:
    """Parse response as JSON. On empty/invalid body raise RuntimeError with status."""
    text = (r.text or "").strip()
    if not text:
        raise RuntimeError(
            f"Comunio API {context}: empty response (status {r.status_code}). "
            "Check login URL or credentials."
        )
    if not (text.startswith("{") or text.startswith("[")):
        preview = text[:150].replace("\n", " ")
        raise RuntimeError(
            f"Comunio API {context}: response is not JSON (status {r.status_code}). "
            f"First chars: {preview!r}…"
        )
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise RuntimeError(
            f"Comunio API {context}: invalid JSON (status {r.status_code}): {e}"
        )


def _api_base() -> str:
    base = (COMUNIO_BASE_URL or "").rstrip("/")
    return f"{base}/api" if base else "https://www.comunio.de/api"


def _tzoffset_minutes() -> int:
    """Timezone offset in minutes (e.g. CET = -60). Simple fallback."""
    try:
        import datetime
        now = datetime.datetime.now()
        return -int(now.utcoffset().total_seconds() / 60) if now.utcoffset() else -60
    except Exception:
        return -60


def login() -> dict[str, Any]:
    """
    POST /api/login with username, password, tzoffset.
    Returns token response: access_token, expires_in, token_type, refresh_token.
    """
    if not COMUNIO_USER:
        raise ValueError("COMUNIO_USER is empty or not set in .env (check .env in project root)")
    if not COMUNIO_PASSWORD:
        raise ValueError("COMUNIO_PASSWORD is empty or not set in .env (check .env in project root)")

    url = f"{_api_base()}/login"
    payload = {
        "username": COMUNIO_USER,
        "password": COMUNIO_PASSWORD,
        "tzoffset": _tzoffset_minutes(),
    }
    with httpx.Client(timeout=30.0) as client:
        r = client.post(url, json=payload, headers=JSON_HEADERS)
        data = _parse_json(r, "Login")
        r.raise_for_status()
    if "access_token" not in data:
        raise RuntimeError("Comunio login failed: no access_token in response")
    return data


def get_root(access_token: str) -> dict[str, Any]:
    """GET /api/ with Bearer token. Returns user, community, _links."""
    url = f"{_api_base()}/"
    headers = {**JSON_HEADERS, "Authorization": f"Bearer {access_token}"}
    with httpx.Client(timeout=30.0) as client:
        r = client.get(url, headers=headers)
        out = _parse_json(r, "GET /api/")
        r.raise_for_status()
        return out


def get_standings(access_token: str, standings_url: str) -> list[dict[str, Any]]:
    """
    GET standings – real table only with query params.
    URL: .../standings?period=total&wpe=true (as on https://www.comunio.de/standings/season/total)
    """
    sep = "&" if "?" in standings_url else "?"
    url = f"{standings_url}{sep}period=total&wpe=true"
    headers = {**JSON_HEADERS, "Authorization": f"Bearer {access_token}"}
    with httpx.Client(timeout=30.0) as client:
        r = client.get(url, headers=headers)
        r.raise_for_status()
        text = (r.text or "").strip()
        if not text:
            return []
        data = _parse_json(r, "Standings")
    # Response has "items" with ranking (totalPoints, _embedded.user, _embedded.teamInfo)
    if "items" in data and isinstance(data["items"], list):
        return data["items"]
    if "_embedded" in data and "standings" in data["_embedded"]:
        return data["_embedded"]["standings"]
    if isinstance(data, list):
        return data
    if "standings" in data:
        return data["standings"]
    return []


def get_squad(access_token: str, user_id: int) -> list[dict[str, Any]]:
    """
    GET /api/users/{userId}/squad – user's squad (current reference date).
    Returns list of players: [ {"id": int, "quotedprice": int}, ... ].
    quotedprice = market value in euros (for salary formula 0.1%).
    """
    url = f"{_api_base()}/users/{user_id}/squad"
    headers = {**JSON_HEADERS, "Authorization": f"Bearer {access_token}"}
    try:
        with httpx.Client(timeout=20.0) as client:
            r = client.get(url, headers=headers)
            if r.status_code != 200:
                return []
            data = _parse_json(r, f"Squad user {user_id}")
    except Exception:
        return []
    items = data.get("items")
    if not isinstance(items, list):
        return []
    out: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        pid = item.get("id")
        price = item.get("quotedprice")
        if pid is not None:
            out.append({
                "id": int(pid) if isinstance(pid, (int, float)) else pid,
                "market_value": int(price) if isinstance(price, (int, float)) else 0,
            })
    return out


def parse_budget_from_user_info(data: dict[str, Any]) -> int | None:
    """Parse budget from user-info response (root or GET /users/:id)."""
    raw = data.get("budget")
    if raw is None and "user" in data:
        raw = data["user"].get("budget")
    if raw is None:
        return None
    if isinstance(raw, int):
        return raw
    s = "".join(c for c in str(raw) if c in "0123456789-")
    return int(s) if s and s != "-" else None


def get_community_details(access_token: str, community_id: str) -> dict[str, Any] | None:
    """
    GET /api/communities/{id}?include=standings&lineBreaks2Description=1
    Returns rules (salaries, creditfactor, creditFactorDisabled) for league info.
    """
    if not community_id:
        return None
    url = f"{_api_base()}/communities/{community_id}"
    params = {"include": "standings", "lineBreaks2Description": "1"}
    headers = {**JSON_HEADERS, "Authorization": f"Bearer {access_token}"}
    try:
        with httpx.Client(timeout=30.0) as client:
            r = client.get(url, headers=headers, params=params)
            if r.status_code != 200:
                return None
            return _parse_json(r, "Community details")
    except Exception:
        return None


def parse_community_rules(details: dict[str, Any] | None) -> dict[str, Any]:
    """
    Read from community details: rules.salaries, creditfactor, creditFactorDisabled.
    Returns: salaries_enabled (bool), creditfactor (str), credit_factor_disabled (bool).
    """
    out = {"salaries_enabled": False, "creditfactor": None, "credit_factor_disabled": True}
    if not details:
        return out
    rules = details.get("rules")
    if not isinstance(rules, dict):
        return out
    # rules may have rules.items (nested) or be flat
    items = rules.get("items") if isinstance(rules.get("items"), dict) else rules
    sal = items.get("salaries")
    out["salaries_enabled"] = str(sal).lower() in ("true", "1", "yes")
    out["creditfactor"] = items.get("creditfactor") or items.get("creditFactor")
    out["credit_factor_disabled"] = bool(items.get("creditFactorDisabled", True))
    return out


def get_community_members(access_token: str, members_url: str) -> list[dict[str, Any]]:
    """
    GET community members (league members). URL from _links['game:community:members']['href'], replace :communityId with community.id. Returns _embedded.members or _embedded.users with name, points, teamValue, budget.
    """
    headers = {**JSON_HEADERS, "Authorization": f"Bearer {access_token}"}
    with httpx.Client(timeout=30.0) as client:
        r = client.get(members_url, headers=headers)
        r.raise_for_status()
        text = (r.text or "").strip()
        if not text:
            return []
        data = _parse_json(r, "Community members")
    if "_embedded" in data:
        emb = data["_embedded"]
        if "members" in emb:
            return emb["members"] if isinstance(emb["members"], list) else []
        if "users" in emb:
            return emb["users"] if isinstance(emb["users"], list) else []
    if isinstance(data, list):
        return data
    if "members" in data:
        return data["members"] if isinstance(data["members"], list) else []
    return []


COMPUTER_USER_ID = 1


# API returns only ~10–20 entries per request (limit ignored or capped).
# Pagination: start=0, then start=20, 40, … until hasMore=False or 0 entries.
NEWS_PAGE_LIMIT = 20

def get_news_page(
    access_token: str,
    news_url: str,
    start: int = 0,
    limit: int = NEWS_PAGE_LIMIT,
) -> dict[str, Any]:
    """GET one page of news (group=true, originaltypes=true)."""
    sep = "&" if "?" in news_url else "?"
    url = f"{news_url}{sep}group=true&originaltypes=true&start={start}&limit={limit}"
    headers = {**JSON_HEADERS, "Authorization": f"Bearer {access_token}"}
    with httpx.Client(timeout=60.0) as client:
        r = client.get(url, headers=headers)
        r.raise_for_status()
        text = (r.text or "").strip()
        if not text:
            return {"newsList": {"groups": {}, "hasMore": False}}
        return _parse_json(r, "News")


def _parse_news_entry_date(entry: dict[str, Any]) -> datetime | None:
    raw = entry.get("date")
    if not raw:
        return None
    try:
        s = str(raw).replace("Z", "+00:00")
        return datetime.fromisoformat(s)
    except Exception:
        return None


def _parse_salary_amount_from_title(title: str) -> int | None:
    """E.g. '61.830 € Spielergehälter wurden...' -> 61830."""
    m = re.search(r"([\d.,]+)\s*€", title or "")
    if not m:
        return None
    s = m.group(1).replace(".", "").replace(",", ".")
    try:
        return int(float(s))
    except ValueError:
        return None


# Salary formula (Comunio): 500 € per player + 0.1% of their market value, per player
# on the daily settlement date. We use the actual debited amounts from TRANSACTION_SALARIES;
# retrospective calculation would need squad + market values per day.


def get_news_balance_deltas_since(
    access_token: str,
    news_url: str,
    since_date: Any,
) -> list[tuple[int, int]]:
    """
    Fetch all news since since_date (date), parse TRANSACTION_TRANSFER and
    TRANSACTION_SALARIES. Returns list (user_id, amount_delta):
    - Buyer: (user_id, -price)
    - Seller: (user_id, +price)
    - Salary: (user_id, -amount)
    Computer (id=1) is skipped.
    """
    if since_date is None:
        since_date = date(2000, 1, 1)
    if not isinstance(since_date, date):
        since_date = date(2000, 1, 1)

    logger.info(
        "News: fetching transfers/salaries since %s (limit=%s per page, pagination start=0,20,40,...)",
        since_date, NEWS_PAGE_LIMIT,
    )

    deltas: list[tuple[int, int]] = []
    start = 0
    limit = NEWS_PAGE_LIMIT
    seen_older_than_since = False
    page = 0

    while True:
        page += 1
        data = get_news_page(access_token, news_url, start=start, limit=limit)
        news_list = data.get("newsList") or {}
        groups = news_list.get("groups") or {}
        if not isinstance(groups, dict):
            logger.warning("News: invalid groups structure")
            break

        entry_count = 0
        for _group_key, group in groups.items():
            if not isinstance(group, dict):
                continue
            entries = group.get("entries") or []
            for entry in entries:
                entry_count += 1
                entry_date = _parse_news_entry_date(entry)
                if entry_date is not None and transfer_settlement_date(entry_date) < since_date:
                    seen_older_than_since = True
                    continue
                typ = entry.get("type") or ""

                if typ == "TRANSACTION_TRANSFER":
                    msg = entry.get("message") or {}
                    for key in ("FROM_COMPUTER", "TO_COMPUTER", "BETWEEN_USERS", "EXCHANGES"):
                        for item in (msg.get(key) or []):
                            if not isinstance(item, dict):
                                continue
                            from_u = item.get("from") or {}
                            to_u = item.get("to") or {}
                            price = int(item.get("price") or 0)
                            from_id = from_u.get("id") if isinstance(from_u, dict) else None
                            to_id = to_u.get("id") if isinstance(to_u, dict) else None
                            # EXCHANGES: fee flows from to; price negative = from pays to
                            if key == "EXCHANGES":
                                if from_id is not None and from_id != COMPUTER_USER_ID:
                                    deltas.append((int(from_id), -price))
                                if to_id is not None and to_id != COMPUTER_USER_ID:
                                    deltas.append((int(to_id), price))
                            else:
                                if to_id is not None and to_id != COMPUTER_USER_ID:
                                    deltas.append((int(to_id), -price))
                                if from_id is not None and from_id != COMPUTER_USER_ID:
                                    deltas.append((int(from_id), price))

                elif typ == "TRANSACTION_SALARIES":
                    recipient = entry.get("recipient") or {}
                    rec_id = recipient.get("id") if isinstance(recipient, dict) else None
                    if rec_id is None:
                        continue
                    title = entry.get("title") or ""
                    amount = _parse_salary_amount_from_title(title)
                    if amount is not None and amount > 0:
                        deltas.append((int(rec_id), -amount))

        has_more = news_list.get("hasMore", False)
        logger.info(
            "News: page %s start=%s, entries=%s, hasMore=%s, deltas total=%s, olderThanSeason=%s",
            page, start, entry_count, has_more, len(deltas), seen_older_than_since,
        )
        if seen_older_than_since or not has_more:
            break
        start += limit

    logger.info("News: %s deltas total for balance", len(deltas))
    return deltas


def get_news_transfer_deltas_since(
    access_token: str,
    news_url: str,
    since_date: Any,
) -> list[tuple[int, int]]:
    """
    Like get_news_balance_deltas_since but only TRANSACTION_TRANSFER:
    (user_id, delta) – buyer -price, seller +price. Computer (id=1) skipped.
    For balance with salary from historical squads: Balance = START_BUDGET
    + sum(transfer_deltas) - computed_salaries.
    """
    if since_date is None:
        since_date = date(2000, 1, 1)
    if not isinstance(since_date, date):
        since_date = date(2000, 1, 1)

    deltas: list[tuple[int, int]] = []
    start = 0
    limit = NEWS_PAGE_LIMIT
    seen_older_than_since = False

    while True:
        data = get_news_page(access_token, news_url, start=start, limit=limit)
        news_list = data.get("newsList") or {}
        groups = news_list.get("groups") or {}
        if not isinstance(groups, dict):
            break
        for _group_key, group in groups.items():
            if not isinstance(group, dict):
                continue
            for entry in group.get("entries") or []:
                entry_date = _parse_news_entry_date(entry)
                if entry_date is not None and transfer_settlement_date(entry_date) < since_date:
                    seen_older_than_since = True
                    continue
                if (entry.get("type") or "") != "TRANSACTION_TRANSFER":
                    continue
                msg = entry.get("message") or {}
                for key in ("FROM_COMPUTER", "TO_COMPUTER", "BETWEEN_USERS", "EXCHANGES"):
                    for item in msg.get(key) or []:
                        if not isinstance(item, dict):
                            continue
                        from_u = item.get("from") or {}
                        to_u = item.get("to") or {}
                        price = int(item.get("price") or 0)
                        from_id = from_u.get("id") if isinstance(from_u, dict) else None
                        to_id = to_u.get("id") if isinstance(to_u, dict) else None
                        if key == "EXCHANGES":
                            if from_id is not None and from_id != COMPUTER_USER_ID:
                                deltas.append((int(from_id), -price))
                            if to_id is not None and to_id != COMPUTER_USER_ID:
                                deltas.append((int(to_id), price))
                        else:
                            if to_id is not None and to_id != COMPUTER_USER_ID:
                                deltas.append((int(to_id), -price))
                            if from_id is not None and from_id != COMPUTER_USER_ID:
                                deltas.append((int(from_id), price))
        has_more = news_list.get("hasMore", False)
        if seen_older_than_since or not has_more:
            break
        start += limit

    return deltas


def get_news_salary_active_dates_since(
    access_token: str,
    news_url: str,
    since_date: Any,
    *,
    only_for_recipient_user_id: int | None = None,
) -> set[date]:
    """
    Find all days on which salaries were debited (TRANSACTION_SALARIES).
    Salaries can be turned on/off anytime in a league; only days with an actual
    debit count as salary-active.
    If only_for_recipient_user_id is set (e.g. logged-in user): only days on which
    that user had a salary debit – then for the whole league that day is salary-active.
    """
    if since_date is None:
        since_date = date(2000, 1, 1)
    if not isinstance(since_date, date):
        since_date = date(2000, 1, 1)

    active_dates: set[date] = set()
    start = 0
    limit = NEWS_PAGE_LIMIT
    seen_older = False
    page = 0

    while True:
        page += 1
        data = get_news_page(access_token, news_url, start=start, limit=limit)
        news_list = data.get("newsList") or {}
        groups = news_list.get("groups") or {}
        if not isinstance(groups, dict):
            break
        for _gk, group in groups.items():
            if not isinstance(group, dict):
                continue
            for entry in group.get("entries") or []:
                if (entry.get("type") or "") != "TRANSACTION_SALARIES":
                    continue
                entry_date = _parse_news_entry_date(entry)
                if entry_date is None or entry_date.date() < since_date:
                    if entry_date and entry_date.date() < since_date:
                        seen_older = True
                    continue
                if only_for_recipient_user_id is not None:
                    recipient = entry.get("recipient") or {}
                    rec_id = recipient.get("id") if isinstance(recipient, dict) else None
                    if rec_id != only_for_recipient_user_id:
                        continue
                active_dates.add(entry_date.date())
        has_more = news_list.get("hasMore", False)
        if seen_older or not has_more:
            break
        start += limit

    return active_dates


def get_news_transfer_events_since(
    access_token: str,
    news_url: str,
    since_date: Any,
) -> list[tuple[datetime, int, int, int]]:
    """
    Fetch all TRANSACTION_TRANSFER news since since_date including player ID (tradable).
    Returns list (transfer_datetime, from_user_id, to_user_id, tradable_id) (with time).
    Computer (id=1) is included. For "salary for day D" squad counts until 3:00 on day D+1.
    """
    if since_date is None:
        since_date = date(2000, 1, 1)
    if not isinstance(since_date, date):
        since_date = date(2000, 1, 1)

    events: list[tuple[datetime, int, int, int]] = []
    start = 0
    limit = NEWS_PAGE_LIMIT
    seen_older = False
    page = 0

    while True:
        page += 1
        data = get_news_page(access_token, news_url, start=start, limit=limit)
        news_list = data.get("newsList") or {}
        groups = news_list.get("groups") or {}
        if not isinstance(groups, dict):
            break
        entry_count = 0
        for _gk, group in groups.items():
            if not isinstance(group, dict):
                continue
            for entry in group.get("entries") or []:
                entry_count += 1
                entry_date = _parse_news_entry_date(entry)
                if entry_date is None or transfer_settlement_date(entry_date) < since_date:
                    if entry_date and transfer_settlement_date(entry_date) < since_date:
                        seen_older = True
                    continue
                if (entry.get("type") or "") != "TRANSACTION_TRANSFER":
                    continue
                msg = entry.get("message") or {}
                for key in ("FROM_COMPUTER", "TO_COMPUTER", "BETWEEN_USERS", "EXCHANGES"):
                    for item in msg.get(key) or []:
                        if not isinstance(item, dict):
                            continue
                        from_u = item.get("from") or {}
                        to_u = item.get("to") or {}
                        from_id = from_u.get("id") if isinstance(from_u, dict) else None
                        to_id = to_u.get("id") if isinstance(to_u, dict) else None
                        if from_id is None or to_id is None:
                            continue
                        from_id, to_id = int(from_id), int(to_id)

                        def _tids_from_list(lst: Any) -> list[int]:
                            out: list[int] = []
                            if not isinstance(lst, list):
                                return out
                            for x in lst:
                                if isinstance(x, dict):
                                    i = x.get("id")
                                elif isinstance(x, (int, float)):
                                    i = int(x)
                                else:
                                    continue
                                if i is not None:
                                    out.append(int(i))
                            return out

                        # Swap with two directions: EXCHANGES (tradablesA = from→to, tradablesB = to→from) or fromTradables/toTradables
                        from_tids = _tids_from_list(
                            item.get("tradablesA")
                            or item.get("fromTradables")
                            or item.get("tradablesFrom")
                            or item.get("fromUserTradables")
                        )
                        to_tids = _tids_from_list(
                            item.get("tradablesB")
                            or item.get("toTradables")
                            or item.get("tradablesTo")
                            or item.get("toUserTradables")
                        )
                        if from_tids or to_tids:
                            for tid in from_tids:
                                events.append((entry_date, from_id, to_id, tid))
                            for tid in to_tids:
                                events.append((entry_date, to_id, from_id, tid))
                            continue

                        # Single player or one direction: "tradable" (1 player) or "tradables" (list)
                        # Structure per API: item = { tradable: { id, name }, from: { id, name }, to: { id, name }, price?, ... }
                        tradables_raw = item.get("tradables") or (item.get("tradable") and [item["tradable"]]) or []
                        if not isinstance(tradables_raw, list):
                            tradables_raw = [tradables_raw] if tradables_raw else []
                        added = 0
                        for tradable in tradables_raw:
                            tid = None
                            if isinstance(tradable, dict):
                                tid = tradable.get("id")
                            elif isinstance(tradable, (int, float)):
                                tid = int(tradable)
                            if tid is None:
                                continue
                            events.append((entry_date, from_id, to_id, int(tid)))
                            added += 1
                        # Unknown swap structure: log keys so we can adapt the format
                        if key in ("BETWEEN_USERS", "EXCHANGES") and added == 0:
                            logger.debug(
                                "%s item with no recognised tradables (maybe different format). Keys: %s",
                                key, list(item.keys()),
                            )
        has_more = news_list.get("hasMore", False)
        logger.info(
            "News transfer events: page %s, start=%s, entries=%s, hasMore=%s, events=%s",
            page, start, entry_count, has_more, len(events),
        )
        if seen_older or not has_more:
            break
        start += limit

    return events


def get_offers_history(access_token: str, offers_history_url: str) -> list[dict[str, Any]]:
    """GET offers history URL from _links['game:readOffersHistory']. Returns list of offer/transfer entries."""
    headers = {**JSON_HEADERS, "Authorization": f"Bearer {access_token}"}
    with httpx.Client(timeout=30.0) as client:
        r = client.get(offers_history_url, headers=headers)
        data = _parse_json(r, "Offers history")
        r.raise_for_status()
    if "_embedded" in data and "offers" in data["_embedded"]:
        return data["_embedded"]["offers"]
    if isinstance(data, list):
        return data
    if "offers" in data:
        return data["offers"]
    return []


def get_exchangemarket(
    access_token: str, community_id: str | int, user_id: int | str
) -> list[dict[str, Any]]:
    """
    GET /api/communities/:communityId/users/:userId/exchangemarket?include=trend,direct
    Returns all offers on the transfer market (for the league). Response has "items" with
    _embedded.owner (seller: id, name) and _embedded.player (player: name, quotedPrice).
    """
    base = _api_base()
    cid = str(community_id).strip()
    uid = str(user_id).strip()
    if not cid or not uid:
        return []
    url = f"{base}/communities/{cid}/users/{uid}/exchangemarket?include=trend,direct"
    headers = {**JSON_HEADERS, "Authorization": f"Bearer {access_token}"}
    try:
        with httpx.Client(timeout=30.0) as client:
            r = client.get(url, headers=headers)
            r.raise_for_status()
            data = _parse_json(r, "Exchangemarket")
    except Exception:
        return []
    if "items" in data and isinstance(data["items"], list):
        return data["items"]
    return []


def get_quote_history(access_token: str, tradable_id: int) -> list[tuple[date, int]]:
    """
    GET /api/players/:tradableId/quote-history – market values per day (up to 365 days).
    Returns list (date, quoted_price), newest first.
    """
    url = f"{_api_base()}/players/{tradable_id}/quote-history"
    headers = {**JSON_HEADERS, "Authorization": f"Bearer {access_token}"}
    try:
        with httpx.Client(timeout=15.0) as client:
            r = client.get(url, headers=headers)
            if r.status_code != 200:
                return []
            data = _parse_json(r, f"Quote history {tradable_id}")
    except Exception:
        return []
    coll = data.get("quoteCollection") or []
    if not isinstance(coll, list):
        return []
    out: list[tuple[date, int]] = []
    for item in coll:
        if not isinstance(item, dict):
            continue
        ts = item.get("timestamp")
        price = item.get("quotedPrice")
        if ts is None or price is None:
            continue
        try:
            s = str(ts).replace("+0100", "+01:00").replace("+0200", "+02:00").replace("Z", "+00:00")
            d = datetime.fromisoformat(s).date()
            out.append((d, int(price)))
        except (ValueError, TypeError):
            continue
    return out


def get_tradables_from_transfer_news(
    access_token: str,
    news_url: str,
    since_date: Any,
) -> list[dict[str, Any]]:
    """
    Read all TRANSACTION_TRANSFER news since since_date and collect each
    tradable (id, name, club, position) from message items.
    Useful for players who have moved to another league and no longer
    appear in the community players endpoint.
    Returns list of dicts like get_community_players: tradable_id, name, club, position.
    For duplicate tradable_id the last occurrence wins (most recent state).
    """
    if since_date is None:
        since_date = date(2000, 1, 1)
    if not isinstance(since_date, date):
        since_date = date(2000, 1, 1)

    by_id: dict[int, dict[str, Any]] = {}
    start = 0
    limit = NEWS_PAGE_LIMIT
    while True:
        data = get_news_page(access_token, news_url, start=start, limit=limit)
        news_list = data.get("newsList") or {}
        groups = news_list.get("groups") or {}
        if not isinstance(groups, dict):
            break
        for _gk, group in groups.items():
            if not isinstance(group, dict):
                continue
            for entry in group.get("entries") or []:
                if (entry.get("type") or "") != "TRANSACTION_TRANSFER":
                    continue
                msg = entry.get("message") or {}
                for key in ("FROM_COMPUTER", "TO_COMPUTER", "BETWEEN_USERS"):
                    for item in msg.get(key) or []:
                        if not isinstance(item, dict):
                            continue
                        tradable = item.get("tradable")
                        if not isinstance(tradable, dict):
                            continue
                        tid = tradable.get("id")
                        if tid is None:
                            continue
                        tid = int(tid)
                        club = tradable.get("club") or {}
                        club_name = club.get("name") if isinstance(club, dict) else ""
                        by_id[tid] = {
                            "tradable_id": tid,
                            "name": (tradable.get("name") or "").strip(),
                            "club": club_name,
                            "position": tradable.get("position") or "",
                        }
        if not news_list.get("hasMore", False):
            break
        start += limit
    out = list(by_id.values())
    logger.info("Transfer news: %s unique tradables since %s", len(out), since_date)
    return out


def get_community_players(
    access_token: str,
    community_id: str,
    *,
    page_size: int = 500,
    include_inactive_param: bool = True,
) -> list[dict[str, Any]]:
    """
    GET /api/communities/:communityId/players with pagination (start, limit).
    Returns all league tradables: list with id, name, club (name), position.
    include_inactive_param: if True, also send includeInactive=1 to the API
    (if supported, API returns players who moved to another league). If the API
    does not support it, use get_tradables_from_transfer_news + merge for inactive players.
    """
    if not community_id:
        return []
    url = f"{_api_base()}/communities/{community_id}/players"
    headers = {**JSON_HEADERS, "Authorization": f"Bearer {access_token}"}
    params: dict[str, Any] = {"start": 0, "limit": page_size}
    if include_inactive_param:
        params["includeInactive"] = 1
    out: list[dict[str, Any]] = []
    start = 0
    while True:
        try:
            with httpx.Client(timeout=60.0) as client:
                r = client.get(
                    url,
                    headers=headers,
                    params={**params, "start": start, "limit": page_size},
                )
                if r.status_code != 200:
                    break
                data = _parse_json(r, "Community players")
        except Exception:
            break
        tradables = data.get("tradables") or []
        if not isinstance(tradables, list):
            break
        for item in tradables:
            if not isinstance(item, dict):
                continue
            pid = item.get("id")
            if pid is None:
                continue
            club = item.get("club") or {}
            club_name = club.get("name") if isinstance(club, dict) else ""
            out.append({
                "tradable_id": int(pid),
                "name": (item.get("name") or "").strip(),
                "club": club_name,
                "position": item.get("position") or "",
            })
        if len(tradables) < page_size:
            break
        start += page_size
    return out
