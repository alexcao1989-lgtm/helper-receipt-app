r"""
Sir & Helper Receipt System
- Supabase-backed expense capture and audit
- OCR recognition + receipt image cloud storage
"""

from __future__ import annotations

import base64
import importlib.util
import io
import json
import os
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pandas as pd
import plotly.express as px
import streamlit as st
from openai import OpenAI
from PIL import Image


def _load_project_database():
    db_path = Path(__file__).resolve().parent / "database.py"
    spec = importlib.util.spec_from_file_location("helper_receipt_database", db_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load database module from {db_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


database = _load_project_database()


def _secret(name: str, default: str = "") -> str:
    try:
        return str(st.secrets[name])
    except Exception:
        return os.environ.get(name, default)


OPENROUTER_API_KEY = _secret("OPENROUTER_API_KEY")
APP_PASSWORD = _secret("APP_PASSWORD")
EMPLOYER_PASSWORD = _secret("EMPLOYER_PASSWORD", "123456")
MODEL_NAME = "qwen/qwen-2.5-vl-72b-instruct"
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
STORAGE_BUCKET = "receipts"
CATEGORIES = ["Meat", "Seafood", "Vegetable", "Grocery"]
VALID_CATEGORIES = set(CATEGORIES)

SYSTEM_PROMPT = """You are a receipt OCR assistant.
Extract purchasable line items from this grocery receipt.
Return ONLY strict JSON:
{
  "items":[{"item_name":"...", "category":"Meat|Seafood|Vegetable|Grocery", "price": 12.5}]
}
"""


def init_session_state() -> None:
    defaults = {
        "items_df": None,
        "uploader_nonce": 0,
        "helper_authenticated": False,
        "employer_authenticated": False,
        "pending_employer_update": None,
    }
    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value


def clear_upload_session() -> None:
    st.session_state.items_df = None
    st.session_state.uploader_nonce += 1


def check_helper_password() -> bool:
    if not APP_PASSWORD:
        return True
    if st.session_state.helper_authenticated:
        return True
    with st.container(border=True):
        st.subheader("Helper Login")
        pwd = st.text_input("Password", type="password", key="helper_pwd")
        if st.button("Enter Helper Portal", key="helper_enter") and pwd == APP_PASSWORD:
            st.session_state.helper_authenticated = True
            st.rerun()
    return False


def check_employer_password() -> bool:
    if st.session_state.employer_authenticated:
        return True
    with st.container(border=True):
        st.subheader("Employer Login")
        pwd = st.text_input("Employer password", type="password", key="employer_pwd")
        if st.button("Enter Employer Dashboard", key="employer_enter") and pwd == EMPLOYER_PASSWORD:
            st.session_state.employer_authenticated = True
            st.rerun()
    return False


def image_to_base64_url(image: Image.Image) -> str:
    buf = io.BytesIO()
    image.save(buf, format="JPEG")
    return f"data:image/jpeg;base64,{base64.b64encode(buf.getvalue()).decode('utf-8')}"


def extract_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return json.loads(text)


def normalize_items(data: dict) -> list[dict]:
    items = data.get("items", [])
    if not isinstance(items, list):
        return []
    out = []
    for row in items:
        if not isinstance(row, dict):
            continue
        cat = str(row.get("category", "Grocery")).strip()
        if cat not in VALID_CATEGORIES:
            cat = "Grocery"
        try:
            price = float(row.get("price", 0))
        except Exception:
            price = 0.0
        out.append(
            {
                "item_name": str(row.get("item_name", "Unknown")).strip() or "Unknown",
                "category": cat,
                "price": round(price, 2),
            }
        )
    return out


def recognize_receipt(image: Image.Image) -> list[dict]:
    if not OPENROUTER_API_KEY:
        raise ValueError("OPENROUTER_API_KEY is missing.")
    client = OpenAI(api_key=OPENROUTER_API_KEY, base_url=OPENROUTER_BASE_URL)
    response = client.chat.completions.create(
        model=MODEL_NAME,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Extract all grocery items."},
                    {"type": "image_url", "image_url": {"url": image_to_base64_url(image.convert("RGB"))}},
                ],
            },
        ],
        temperature=0.1,
    )
    raw = response.choices[0].message.content or "{}"
    return normalize_items(extract_json(raw))


