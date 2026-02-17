"""
Reconstruct historical squads from transfer news.

Requires a reference-date squad (e.g. 2025-05-27) per user – you provide
data/seed_squads_YYYY-MM-DD.json with format:
  { "user_id": [tradable_id, ...], ... }
(user_id as string, e.g. "2891049")

From the news (TRANSACTION_TRANSFER with tradable.id) all transfers since
season start are loaded. For each desired date each user's squad is computed:
  - For D >= reference date: start = reference squads, then apply all transfers
    with reference_date < T <= D (seller loses player, buyer gains).
  - For D < reference date: start = reference squads, then reverse all transfers
    with D < T <= reference_date.
"""
from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from backend.comunio_api import get_news_transfer_events_since

_TZ_BERLIN = ZoneInfo("Europe/Berlin")


def _to_naive_berlin(dt: datetime) -> datetime:
    """Normalise datetime to naive Berlin time for comparison with cutoff_dt."""
    if dt.tzinfo is not None:
        return dt.astimezone(_TZ_BERLIN).replace(tzinfo=None)
    return dt


def load_seed_squads(path: Path | str) -> tuple[date, dict[int, set[int]]]:
    """
    Load seed JSON. Expects seed_date in JSON or YYYY-MM-DD in filename.
    Returns (seed_date, squads); squads[user_id] = set(tradable_id).
    """
    path = Path(path)
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    seed_date_str = data.get("seed_date")
    if seed_date_str:
        seed_date = date.fromisoformat(seed_date_str)
    else:
        stem = path.stem
        for part in stem.split("_"):
            if len(part) == 10 and part[4] == "-" and part[7] == "-":
                seed_date = date.fromisoformat(part)
                break
        else:
            raise ValueError("Neither seed_date in JSON nor YYYY-MM-DD in filename found.")
    squads: dict[int, set[int]] = {}
    users = data.get("users") or data.get("squads") or data
    if isinstance(users, dict) and "users" not in data and "squads" not in data:
        for uid_str, player_list in users.items():
            if uid_str in ("seed_date",) or uid_str.startswith("_"):
                continue
            try:
                uid = int(uid_str)
            except ValueError:
                continue
            squads[uid] = set()
            for p in player_list or []:
                try:
                    squads[uid].add(int(p))
                except (TypeError, ValueError):
                    pass
    return seed_date, squads


def _squads_copy(squads: dict[int, set[int]]) -> dict[int, set[int]]:
    return {uid: set(s) for uid, s in squads.items()}


def reconstruct_squads_at_date(
    seed_date: date,
    seed_squads: dict[int, set[int]],
    transfer_events: list[tuple[datetime, int, int, int]],
    target_date: date,
    *,
    cutoff_time: time | None = None,
) -> dict[int, set[int]]:
    """
    Reconstruct all users' squads at target_date (optionally up to cutoff_time on target_date).

    transfer_events: list of (transfer_datetime, from_user_id, to_user_id, tradable_id).
    cutoff_time: if set (e.g. 4:00), only transfers before (target_date, cutoff_time) count
                 – for salary on debit day d = squad until 4:00 on day d.
    """
    squads = _squads_copy(seed_squads)
    if target_date >= seed_date:
        if cutoff_time is not None:
            cutoff_dt = datetime.combine(target_date, cutoff_time)
            events = [(t, f, to, pid) for (t, f, to, pid) in transfer_events
                      if _to_naive_berlin(t) < cutoff_dt and t.date() > seed_date]
        else:
            events = [(t, f, to, pid) for (t, f, to, pid) in transfer_events
                      if seed_date < t.date() <= target_date]
        events.sort(key=lambda x: x[0])
        for _t, from_uid, to_uid, tradable_id in events:
            squads.setdefault(from_uid, set()).discard(tradable_id)
            squads.setdefault(to_uid, set()).add(tradable_id)
    else:
        events = [(t, f, to, pid) for (t, f, to, pid) in transfer_events
                  if target_date < t.date() <= seed_date]
        events.sort(key=lambda x: x[0], reverse=True)
        for _t, from_uid, to_uid, tradable_id in events:
            squads.setdefault(to_uid, set()).discard(tradable_id)
            squads.setdefault(from_uid, set()).add(tradable_id)
    return squads


def reconstruct_squads_for_date_range(
    seed_date: date,
    seed_squads: dict[int, set[int]],
    transfer_events: list[tuple[datetime, int, int, int]],
    date_from: date,
    date_to: date,
) -> dict[date, dict[int, set[int]]]:
    """Reconstruct squads for each day from date_from to date_to (incl.), end-of-day."""
    out: dict[date, dict[int, set[int]]] = {}
    d = date_from
    while d <= date_to:
        out[d] = reconstruct_squads_at_date(seed_date, seed_squads, transfer_events, d)
        d += timedelta(days=1)
    return out
