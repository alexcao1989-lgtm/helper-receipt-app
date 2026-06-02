"""Supabase cloud database for Helper Receipt App (replaces local SQLite)."""

from __future__ import annotations

import os
from datetime import date, datetime, timedelta
from typing import Any

EXPENSE_CATEGORIES = ("Meat", "Seafood", "Vegetable", "Grocery")
EXPENSES_TABLE = "expenses"
BUDGET_TABLE = "budget_requests"

_client = None


def _get_secrets() -> dict[str, Any]:
    """Read from Streamlit secrets (cloud/local) or environment variables."""
    try:
        import streamlit as st

        return dict(st.secrets)
    except Exception:
        return {}


def get_supabase_client():
    """Create or reuse Supabase client."""
    global _client
    if _client is not None:
        return _client

    secrets = _get_secrets()
    url = secrets.get("SUPABASE_URL") or os.environ.get("SUPABASE_URL")
    key = secrets.get("SUPABASE_KEY") or os.environ.get("SUPABASE_KEY")

    if not url or not key:
        raise RuntimeError(
            "Missing SUPABASE_URL or SUPABASE_KEY. "
            "Add them to .streamlit/secrets.toml (local) or Streamlit Cloud Secrets."
        )

    from supabase import create_client

    _client = create_client(url, key)
    return _client


def init_db() -> None:
    """Verify Supabase connection (tables are created in Supabase SQL Editor)."""
    client = get_supabase_client()
    client.table(EXPENSES_TABLE).select("id").limit(1).execute()
    client.table(BUDGET_TABLE).select("id").limit(1).execute()


def _fetch_expenses() -> list[dict[str, Any]]:
    client = get_supabase_client()
    response = client.table(EXPENSES_TABLE).select("*").execute()
    return list(response.data or [])


def _fetch_budget_requests(status: str | None = None) -> list[dict[str, Any]]:
    client = get_supabase_client()
    query = client.table(BUDGET_TABLE).select("*")
    if status:
        query = query.eq("status", status)
    response = query.execute()
    rows = list(response.data or [])
    rows.sort(key=lambda r: (r.get("date", ""), r.get("id", 0)), reverse=True)
    return rows


def get_remaining_budget() -> float:
    """SUM(Approved budget_requests.amount) - SUM(expenses.actual_price)."""
    init_db()
    approved_rows = _fetch_budget_requests(status="Approved")
    expenses_rows = _fetch_expenses()

    total_budget = sum(float(r.get("amount") or 0) for r in approved_rows)
    total_expense = sum(float(r.get("actual_price") or 0) for r in expenses_rows)
    return round(total_budget - total_expense, 2)


def get_category_spending_anomalies() -> tuple[list[str], dict[str, float]]:
    """
    Past 7 days = this week per category.
    Past 28 days / 4 = weekly baseline.
    Alert if this week > baseline * 1.3.

    Returns: (alerts, current_week_spend_dict)
    """
    init_db()
    expenses = _fetch_expenses()

    categories = list(EXPENSE_CATEGORIES)
    alerts: list[str] = []
    current_week_spend = {cat: 0.0 for cat in categories}

    today = datetime.now()
    seven_days_ago = (today - timedelta(days=6)).strftime("%Y-%m-%d")
    twenty_eight_days_ago = (today - timedelta(days=27)).strftime("%Y-%m-%d")
    today_str = today.strftime("%Y-%m-%d")

    for cat in categories:
        this_week = sum(
            float(row.get("actual_price") or 0)
            for row in expenses
            if row.get("category") == cat
            and seven_days_ago <= (row.get("date") or "") <= today_str
        )
        past_month = sum(
            float(row.get("actual_price") or 0)
            for row in expenses
            if row.get("category") == cat
            and twenty_eight_days_ago <= (row.get("date") or "") <= today_str
        )
        baseline = past_month / 4.0
        current_week_spend[cat] = round(this_week, 2)

        if baseline > 0 and this_week > (baseline * 1.3):
            spike_pct = int(((this_week - baseline) / baseline) * 100)
            alerts.append(
                f"WARNING: {cat} spending spiked by {spike_pct}% this week "
                f"(HK$ {this_week:,.2f} vs baseline HK$ {baseline:,.2f}/week)."
            )

    return alerts, current_week_spend