def upload_receipt_to_storage(uploaded_file) -> str:
    client = database.get_supabase_client()
    suffix = Path(uploaded_file.name).suffix.lower() or ".jpg"
    object_path = f"receipts/{date.today().isoformat()}/{uuid4().hex}{suffix}"
    client.storage.from_(STORAGE_BUCKET).upload(
        object_path,
        uploaded_file.getvalue(),
        {"content-type": uploaded_file.type or "image/jpeg", "upsert": "true"},
    )
    return client.storage.from_(STORAGE_BUCKET).get_public_url(object_path)


def recognize_all_receipts(uploaded_files: list) -> pd.DataFrame:
    rows: list[dict] = []
    for i, uf in enumerate(uploaded_files, start=1):
        with st.spinner(f"Processing receipt {i}/{len(uploaded_files)}..."):
            image_url = upload_receipt_to_storage(uf)
            image = Image.open(io.BytesIO(uf.getvalue()))
            for item in recognize_receipt(image):
                rows.append(
                    {
                        "Item Name": item["item_name"],
                        "Category": item["category"],
                        "AI Price": item["price"],
                        "Actual Price": item["price"],
                        "Image URL": image_url,
                    }
                )
    return pd.DataFrame(rows)


def dataframe_to_db_rows(df: pd.DataFrame) -> list[dict]:
    rows = []
    for _, r in df.iterrows():
        ai_price = round(float(r["AI Price"]), 2)
        actual_price = round(float(r["Actual Price"]), 2)
        category = str(r["Category"]).strip()
        if category not in VALID_CATEGORIES:
            category = "Grocery"
        rows.append(
            {
                "item_name": str(r["Item Name"]).strip() or "Unknown",
                "category": category,
                "ai_price": ai_price,
                "actual_price": actual_price,
                "is_modified": ai_price != actual_price,
                "image_url": str(r.get("Image URL", "")).strip(),
            }
        )
    return rows


def show_review_editor(df: pd.DataFrame) -> pd.DataFrame:
    with st.container(border=True):
        st.markdown("#### Review OCR Items Before Submit")
        return st.data_editor(
            df,
            hide_index=True,
            use_container_width=True,
            num_rows="dynamic",
            column_config={
                "Item Name": st.column_config.TextColumn("Item Name", required=True),
                "Category": st.column_config.SelectboxColumn("Category", options=CATEGORIES),
                "AI Price": st.column_config.NumberColumn("AI Price", format="HK$ %.2f", disabled=True),
                "Actual Price": st.column_config.NumberColumn("Actual Price", format="HK$ %.2f", min_value=0.0, step=0.1),
                "Image URL": st.column_config.TextColumn("Image URL", disabled=True),
            },
        )


def _plot_category_bar(summary: dict, title: str):
    df = pd.DataFrame(
        {"Category": CATEGORIES, "Amount": [summary["by_category"].get(cat, 0.0) for cat in CATEGORIES]}
    )
    fig = px.bar(
        df,
        x="Category",
        y="Amount",
        color="Amount",
        color_continuous_scale="Blues",
        title=title,
    )
    fig.update_layout(margin=dict(l=10, r=10, t=40, b=10), height=320)
    return fig


def render_expense_summary_tabs() -> None:
    with st.container(border=True):
        st.markdown("### Expense Summary & Analytics")
        tabs = st.tabs(["This Week", "This Month", "All Time"])
        summaries = [database.get_week_summary(), database.get_month_summary(), database.get_all_time_summary()]
        for tab, summary, label in zip(tabs, summaries, ["This Week", "This Month", "All Time"]):
            with tab:
                st.metric(f"{label} Total Spending", f"HK$ {summary['total_actual']:,.2f}")
                st.plotly_chart(_plot_category_bar(summary, f"{label} Category Spend"), use_container_width=True)


