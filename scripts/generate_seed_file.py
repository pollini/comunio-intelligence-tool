#!/usr/bin/env python3
"""
Generate the seed file for historical squad reconstruction.

Uses credentials from .env (COMUNIO_USER, COMUNIO_PASSWORD).
Fetches all league managers via API and writes data/seed_squads_YYYY-MM-DD.json
with empty player lists; you then fill in tradable_ids for the reference date by hand.

Usage:
  venv/bin/python scripts/generate_seed_file.py
  venv/bin/python scripts/generate_seed_file.py --date 2025-05-27
  venv/bin/python scripts/generate_seed_file.py --out data/seed_squads_2025-05-27.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_project_root = Path(__file__).resolve().parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from backend.comunio_api import get_root, get_standings, login
from backend.scraper import _parse_standings_entry


def main() -> None:
    ap = argparse.ArgumentParser(description="Generate seed file from league data (.env)")
    ap.add_argument(
        "--date",
        type=str,
        default="2025-05-27",
        help="Reference date YYYY-MM-DD (default: 2025-05-27)",
    )
    ap.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Output path (default: data/seed_squads_<date>.json)",
    )
    args = ap.parse_args()

    try:
        seed_date = args.date
        if len(seed_date) != 10 or seed_date[4] != "-" or seed_date[7] != "-":
            raise ValueError("--date must be YYYY-MM-DD")
    except ValueError as e:
        print("Error:", e, file=sys.stderr)
        sys.exit(1)

    print("Login with credentials from .env …")
    token = login()["access_token"]
    root = get_root(token)
    links = root.get("_links") or {}
    standings_url = (links.get("game:standings") or {}).get("href")
    if not standings_url:
        print("No game:standings URL.", file=sys.stderr)
        sys.exit(1)

    raw = get_standings(token, standings_url)
    users: list[tuple[int, str]] = []
    for entry in raw or []:
        row = _parse_standings_entry(entry)
        if row and row.user_id is not None and row.user_id != 1:
            users.append((row.user_id, row.name or ""))

    if not users:
        print("No league members found.", file=sys.stderr)
        sys.exit(1)

    out_path = args.out
    if out_path is None:
        out_path = _project_root / "data" / f"seed_squads_{seed_date}.json"
    if not out_path.is_absolute():
        out_path = _project_root / out_path
    out_path.parent.mkdir(parents=True, exist_ok=True)

    data = {
        "seed_date": seed_date,
        "_names": {str(uid): name for uid, name in sorted(users, key=lambda x: x[0])},
        **{str(uid): [] for uid, _ in sorted(users, key=lambda x: x[0])},
    }
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    print(f"Seed file written: {out_path}")
    print(f"  Reference date: {seed_date}, {len(users)} users (empty player lists)")
    print("  Fill in tradable_ids for the reference date by hand. Reference: data/players_reference.json")


if __name__ == "__main__":
    main()