def get_pending_budget_total() -> float:
    init_db()
    rows = _fetch_budget_requests(status="Pending")
    return round(sum(float(r.get("amount") or 0) for r in rows), 2)


def create_budget_request(amount: float, request_type: str, reason: str = "") -> int:
    init_db()
    client = get_supabase_client()
    payload = {
        "date": date.today().isoformat(),
        "amount": round(float(amount), 2),
        "type": request_type,
        "status": "Pending",
        "reason": reason or "",
    }
    response = client.table(BUDGET_TABLE).insert(payload).execute()
    if not response.data:
        raise RuntimeError("Failed to create budget request.")
    return int(response.data[0]["id"])


def get_pending_budget_requests() -> list[dict[str, Any]]:
    init_db()
    return _fetch_budget_requests(status="Pending")


def set_budget_request_status(request_id: int, status: str) -> bool:
    if status not in ("Approved", "Rejected"):
        return False

    init_db()
    client = get_supabase_client()
    existing = (
        client.table(BUDGET_TABLE)
        .select("status")
        .eq("id", request_id)
        .limit(1)
        .execute()
    )
    if not existing.data or existing.data[0].get("status") != "Pending":
        return False

    client.table(BUDGET_TABLE).update({"status": status}).eq("id", request_id).execute()
    return True


def save_expenses(rows: list[dict], expense_date: str | None = None) -> int:
    if not rows:
        return 0

    init_db()
    client = get_supabase_client()
    day = expense_date or date.today().isoformat()

    payload = [
        {
            "date": day,
            "item_name": row["item_name"],
            "category": row["category"],
            "ai_price": float(row["ai_price"]),
            "actual_price": float(row["actual_price"]),
            "is_modified": 1 if row["is_modified"] else 0,
        }
        for row in rows
    ]
    client.table(EXPENSES_TABLE).insert(payload).execute()
    return len(payload)


def _sum_expenses_between(start_str: str, end_str: str) -> float:
    init_db()
    total = sum(
        float(row.get("actual_price") or 0)
        for row in _fetch_expenses()
        if start_str <= (row.get("date") or "") <= end_str
    )
    return round(total, 2)


def get_current_month_spending() -> float:
    today = date.today()
    return _sum_expenses_between(today.replace(day=1).isoformat(), today.isoformat())


def get_current_week_spending() -> float:
    today = date.today()
    week_start = (today - timedelta(days=6)).isoformat()
    return _sum_expenses_between(week_start, today.isoformat())


def get_spending_by_category() -> dict[str, float]:
    init_db()
    totals = {cat: 0.0 for cat in EXPENSE_CATEGORIES}
    for row in _fetch_expenses():
        cat = str(row.get("category", ""))
        if cat in totals:
            totals[cat] += float(row.get("actual_price") or 0)
    return {cat: round(val, 2) for cat, val in totals.items()}


def get_modified_expense_records() -> list[dict[str, Any]]:
    init_db()
    rows = [
        row
        for row in _fetch_expenses()
        if int(row.get("is_modified") or 0) == 1
    ]
    rows.sort(key=lambda r: (r.get("date", ""), r.get("id", 0)), reverse=True)
    return rows


def get_category_weekly_baseline_dict() -> dict[str, float]:
    init_db()
    expenses = _fetch_expenses()
    today = datetime.now()
    start = (today - timedelta(days=27)).strftime("%Y-%m-%d")
    end = today.strftime("%Y-%m-%d")
    baseline = {cat: 0.0 for cat in EXPENSE_CATEGORIES}

    for cat in EXPENSE_CATEGORIES:
        total = sum(
            float(row.get("actual_price") or 0)
            for row in expenses
            if row.get("category") == cat
            and start <= (row.get("date") or "") <= end
        )
        baseline[cat] = round(total / 4.0, 2)

    return baseline