def _safe_date(value: str) -> datetime:
    try:
        return datetime.strptime(value, "%Y-%m-%d")
    except Exception:
        return datetime.min


def _partition_expenses_for_audit():
    all_rows = database.get_all_time_summary()["rows"]
    now = datetime.now()
    w7 = now - timedelta(days=7)
    w14 = now - timedelta(days=14)
    recent, mid, old = [], [], []
    for row in all_rows:
        d = _safe_date(str(row.get("date", "")))
        if d >= w7:
            recent.append(row)
        elif d >= w14:
            mid.append(row)
        else:
            old.append(row)
    return recent, mid, old


def _update_expense_from_audit(expense_id: int, final_price: float, final_name: str) -> None:
    client = database.get_supabase_client()
    client.table(database.EXPENSES_TABLE).update(
        {"actual_price": float(final_price), "item_name": final_name.strip()}
    ).eq("id", expense_id).execute()


@st.dialog("Confirm Final Input")
def employer_confirm_update_dialog() -> None:
    payload = st.session_state.get("pending_employer_update")
    if not payload:
        st.info("Nothing to confirm.")
        return

    st.markdown(
        f"Please confirm updating record **#{payload['id']}**:\n\n"
        f"- Final Item: **{payload['name']}**\n"
        f"- Final Price: **HK$ {payload['price']:.2f}**"
    )
    c1, c2 = st.columns(2)
    with c1:
        if st.button("✅ Confirm Update", use_container_width=True):
            _update_expense_from_audit(payload["id"], payload["price"], payload["name"])
            database.log_audit_event(
                actor="Employer",
                action="UPDATE_EXPENSE_FINAL_INPUT",
                target_type="expense",
                target_id=payload["id"],
                details=f"name={payload['name']}, price={payload['price']:.2f}",
            )
            st.session_state.pending_employer_update = None
            st.success(f"Updated #{payload['id']}")
            st.rerun()
    with c2:
        if st.button("Cancel", use_container_width=True):
            st.session_state.pending_employer_update = None
            st.rerun()


def render_audit_rows(rows: list[dict], portal: str) -> None:
    if not rows:
        st.info("No records in this range.")
        return

    for row in rows:
        with st.container(border=True):
            rid = int(row.get("id", 0))
            image_url = row.get("image_url", "")
            c1, c2, c3 = st.columns([2, 3, 2])
            with c1:
                if image_url:
                    st.image(image_url, caption="Receipt original", width=200)
                else:
                    st.caption("No receipt image")
            with c2:
                st.markdown(f"**Date:** {row.get('date','')}")
                st.markdown(f"**Item:** {row.get('item_name','')}")
                st.markdown(f"**Category:** {row.get('category','')}")
                st.markdown(f"AI Price: HK$ {float(row.get('ai_price') or 0):,.2f}")
                st.markdown(f"Actual Price: HK$ {float(row.get('actual_price') or 0):,.2f}")
            with c3:
                if portal == "Employer":
                    final_name = st.text_input("Final Item", value=str(row.get("item_name", "")), key=f"n_{rid}")
                    final_price = st.number_input("Final Input (HKD)", min_value=0.0, value=float(row.get("actual_price") or 0), step=0.1, key=f"p_{rid}")
                    if st.button("✅ Review & Confirm", key=f"ok_{rid}", use_container_width=True):
                        st.session_state.pending_employer_update = {
                            "id": rid,
                            "name": final_name.strip(),
                            "price": float(final_price),
                        }
                        employer_confirm_update_dialog()
                else:
                    if st.button("🗑️ Delete", key=f"del_{rid}", type="secondary", use_container_width=True):
                        if database.delete_expense(rid):
                            database.log_audit_event(
                                actor="Helper",
                                action="DELETE_EXPENSE",
                                target_type="expense",
                                target_id=rid,
                                details=f"item={row.get('item_name','')}",
                            )
                            st.success(f"Deleted #{rid}")
                            st.rerun()
                        st.error("Delete failed.")


