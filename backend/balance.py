"""
Derive per-manager balance from start budget, transfers, and optional salaries.
"""
from datetime import date
from typing import Optional

from backend.config import SALARIES_ENABLED, START_BUDGET
from backend.models import ManagerLeagueRow, TransferData, TransferEntry


def net_transfer_spending(transfers: list[TransferEntry], manager_name: str) -> int:
    """
    Sum of (purchases - sales) for the given manager.
    Positive = net spending (money left account).
    """
    total = 0
    name = manager_name.strip()
    for t in transfers:
        if t.manager_name.strip() != name:
            continue
        if t.is_purchase:
            total += t.amount
        else:
            total -= t.amount
    return total


def cumulative_salaries(
    manager_name: str,
    team_value_at_day: int,
    from_date: date,
    to_date: date,
) -> int:
    """
    Placeholder for daily salary formula.
    When SALARIES_ENABLED is True, Comunio charges salaries per day based on squad value.
    Formula not publicly documented; override or configure in config.
    Returns total salary cost from from_date to to_date (inclusive).
    """
    # Stub: assume a simple rule e.g. (team_value * 0.0001) per day
    # User can replace with actual formula from official rules
    days = (to_date - from_date).days + 1
    if days <= 0:
        return 0
    per_day = max(0, int(team_value_at_day * 0.0001))  # 0.01% of team value per day
    return per_day * days


def derived_balance(
    manager: ManagerLeagueRow,
    transfer_data: TransferData,
    start_budget: int = START_BUDGET,
    salaries_enabled: bool = SALARIES_ENABLED,
    season_start: Optional[date] = None,
) -> int:
    """
    Balance: if API provides budget (balance_from_api), use it; else start_budget - net_transfer_spending - [optional] cumulative_salaries.
    """
    if manager.balance_from_api is not None:
        return manager.balance_from_api
    spending = net_transfer_spending(transfer_data.transfers, manager.name)
    balance = start_budget - spending
    if salaries_enabled and season_start:
        today = date.today()
        if today > season_start:
            salaries = cumulative_salaries(
                manager.name,
                manager.team_value,
                season_start,
                today,
            )
            balance -= salaries
    return balance


def available_purchasing_power(balance: int, team_value: int) -> int:
    """Maximal bid = balance + team value (Comunio rule)."""
    return balance + team_value


def is_in_minus(balance: int) -> bool:
    return balance < 0
