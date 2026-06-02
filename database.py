"""Supabase cloud database layer for Helper Receipt App."""

from __future__ import annotations

import os
from datetime import date, datetime, timedelta
from typing import Any

EXPENSE_CATEGORIES = ("Meat", "Seafood", "Vegetable", "Grocery")
EXPENSES_TABLE = "expenses"
BUDGET_TABLE = "budget_requests"

_client = None


def _get_secrets() -> dict[str, Any]:
    """Read secrets from Streamlit Cloud/local, fallback to env vars."""
    try:
        import streamlit as st

        return dict(st.secrets)
    except Exception:
        return {}


def get_supabase_client():
    """Create and cache Supabase client."""
    global _client
    if _client is not None:
        return _client

    secrets = _get_secrets()
    url = secrets.get("SUPABASE_URL") or os.environ.get("SUPABASE_URL")
    key = secrets.get("SUPABASE_KEY") or os.environ.get("SUPABASE_KEY")
    if not url or not key:
        raise RuntimeError(
            "Missing SUPABASE_URL or SUPABASE_KEY. "
            "Add them to Streamlit secrets or environment variables."
        )

    from supabase import create_client

    _client = create_client(url, key)
    return _client


def init_db() -> None:
    """Smoke-check required tables. Schema should be created in Supabase SQL editor."""
    client = get_supabase_client()
    client.table(EXPENSES_TABLE).select("id").limit(1).execute()
    client.table(BUDGET_TABLE).select("id").limit(1).execute()


def _today_str() -> str:
    return date.today().isoformat()