def render_receipt_audit_center(portal: str) -> None:
    with st.container(border=True):
        st.markdown("### Receipt Audit Center")
        recent, mid, old = _partition_expenses_for_audit()
        st.markdown("#### Past 7 Days")
        render_audit_rows(recent, portal)
        with st.expander("Show Past 8-14 Days"):
            render_audit_rows(mid, portal)
        with st.expander("Show Older Expenses"):
            render_audit_rows(old, portal)


def render_pending_financial_requests() -> None:
    with st.container(border=True):
        st.markdown("### Financial Requests Dashboard")
        pending = database.get_pending_budget_requests()
        if not pending:
            st.info("No pending requests.")
            return
        st.dataframe(pd.DataFrame(pending), use_container_width=True, hide_index=True)
        for req in pending:
            c1, c2 = st.columns(2)
            with c1:
                if st.button("Approve", key=f"approve_{req['id']}", type="primary", use_container_width=True):
                    database.set_budget_request_status(req["id"], "Approved")
                    database.log_audit_event(
                        actor="Employer",
                        action="APPROVE_BUDGET_REQUEST",
                        target_type="budget_request",
                        target_id=req["id"],
                        details=f"type={req.get('type','')}, amount={req.get('amount',0)}",
                    )
                    st.rerun()
            with c2:
                if st.button("Reject", key=f"reject_{req['id']}", use_container_width=True):
                    database.set_budget_request_status(req["id"], "Rejected")
                    database.log_audit_event(
                        actor="Employer",
                        action="REJECT_BUDGET_REQUEST",
                        target_type="budget_request",
                        target_id=req["id"],
                        details=f"type={req.get('type','')}, amount={req.get('amount',0)}",
                    )
                    st.rerun()


def render_monthly_category_breakdown() -> None:
    with st.container(border=True):
        st.markdown("### 1) Monthly Category Breakdown")
        rows = database.get_all_time_summary()["rows"]
        if not rows:
            st.info("No expense data yet.")
            return
        months = sorted({str(r.get("date", ""))[:7] for r in rows if str(r.get("date", ""))}, reverse=True)
        selected = st.selectbox("Select month", months, index=0)
        month_rows = [r for r in rows if str(r.get("date", "")).startswith(selected)]
        totals = {c: 0.0 for c in CATEGORIES}
        for r in month_rows:
            cat = str(r.get("category", ""))
            if cat in totals:
                totals[cat] += float(r.get("actual_price") or 0)
        pie_df = pd.DataFrame({"Category": list(totals.keys()), "Amount": list(totals.values())})
        pie_df = pie_df[pie_df["Amount"] > 0]
        if pie_df.empty:
            st.info("No spend in selected month.")
            return
        fig = px.pie(pie_df, values="Amount", names="Category", hole=0.45, title=f"{selected} Category Share")
        fig.update_traces(textposition="inside", textinfo="percent+label")
        st.plotly_chart(fig, use_container_width=True)


def render_expenditure_trend_analysis() -> None:
    with st.container(border=True):
        st.markdown("### 2) Expenditure Trend Analysis")
        rows = database.get_all_time_summary()["rows"]
        if not rows:
            st.info("No expense data yet.")
            return
        df = pd.DataFrame(rows)
        df["date"] = pd.to_datetime(df["date"], errors="coerce")
        df["actual_price"] = pd.to_numeric(df["actual_price"], errors="coerce").fillna(0)
        df = df.dropna(subset=["date"])
        weekly = df.groupby(pd.Grouper(key="date", freq="W-MON"))["actual_price"].sum().reset_index()
        monthly = df.groupby(pd.Grouper(key="date", freq="MS"))["actual_price"].sum().reset_index()
        mode = st.radio("Trend granularity", ["Weekly", "Monthly"], horizontal=True, key="trend_mode")
        trend_df = weekly if mode == "Weekly" else monthly
        fig = px.line(trend_df, x="date", y="actual_price", markers=True, title=f"{mode} Spending Trend")
        fig.update_traces(line=dict(width=3, color="#2563EB"))
        fig.update_layout(yaxis_title="HKD")
        st.plotly_chart(fig, use_container_width=True)


