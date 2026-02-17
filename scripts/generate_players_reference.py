#!/usr/bin/env python3
"""
Generate the player reference (name → tradable_id) for the league.

Uses credentials from .env. Fetches all players available in the community
via API and writes data/players_reference.json – sorted by name so you can
look up players when filling the seed file and copy the tradable_id.

The file is not updated automatically. Run this script periodically (e.g. every 7 days)
or when new tradables are added (e.g. new youth players during the season).

Usage:
  venv/bin/python scripts/generate_players_reference.py
  venv/bin/python scripts/generate_players_reference.py --out data/players_reference.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_project_root = Path(__file__).resolve().parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from backend.comunio_api import (
    get_community_players,
    get_root,
    get_tradables_from_transfer_news,
    login,
)
from backend.config import SEASON_START


def main() -> None:
    ap = argparse.ArgumentParser(description="Generate player reference (name + tradable_id) from API (.env)")
    ap.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Output path (default: data/players_reference.json)",
    )
    ap.add_argument(
        "--no-inactive",
        action="store_true",
        help="Only current league players (do not add from transfer news)",
    )
    args = ap.parse_args()

    print("Login with credentials from .env …")
    token = login()["access_token"]
    root = get_root(token)
    community = root.get("community") or {}
    cid = community.get("id") or ""
    if not cid:
        print("No community (league) found.", file=sys.stderr)
        sys.exit(1)

    print("Loading league players (community players, incl. includeInactive=1) …")
    players = get_community_players(token, cid)
    if not players:
        print("No players loaded from API.", file=sys.stderr)

    # Optional: add tradables from transfer news since season start (players who moved to another league)
    if not args.no_inactive and SEASON_START:
        links = root.get("_links") or {}
        news_link = links.get("game:news") or {}
        news_url = news_link.get("href") if isinstance(news_link, dict) else None
        if news_url and ":communityId" in news_url:
            news_url = news_url.replace(":communityId", cid)
        if news_url:
            print(f"Loading tradables from transfer news since {SEASON_START} (incl. inactive/transferred) …")
            from_news = get_tradables_from_transfer_news(token, news_url, SEASON_START)
            by_id = {p["tradable_id"]: p for p in players}
            added = 0
            for p in from_news:
                tid = p.get("tradable_id")
                if tid is not None and tid not in by_id:
                    by_id[tid] = p
                    added += 1
            players = list(by_id.values())
            if added:
                print(f"  + {added} players added from transfer news (no longer in current league).")
        else:
            print("No game:news URL – skipping addition from transfer news.")

    if not players:
        print("No players available.", file=sys.stderr)
        sys.exit(1)

    # Sort by name for easy lookup
    players.sort(key=lambda p: (p.get("name") or "").lower())

    out_path = args.out
    if out_path is None:
        out_path = _project_root / "data" / "players_reference.json"
    if not out_path.is_absolute():
        out_path = _project_root / out_path
    out_path.parent.mkdir(parents=True, exist_ok=True)

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "_comment": "Player reference: name + tradable_id for filling the seed file. Regenerate with: venv/bin/python scripts/generate_players_reference.py",
                "players": players,
            },
            f,
            ensure_ascii=False,
            indent=2,
        )

    print(f"Player reference written: {out_path}")
    print(f"  {len(players)} players (sorted by name; includes transfer-news players unless --no-inactive)")
    print("  Use the tradable_id in data/seed_squads_*.json.")


if __name__ == "__main__":
    main()
