"""Data models for league and transfer data."""
from datetime import date, datetime
from typing import Any, Optional

from pydantic import BaseModel


class ManagerLeagueRow(BaseModel):
    """One manager as parsed from the league table (or API standings)."""
    name: str
    points: int = 0
    team_value: int = 0  # team value in euros
    balance_from_api: Optional[int] = None  # from API when available, else derived
    user_id: Optional[int] = None  # Comunio user id, for matching with members (lastaction)


class TransferEntry(BaseModel):
    """Single transfer from the news/transfer overview."""
    manager_name: str
    transfer_date: date
    amount: int  # positive for purchase, negative for sale (or separate is_purchase flag)
    is_purchase: bool = True


class LeagueData(BaseModel):
    """Raw scraped league table."""
    managers: list[ManagerLeagueRow] = []


class TransferData(BaseModel):
    """Raw scraped transfer list."""
    transfers: list[TransferEntry] = []


class TransferMarketPlayer(BaseModel):
    """A player on the transfer market (seller = user)."""
    name: str
    market_value: int
    listed_since: Optional[str] = None  # ISO date/time when listed on transfer market


class ManagerOverview(BaseModel):
    """One manager as shown in the API/UI."""
    name: str
    user_id: Optional[int] = None  # Comunio user id (for "you" marker)
    points: int = 0
    team_value: int = 0
    balance: int = 0  # balance including points payout
    last_activity: Optional[datetime] = None  # last activity (when last online, from API lastaction)
    in_minus: bool = False
    max_minus_allowed: Optional[int] = None  # credit limit (e.g. 1/4 team value), null if unknown
    salary_today: Optional[int] = None  # daily salary (500 € + 0.1% MW per player, from today's squad)
    player_count: Optional[int] = None  # number of players in squad (when seed set)
    transfer_market_players: Optional[list[TransferMarketPlayer]] = None  # this user's players on transfer market + MW