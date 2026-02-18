# Comunio Intelligence Tool

Web app that uses the **Comunio REST API** to show your league's members with: **balance**, **team value**, **credit limit**, **salary (today)** (with seed), **last activity**, and **"In the red"**. UI language: DE or EN.

> **Tested with 1. Bundesliga only.** The tool should work with other Comunio leagues (2. Bundesliga, other competitions), but it has not been tested or developed for them. Season transition date (e.g. 2025-05-27) and API behaviour may differ per league.

## Requirements

- Python 3.10+
- Comunio account (credentials only in local `.env`)

## Setup

```bash
cd comunio-intelligence-tool
python3 -m venv venv
source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
```

Edit `.env`: set **COMUNIO_USER** and **COMUNIO_PASSWORD** at least. See **Configuration** for all options.

## Run

```bash
uvicorn backend.main:app --reload
```

Open **http://127.0.0.1:8000/** – table with Refresh (data cached ~1 min).

## Configuration

| Variable | Required | Description |
|----------|----------|-------------|
| `COMUNIO_USER` | yes | Comunio login |
| `COMUNIO_PASSWORD` | yes | Comunio password |
| `START_BUDGET` | no | League start balance in euros (default: 40,000,000) |
| `SEASON_START` | no | Season start (YYYY-MM-DD); transfers/salaries before this date are ignored |
| `SALARIES_ENABLED` | no | `true`/`false` – whether the league uses player salaries |
| `SEED_SQUADS_PATH` | no | Path to seed file for squad reconstruction (e.g. `data/seed_squads_2025-05-27.json`) |
| `COMUNIO_BASE_URL` | no | Base URL (default: https://www.comunio.de) |
| `POINTS_PAYOUT_WITH_SALARIES` | no | Euros per point when salaries are on (default: 30000) |
| `POINTS_PAYOUT_WITHOUT_SALARIES` | no | Euros per point when salaries are off (default: 10000) |
| `CACHE_TTL_SECONDS` | no | League overview cache TTL in seconds (default: 60) |
| `NEWS_CACHE_TTL_SECONDS` | no | News/transfer cache TTL in seconds (default: 900) |
| `CUTOFF_HOUR_MEZ` | no | Hour (MEZ/CEST) before which transfers count as previous day (default: 4) |
| `DEFAULT_LANGUAGE` | no | Default UI language: `de` or `en` (default: `de`) |
| `DEBUG_PLAYER_COUNT` | no | Set to 1 to log player count per manager and add `debug_player_count` to API |
| `DEBUG_SQUAD_COMPARE` | no | Set to 1 to compare reconstructed squads with live API squads |

## Notes

- **API:** Official Comunio API (login, standings, members, news via `_links`). No scraping.
- **Language:** Frontend DE/EN; default via `DEFAULT_LANGUAGE`, user choice in `localStorage`.

### What the frontend table shows

- **Balance:** Logged-in user = API `user.budget`. Others: **START_BUDGET** + news deltas (transfers + salaries) since **SEASON_START** + points payout (30k/10k € per point; configurable).
- **Team value:** From standings (exact).
- **Credit limit:** 1/4 team value or fixed; reduced when balance is negative; 0 if disabled.
- **Last activity:** Members API `lastaction` (when last online); fallback: last transfer from offer history.
- **In the red:** Balance < 0.
- **Salary:** 500 € + 0.1% market value per player per day. App uses debited amounts from news; with **SEED_SQUADS_PATH** set, balance uses reconstructed squads + quote history.

### Balance with salaries (seed file only)

The API has **no historical squads** – only the current squad per user. To show **balance including salaries** (and player count / salary today), the app needs a **seed file**: the squads of all managers at a **reference date** (e.g. season start or when “carry over players” was used). From that, squads are reconstructed from transfer news + quote history.

**Generating the seed file and player reference**

1. **Player reference** (name → tradable_id, for looking up players when filling the seed):
   ```bash
   venv/bin/python scripts/generate_players_reference.py
   ```
   Writes `data/players_reference.json`. **This file is not updated automatically.** Run it periodically (e.g. every 7 days) or whenever Comunio adds new tradables (e.g. new youth players during the season). Example cron (weekly):
   ```bash
   0 8 * * 0 cd /path/to/comunio-intelligence-tool && venv/bin/python scripts/generate_players_reference.py
   ```
   Optional: `--out data/players_reference.json`; `--no-inactive` to skip players from transfer news (only current league pool).

2. **Seed file** (all league users with empty player lists for the reference date):
   ```bash
   venv/bin/python scripts/generate_seed_file.py --date 2025-05-27
   ```
   Writes `data/seed_squads_2025-05-27.json`. Optional: `--out data/seed_squads_2025-05-27.json`.

3. **Fill the seed file:** For each manager, add the `tradable_id` of each player they had on the reference date. Look up names in `data/players_reference.json` and copy the `tradable_id` into the corresponding user list in the seed JSON. See `data/seed_squads_2025-05-27.example.json` for format.

4. **Use the seed:** In `.env` set `SEED_SQUADS_PATH=data/seed_squads_2025-05-27.json` (or your path) and `SEASON_START=2025-05-27` (or your season start). Restart the app.

**Testing reconstruction (optional):**  
Squad on a day: `venv/bin/python scripts/reconstruct_historical_squads.py --seed data/seed_squads_2025-05-27.json --date 2025-08-01`  
Date range: add `--from 2025-05-27 --to 2025-08-01`. With salaries: add `--with-values`.

**Troubleshooting (wrong player count or balance):**
- Manager not in seed or never in a transfer → add to seed
- User ID in standings differs from seed/news → check IDs
- Transfers on seed date are not applied (seed = state before that day)

**Further notes:**
News might only include transfers for the logged-in user. Balance errors cascade from one wrong salary day. Computer (user_id 1) is skipped. Swaps (EXCHANGES) with `price` are included in balance and reconstruction.

**Debug:** `DEBUG_PLAYER_COUNT=1` logs player count per manager and adds `debug_player_count` to the API. `DEBUG_SQUAD_COMPARE=1` compares reconstructed vs API squad per manager and adds `league_meta.debug_squad_compare`.

### Roadmap

- **Done:** “(You)” in table; historical squads from seed + news; balance with salary from squads when seed set. Transfer/news cache: TTL from `NEWS_CACHE_TTL_SECONDS`; cut-off 4:00 MEZ (transfers before that = previous day).
- **Next:** Show current matchday; further UI tweaks.
- **Later:** Statement for “you” only; fixed credit limits from API.

## Legal / ToS

Automated access to Comunio may violate the terms of use of comunio.de. Use at your own risk. See [Comunio ToS](https://www.comunio.de).

## License

All rights reserved. Use, modification, and distribution of this code require **explicit permission** from the author. See [LICENSE](LICENSE).

## API

- **GET /api/league** – JSON: `managers` (name, user_id, points, team_value, balance, max_minus_allowed, salary_today, last_activity, in_minus, …), `league_meta` (salaries_enabled, creditfactor, credit_factor_disabled, statement_user_id), `default_language`.

**Comunio API (GET /api/):** We use `game:standings`, `game:community:members`, `game:news`, `game:readOffersHistory`, `game:community`, `game:squad`, `game:userInfo`. Other links (e.g. `game:currentMatchday`, `game:matchdays`, `game:tradableQuoteHistory`) are available in the API response `_links` for future use.
