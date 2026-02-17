#!/usr/bin/env python3
"""
Probe whether GET /api/users/:userId/squad returns historical data via query parameters.

Run: venv/bin/python scripts/probe_squad_params.py

Parameters tested so far (all without effect – response same as without parameters):
  date, matchday, matchdayId, at, asOf, snapshot, day, timestamp,
  pointInTime, forDate, on, view=history, scope=historical
"""
from __future__ import annotations

import sys
from pathlib import Path

_project_root = Path(__file__).resolve().parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from backend.comunio_api import login, _api_base, JSON_HEADERS
import httpx


def main() -> None:
    token = login()["access_token"]
    base = _api_base()
    # Logged-in user (or fixed ID for testing)
    from backend.comunio_api import get_root
    root = get_root(token)
    uid = (root.get("user") or {}).get("id") or "13467138"
    if isinstance(uid, str) and uid.isdigit():
        uid = int(uid)

    url = f"{base}/users/{uid}/squad"
    headers = {**JSON_HEADERS, "Authorization": f"Bearer {token}"}

    # Reference without parameters
    r0 = httpx.get(url, headers=headers, timeout=15)
    if r0.status_code != 200:
        print("Error: squad without parameters:", r0.status_code)
        return
    ref = r0.json()
    ref_keys = list(ref.keys())
    ref_count = len(ref.get("items") or [])
    ref_sum = sum(
        (x.get("quotedprice") or 0)
        for x in (ref.get("items") or [])
        if isinstance(x, dict)
    )
    print(f"Reference (no params): keys={ref_keys}, items={ref_count}, sum(quotedprice)={ref_sum}")

    # Add more parameters here if you want to test others
    params_list = [
        {"date": "2025-02-01"},
        {"matchday": "20"},
        {"at": "2025-02-01"},
        {"asOf": "2025-02-01"},
    ]

    for params in params_list:
        r = httpx.get(url, headers=headers, params=params, timeout=15)
        data = r.json() if r.status_code == 200 else {}
        n = len(data.get("items") or [])
        s = sum(
            (x.get("quotedprice") or 0)
            for x in (data.get("items") or [])
            if isinstance(x, dict)
        )
        same = n == ref_count and s == ref_sum
        print(f"  {params} -> items={n}, sum={s}, unchanged={same}")


if __name__ == "__main__":
    main()