def render_ai_spending_anomaly_detection() -> None:
    with st.container(border=True):
        st.markdown("### 3) AI Spending Anomaly Detection")
        alerts, this_week = database.get_category_spending_anomalies()
        baseline = database.get_category_weekly_baseline_dict()
        if alerts:
            for msg in alerts:
                st.error(f"🚨 {msg}")
        else:
            st.success("✅ All categories are within normal range.")
        df = pd.DataFrame(
            {
                "Category": CATEGORIES,
                "This Week": [this_week.get(c, 0.0) for c in CATEGORIES],
                "Baseline/Week": [baseline.get(c, 0.0) for c in CATEGORIES],
            }
        )
        fig = px.bar(df, x="Category", y=["This Week", "Baseline/Week"], barmode="group", title="This Week vs Baseline")
        st.plotly_chart(fig, use_container_width=True)


def render_cost_saving_insights() -> None:
    with st.container(border=True):
        st.markdown("### 4) Cost-Saving Insights (Market Price Gap Summary)")
        rows = database.get_all_time_summary()["rows"]
        if not rows:
            st.info("No data yet.")
            return
        df = pd.DataFrame(rows)
        df["ai_price"] = pd.to_numeric(df["ai_price"], errors="coerce").fillna(0)
        df["actual_price"] = pd.to_numeric(df["actual_price"], errors="coerce").fillna(0)
        df["gap"] = df["actual_price"] - df["ai_price"]
        summary = df["gap"].agg(["sum", "mean"]).to_dict()
        c1, c2 = st.columns(2)
        c1.metric("Total Market Price Gap", f"HK$ {summary['sum']:,.2f}")
        c2.metric("Avg Gap / Item", f"HK$ {summary['mean']:,.2f}")
        worst = (
            df.groupby("item_name", as_index=False)["gap"]
            .sum()
            .sort_values("gap", ascending=False)
            .head(10)
        )
        if not worst.empty:
            fig = px.bar(worst, x="item_name", y="gap", color="gap", color_continuous_scale="Reds", title="Top Overpriced Items")
            fig.update_layout(xaxis_title="Item", yaxis_title="Actual - AI (HKD)")
            st.plotly_chart(fig, use_container_width=True)
        st.dataframe(
            df.sort_values("gap", ascending=False)[["date", "item_name", "category", "ai_price", "actual_price", "gap"]].head(20),
            use_container_width=True,
            hide_index=True,
        )


def render_helper_scanner_tab() -> None:
    with st.container(border=True):
        st.markdown("### Expense Scanner & Uploader")
        uploaded_files = st.file_uploader(
            "Upload one or more receipt photos",
            type=["jpg", "jpeg", "png", "webp"],
            accept_multiple_files=True,
            key=f"receipt_uploader_{st.session_state.uploader_nonce}",
        )
        if uploaded_files and st.button("Recognize All Receipts", type="primary", use_container_width=True):
            try:
                st.session_state.items_df = recognize_all_receipts(uploaded_files)
                st.success("OCR completed. Please review before submit.")
            except Exception as exc:
                st.error(f"OCR/Upload failed: {exc}")
        if st.session_state.items_df is not None and not st.session_state.items_df.empty:
            edited = show_review_editor(st.session_state.items_df.copy())
            st.session_state.items_df = edited
            if st.button("Confirm & Submit to Sir", type="primary", use_container_width=True):
                rows = dataframe_to_db_rows(edited)
                saved = database.save_expenses(rows)
                total_actual = sum(float(r["actual_price"]) for r in rows)
                database.log_audit_event(
                    actor="Helper",
                    action="SUBMIT_EXPENSE_BATCH",
                    target_type="expense_batch",
                    target_id=None,
                    details=f"count={saved}, total_actual={total_actual:.2f}",
                )
                clear_upload_session()
                st.success(f"Saved {saved} line item(s).")
                st.rerun()


