#!/usr/bin/env python3
"""
Reconstruct historical squads from transfer news + reference-date squads.

Requires a seed file, e.g. data/seed_squads_2025-05-27.json, with each user's
squad at the reference date (e.g. 2025-05-27 for 1. Bundesliga season transition). Format:
  { "2891049": [32880, 33130, ...], "13467138": [34116, ...], ... }
(user_id as string, values = tradable_id of players)

Usage:
  venv/bin/python scripts/reconstruct_historical_squads.py --seed data/seed_squads_2025-05-27.json --from 2025-05-01 --to 2025-06-15
  venv/bin/python scripts/reconstruct_historical_squads.py --seed data/seed_squads_2025-05-27.json --date 2025-06-01  # single date

Optional: --with-values fetches quote-history per player and prints salary per day (many API calls).
"""
from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

_project_root = Path(__file__).resolve().parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from backend.config import SEASON_START
from backend.comunio_api import (
    get_root,
    get_news_salary_active_dates_since,
    get_news_transfer_events_since,
    get_quote_history,
    login,
)
from backend.historical_squads import (
    load_seed_squads,
    reconstruct_squads_at_date,
    reconstruct_squads_for_date_range,
)

SALARY_BASE = 500
SALARY_PCT = 0.001


def main() -> None:
    ap = argparse.ArgumentParser(description="Reconstruct historical squads from news + reference date")
    ap.add_argument("--seed", required=True, type=Path, help="Path to seed JSON (e.g. data/seed_squads_2025-05-27.json)")
    ap.add_argument("--from", dest="date_from", type=str, help="Start date YYYY-MM-DD")
    ap.add_argument("--to", dest="date_to", type=str, help="End date YYYY-MM-DD")
    ap.add_argument("--date", type=str, help="Single date YYYY-MM-DD (instead of --from/--to)")
    ap.add_argument("--with-values", action="store_true", help="Fetch quote-history per player, compute salary (many API calls)")
    args = ap.parse_args()

    seed_path = args.seed if args.seed.is_absolute() else _project_root / args.seed
    if not seed_path.exists():
        print("Seed file not found:", seed_path, file=sys.stderr)
        sys.exit(1)

    seed_date, seed_squads = load_seed_squads(seed_path)
    print("Reference date:", seed_date, "Users with squad:", len(seed_squads))

    token = login()["access_token"]
    root = get_root(token)
    news_url = (root.get("_links") or {}).get("game:news") or {}
    news_href = news_url.get("href") if isinstance(news_url, dict) else None
    if not news_href:
        print("No game:news URL.", file=sys.stderr)
        sys.exit(1)

    since = SEASON_START or date(seed_date.year, 1, 1)
    if since > seed_date:
        since = date(seed_date.year - 1, 8, 1)  # rough season start
    print("Loading transfer events since", since, "...")
    events = get_news_transfer_events_since(token, news_href, since)
    print("Transfer events:", len(events))

    computer_uid = 1

    if args.date:
        target = date.fromisoformat(args.date)
        squads_at = reconstruct_squads_at_date(seed_date, seed_squads, events, target)
        print(f"\nSquads on {target}:")
        for uid in sorted(squads_at.keys()):
            if uid == computer_uid:
                continue
            pids = squads_at[uid]
            print(f"  User {uid}: {len(pids)} players", list(pids)[:5], "..." if len(pids) > 5 else "")
    else:
        date_from = date.fromisoformat(args.date_from or str(seed_date))
        date_to = date.fromisoformat(args.date_to or str(seed_date))
        if date_from > date_to:
            date_from, date_to = date_to, date_from
        by_date = reconstruct_squads_for_date_range(seed_date, seed_squads, events, date_from, date_to)
        print(f"\nSquads from {date_from} to {date_to} ({len(by_date)} days)")
        for d in sorted(by_date.keys())[:5]:
            total_players = sum(len(s) for s in by_date[d].values())
            print(f"  {d}: {len(by_date[d])} users, {total_players} players total")
        if len(by_date) > 5:
            print("  ...")

    if args.with_values and (args.date or args.date_from):
        # Single day: salary per user from quote-history; only if salaries were active that day (news)
        target = date.fromisoformat(args.date) if args.date else date.fromisoformat(args.date_to or args.date_from)
        squads_at = reconstruct_squads_at_date(seed_date, seed_squads, events, target)
        current_uid = (root.get("user") or {}).get("id")
        uid_int = int(current_uid) if current_uid is not None and (isinstance(current_uid, int) or (isinstance(current_uid, str) and current_uid.isdigit())) else None
        salary_active_dates = get_news_salary_active_dates_since(
            token, news_href, since, only_for_recipient_user_id=uid_int
        ) if uid_int is not None else set()
        salaries_active_today = target in salary_active_dates
        if not salaries_active_today:
            print(f"\nSalaries on {target} not active per news (no debit for logged-in user that day) → 0 €.")
        else:
            print(f"\nSalary on {target} (500€ + 0.1% MW per player, from quote-history):")
            for uid in sorted(squads_at.keys()):
                if uid == computer_uid:
                    continue
                pids = list(squads_at[uid])
                total_salary = 0
                for pid in pids:
                    hist = get_quote_history(token, pid)
                    val_by_date = {d: v for d, v in hist}
                    mv = val_by_date.get(target, 0)
                    total_salary += SALARY_BASE + int(mv * SALARY_PCT)
                print(f"  User {uid}: {total_salary:,} € ({len(pids)} players)".replace(",", "."))


if __name__ == "__main__":
    main()