def _sort_by_date_desc(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows.sort(key=lambda r: (r.get("date", ""), r.get("id", 0)), reverse=True)
    return rows


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
    return _sort_by_date_desc(rows)


def insert_expense(
    *,
    item_name: str,
    category: str,
    ai_price: float,
    actual_price: float,
    is_modified: bool,
    expense_date: str | None = None,
    image_url: str | None = None,
) -> int:
    """
    Insert one expense record into Supabase.
    Supports image_url for cloud image storage linkage.
    """
    init_db()
    client = get_supabase_client()
    payload = {
        "date": expense_date or _today_str(),
        "item_name": item_name,
        "category": category,
        "ai_price": float(ai_price),
        "actual_price": float(actual_price),
        "is_modified": 1 if is_modified else 0,
        "image_url": image_url or "",
    }
    response = client.table(EXPENSES_TABLE).insert(payload).execute()
    if not response.data:
        raise RuntimeError("Failed to insert expense row.")
    return int(response.data[0]["id"])


def save_expenses(
    rows: list[dict[str, Any]],
    expense_date: str | None = None,
    image_url: str | None = None,
) -> int:
    """
    Backward-compatible batch insert used by current app.
    If image_url is provided, apply it to all inserted rows.
    """
    if not rows:
        return 0

    inserted = 0
    day = expense_date or _today_str()
    for row in rows:
        insert_expense(
            item_name=row["item_name"],
            category=row["category"],
            ai_price=float(row["ai_price"]),
            actual_price=float(row["actual_price"]),
            is_modified=bool(row["is_modified"]),
            expense_date=day,
            image_url=row.get("image_url") or image_url,
        )
        inserted += 1
    return inserted


def delete_expense(expense_id: int) -> bool:
    """Delete one expense row by id from Supabase."""
    init_db()
    client = get_supabase_client()
    existing = client.table(EXPENSES_TABLE).select("id").eq("id", expense_id).limit(1).execute()
    if not existing.data:
        return False
    client.table(EXPENSES_TABLE).delete().eq("id", expense_id).execute()
    return True


def get_filtered_expenses(days_offset: int = 7) -> list[dict[str, Any]]:
    """
    Get expense rows within the last `days_offset` days (inclusive), newest first.
    Useful for weekly/monthly fold views in frontend.
    """
    init_db()
    days = max(int(days_offset), 1)
    start = (date.today() - timedelta(days=days - 1)).isoformat()
    end = _today_str()

    rows = [
        row
        for row in _fetch_expenses()
        if start <= str(row.get("date", "")) <= end
    ]
    return _sort_by_date_desc(rows)


def _sum_actual(rows: list[dict[str, Any]]) -> float:
    return round(sum(float(r.get("actual_price") or 0) for r in rows), 2)


def _sum_ai(rows: list[dict[str, Any]]) -> float:
    return round(sum(float(r.get("ai_price") or 0) for r in rows), 2)


def _category_totals(rows: list[dict[str, Any]]) -> dict[str, float]:
    totals = {cat: 0.0 for cat in EXPENSE_CATEGORIES}
    for row in rows:
        cat = str(row.get("category", ""))
        if cat in totals:
            totals[cat] += float(row.get("actual_price") or 0)
    return {k: round(v, 2) for k, v in totals.items()}


def get_spending_summary(days_offset: int) -> dict[str, Any]:
    """Generic slice summary by day window."""
    rows = get_filtered_expenses(days_offset=days_offset)
    modified_count = sum(1 for r in rows if int(r.get("is_modified") or 0) == 1)
    return {
        "range_days": int(days_offset),
        "count": len(rows),
        "total_actual": _sum_actual(rows),
        "total_ai": _sum_ai(rows),
        "modified_count": modified_count,
        "by_category": _category_totals(rows),
        "rows": rows,
    }


def get_week_summary() -> dict[str, Any]:
    return get_spending_summary(days_offset=7)


def get_month_summary() -> dict[str, Any]:
    return get_spending_summary(days_offset=30)


def get_all_time_summary() -> dict[str, Any]:
    init_db()
    rows = _sort_by_date_desc(_fetch_expenses())
    modified_count = sum(1 for r in rows if int(r.get("is_modified") or 0) == 1)
    return {
        "range_days": "all",
        "count": len(rows),
        "total_actual": _sum_actual(rows),
        "total_ai": _sum_ai(rows),
        "modified_count": modified_count,
        "by_category": _category_totals(rows),
        "rows": rows,
    }


def get_remaining_budget() -> float:
    """SUM(Approved budget_requests.amount) - SUM(expenses.actual_price)."""
    init_db()
    approved_rows = _fetch_budget_requests(status="Approved")
    all_expenses = _fetch_expenses()
    total_budget = sum(float(r.get("amount") or 0) for r in approved_rows)
    total_expense = sum(float(r.get("actual_price") or 0) for r in all_expenses)
    return round(total_budget - total_expense, 2)


def get_category_spending_anomalies() -> tuple[list[str], dict[str, float]]:
    """
    Baseline logic:
    - current_week_spend: last 7 days by category
    - baseline weekly avg: last 28 days total / 4
    - alert if this_week > baseline * 1.3
    Returns (alerts, current_week_spend_dict)
    """
    init_db()
    expenses = _fetch_expenses()

    alerts: list[str] = []
    current_week_spend = {cat: 0.0 for cat in EXPENSE_CATEGORIES}
    today = datetime.now()
    week_start = (today - timedelta(days=6)).strftime("%Y-%m-%d")
    month_start = (today - timedelta(days=27)).strftime("%Y-%m-%d")
    today_str = today.strftime("%Y-%m-%d")

    for cat in EXPENSE_CATEGORIES:
        this_week = sum(
            float(row.get("actual_price") or 0)
            for row in expenses
            if row.get("category") == cat and week_start <= (row.get("date") or "") <= today_str
        )
        last_28_days = sum(
            float(row.get("actual_price") or 0)
            for row in expenses
            if row.get("category") == cat and month_start <= (row.get("date") or "") <= today_str
        )
        baseline = last_28_days / 4.0
        current_week_spend[cat] = round(this_week, 2)

        if baseline > 0 and this_week > baseline * 1.3:
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
        "date": _today_str(),
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
    existing = client.table(BUDGET_TABLE).select("status").eq("id", request_id).limit(1).execute()
    if not existing.data or existing.data[0].get("status") != "Pending":
        return False

    client.table(BUDGET_TABLE).update({"status": status}).eq("id", request_id).execute()
    return True


def _sum_expenses_between(start_str: str, end_str: str) -> float:
    rows = [
        row
        for row in _fetch_expenses()
        if start_str <= (row.get("date") or "") <= end_str
    ]
    return _sum_actual(rows)


def get_current_month_spending() -> float:
    today = date.today()
    return _sum_expenses_between(today.replace(day=1).isoformat(), _today_str())


def get_current_week_spending() -> float:
    today = date.today()
    week_start = (today - timedelta(days=6)).isoformat()
    return _sum_expenses_between(week_start, _today_str())


def get_spending_by_category() -> dict[str, float]:
    init_db()
    return _category_totals(_fetch_expenses())


def get_modified_expense_records() -> list[dict[str, Any]]:
    init_db()
    rows = [row for row in _fetch_expenses() if int(row.get("is_modified") or 0) == 1]
    return _sort_by_date_desc(rows)


def get_category_weekly_baseline_dict() -> dict[str, float]:
    """Weekly average per category over the last 28 days (total / 4)."""
    rows_28d = get_filtered_expenses(days_offset=28)
    baseline = {cat: 0.0 for cat in EXPENSE_CATEGORIES}
    for cat in EXPENSE_CATEGORIES:
        total = sum(
            float(row.get("actual_price") or 0)
            for row in rows_28d
            if row.get("category") == cat
        )
        baseline[cat] = round(total / 4.0, 2)
    return baseline