def render_helper_wallet_tab() -> None:
    with st.container(border=True):
        st.markdown("### Smart Wallet & Budget Center")
        remaining = database.get_remaining_budget()
        pending = database.get_pending_budget_total()
        c1, c2 = st.columns(2)
        c1.metric("Remaining Budget", f"HK$ {remaining:,.2f}")
        c2.metric("Pending Approval Amount", f"HK$ {pending:,.2f}")
        st.markdown("---")
        topup = st.number_input("Cash top-up received (HKD)", min_value=0.01, step=50.0, key="wallet_topup")
        if st.button("Submit Top-up Request", use_container_width=True):
            rid = database.create_budget_request(topup, "Top-up")
            database.log_audit_event(
                actor="Helper",
                action="CREATE_TOPUP_REQUEST",
                target_type="budget_request",
                target_id=rid,
                details=f"amount={topup:.2f}",
            )
            st.success(f"Top-up request #{rid} submitted.")
            st.rerun()
        adjust = st.number_input("Correction amount (+/- HKD)", step=50.0, key="wallet_adjust")
        reason = st.text_area("Correction reason", key="wallet_reason")
        if st.button("Submit Correction Request", use_container_width=True):
            if not reason.strip():
                st.error("Reason is required.")
            elif adjust == 0:
                st.error("Amount cannot be zero.")
            else:
                rid = database.create_budget_request(adjust, "Adjustment", reason.strip())
                database.log_audit_event(
                    actor="Helper",
                    action="CREATE_ADJUSTMENT_REQUEST",
                    target_type="budget_request",
                    target_id=rid,
                    details=f"amount={adjust:.2f}, reason={reason.strip()}",
                )
                st.success(f"Correction request #{rid} submitted.")
                st.rerun()

    with st.container(border=True):
        st.markdown("#### Past 7 Days: Your Submitted Expenses")
        rows = database.get_filtered_expenses(days_offset=7)
        if rows:
            table = pd.DataFrame(rows)[["id", "date", "item_name", "category", "ai_price", "actual_price"]]
            st.dataframe(table, use_container_width=True, hide_index=True)
        else:
            st.info("No expenses in the last 7 days.")


def render_helper_portal() -> None:
    if not check_helper_password():
        return
    st.title("Helper Portal")
    render_expense_summary_tabs()
    tab1, tab2 = st.tabs(["Expense Scanner & Uploader", "Smart Wallet & Budget Center"])
    with tab1:
        render_helper_scanner_tab()
    with tab2:
        render_helper_wallet_tab()
    render_receipt_audit_center("Helper")


def render_employer_portal() -> None:
    if not check_employer_password():
        return
    st.title("Employer Dashboard")
    render_pending_financial_requests()
    render_expense_summary_tabs()
    render_monthly_category_breakdown()
    render_expenditure_trend_analysis()
    render_ai_spending_anomaly_detection()
    render_cost_saving_insights()
    render_receipt_audit_center("Employer")
    with st.container(border=True):
        st.markdown("### Operation Audit Log")
        logs = database.get_recent_audit_logs(limit=50)
        if logs:
            st.dataframe(pd.DataFrame(logs), use_container_width=True, hide_index=True)
        else:
            st.caption("No audit logs yet (or audit_logs table not created).")


def main() -> None:
    st.set_page_config(page_title="Helper Receipt App", page_icon="🧾", layout="wide")
    init_session_state()
    database.init_db()

    with st.sidebar:
        st.header("Mode")
        portal = st.segmented_control(
            "Portal",
            options=["Helper", "Employer"],
            default="Helper",
            label_visibility="collapsed",
        )

    if portal == "Employer":
        render_employer_portal()
    else:
        render_helper_portal()


if __name__ == "__main__":
    main()
