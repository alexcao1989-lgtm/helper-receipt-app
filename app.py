r"""
Helper Receipt App
- Helper uploads receipt photos, OCR extracts line items, then submits.
- Sir reviews budget requests and audits records with receipt images.
"""

from __future__ import annotations

import base64
import io
import json
import os
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pandas as pd
import streamlit as st
from openai import OpenAI
from PIL import Image

import importlib.util


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

CATEGORIES = ["Meat", "Seafood", "Vegetable", "Grocery"]
VALID_CATEGORIES = set(CATEGORIES)
STORAGE_BUCKET = "receipts"

SYSTEM_PROMPT = """You are a grocery receipt OCR assistant.
Extract purchasable line items from the receipt image.
Return ONLY strict JSON:
{
  "items": [
    {"item_name":"...", "category":"Meat|Seafood|Vegetable|Grocery", "price": 12.5}
  ]
}
"""


def init_session_state() -> None:
    defaults = {
        "items_df": None,
        "uploader_nonce": 0,
        "helper_authenticated": False,
        "employer_authenticated": False,
        "last_save_message": None,
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v


def clear_upload_session() -> None:
    st.session_state.items_df = None
    st.session_state.uploader_nonce += 1


def check_helper_password() -> bool:
    if not APP_PASSWORD:
        st.session_state.helper_authenticated = True
        return True
    if st.session_state.helper_authenticated:
        return True
    st.title("Helper Portal")
    pwd = st.text_input("Password", type="password", key="helper_pwd")
    if st.button("Enter", key="helper_enter") and pwd == APP_PASSWORD:
        st.session_state.helper_authenticated = True
        st.rerun()
    return False


def check_employer_password() -> bool:
    if st.session_state.employer_authenticated:
        return True
    st.title("Employer Dashboard")
    pwd = st.text_input("Employer password", type="password", key="employer_pwd")
    if st.button("Enter", key="employer_enter") and pwd == EMPLOYER_PASSWORD:
        st.session_state.employer_authenticated = True
        st.rerun()
    return False


def image_to_base64_url(image: Image.Image, fmt: str = "JPEG") -> str:
    buf = io.BytesIO()
    image.save(buf, format=fmt)
    b64 = base64.b64encode(buf.getvalue()).decode("utf-8")
    return f"data:image/jpeg;base64,{b64}"


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
    parsed = []
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
        parsed.append(
            {
                "item_name": str(row.get("item_name", "Unknown")).strip() or "Unknown",
                "category": cat,
                "price": round(price, 2),
            }
        )
    return parsed


def recognize_receipt(image: Image.Image) -> list[dict]:
    if not OPENROUTER_API_KEY:
        raise ValueError("OPENROUTER_API_KEY is missing.")
    client = OpenAI(api_key=OPENROUTER_API_KEY, base_url=OPENROUTER_BASE_URL)
    image_url = image_to_base64_url(image.convert("RGB"))
    response = client.chat.completions.create(
        model=MODEL_NAME,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Extract all grocery items."},
                    {"type": "image_url", "image_url": {"url": image_url}},
                ],
            },
        ],
        temperature=0.1,
    )
    raw = response.choices[0].message.content or "{}"
    return normalize_items(extract_json(raw))


def upload_receipt_to_storage(uploaded_file) -> str:
    """
    Upload receipt image to Supabase Storage bucket `receipts` and return public URL.
    """
    client = database.get_supabase_client()
    suffix = Path(uploaded_file.name).suffix.lower() or ".jpg"
    object_path = f"receipts/{date.today().isoformat()}/{uuid4().hex}{suffix}"
    content = uploaded_file.getvalue()
    client.storage.from_(STORAGE_BUCKET).upload(
        object_path,
        content,
        {"content-type": uploaded_file.type or "image/jpeg", "upsert": "true"},
    )
    return client.storage.from_(STORAGE_BUCKET).get_public_url(object_path)


def recognize_all_receipts(uploaded_files: list) -> pd.DataFrame:
    rows: list[dict] = []
    total = len(uploaded_files)
    for idx, uf in enumerate(uploaded_files, start=1):
        with st.spinner(f"Processing receipt {idx}/{total}..."):
            image_url = upload_receipt_to_storage(uf)
            image = Image.open(io.BytesIO(uf.getvalue()))
            items = recognize_receipt(image)
            for item in items:
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
    data = []
    for _, r in df.iterrows():
        ai_price = round(float(r["AI Price"]), 2)
        actual_price = round(float(r["Actual Price"]), 2)
        category = str(r["Category"]).strip()
        if category not in VALID_CATEGORIES:
            category = "Grocery"
        data.append(
            {
                "item_name": str(r["Item Name"]).strip() or "Unknown",
                "category": category,
                "ai_price": ai_price,
                "actual_price": actual_price,
                "is_modified": ai_price != actual_price,
                "image_url": str(r.get("Image URL", "")).strip(),
            }
        )
    return data


def render_budget_header() -> None:
    remaining = database.get_remaining_budget()
    pending = database.get_pending_budget_total()
    st.markdown(f"### Remaining Budget: **HK$ {remaining:,.2f}**")
    if pending:
        st.caption(f"Pending approval amount: HK$ {pending:,.2f}")


def render_expense_summary_tabs() -> None:
    st.subheader("Expense Summary & Analytics")
    tabs = st.tabs(["This Week", "This Month", "All Time"])
    summary_funcs = [database.get_week_summary, database.get_month_summary, database.get_all_time_summary]

    for tab, fn in zip(tabs, summary_funcs):
        with tab:
            summary = fn()
            st.metric("Total Actual Spending", f"HK$ {summary['total_actual']:,.2f}")
            chart_df = pd.DataFrame(
                {"Amount": [summary["by_category"].get(cat, 0.0) for cat in CATEGORIES]},
                index=CATEGORIES,
            )
            if chart_df["Amount"].sum() > 0:
                st.bar_chart(chart_df, height=260)
            else:
                st.info("No data in this time range.")


def _safe_date(value: str) -> datetime:
    try:
        return datetime.strptime(value, "%Y-%m-%d")
    except Exception:
        return datetime.min


def _partition_expenses_for_audit() -> tuple[list[dict], list[dict], list[dict]]:
    all_rows = database.get_all_time_summary()["rows"]
    now = datetime.now()
    week_cut = now - timedelta(days=7)
    two_week_cut = now - timedelta(days=14)

    recent, prev_week, older = [], [], []
    for row in all_rows:
        d = _safe_date(str(row.get("date", "")))
        if d >= week_cut:
            recent.append(row)
        elif d >= two_week_cut:
            prev_week.append(row)
        else:
            older.append(row)
    return recent, prev_week, older


def _update_expense_from_audit(expense_id: int, final_price: float, final_name: str) -> None:
    client = database.get_supabase_client()
    client.table(database.EXPENSES_TABLE).update(
        {"actual_price": float(final_price), "item_name": final_name.strip()}
    ).eq("id", expense_id).execute()


def render_audit_rows(rows: list[dict], portal: str) -> None:
    if not rows:
        st.info("No records in this range.")
        return

    for row in rows:
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
                final_name = st.text_input(
                    "Final Item",
                    value=str(row.get("item_name", "")),
                    key=f"final_name_{rid}",
                )
                final_price = st.number_input(
                    "Final Input (HKD)",
                    min_value=0.0,
                    value=float(row.get("actual_price") or 0),
                    step=0.1,
                    key=f"final_price_{rid}",
                )
                if st.button("✅ Confirm Final Input", key=f"confirm_final_{rid}", use_container_width=True):
                    _update_expense_from_audit(rid, final_price, final_name)
                    st.success(f"Updated record #{rid}")
                    st.rerun()
            else:
                if st.button("🗑️ Delete", key=f"delete_helper_{rid}", type="secondary", use_container_width=True):
                    if database.delete_expense(rid):
                        st.success(f"Deleted record #{rid}")
                        st.rerun()
                    st.error("Delete failed.")
        st.markdown("---")


def render_receipt_audit_center(portal: str) -> None:
    st.subheader("Receipt Audit Center")
    recent, prev_week, older = _partition_expenses_for_audit()
    st.markdown("#### Past 7 Days")
    render_audit_rows(recent, portal)
    with st.expander("Show Past 8-14 Days"):
        render_audit_rows(prev_week, portal)
    with st.expander("Show Older Expenses"):
        render_audit_rows(older, portal)


def render_manage_budget_expander() -> None:
    with st.expander("Manage Budget / Correct Error"):
        topup = st.number_input("Cash top-up received (HKD)", min_value=0.01, step=50.0)
        if st.button("Submit Top-up Request", use_container_width=True):
            rid = database.create_budget_request(topup, "Top-up")
            st.success(f"Top-up request #{rid} submitted.")
            st.rerun()

        st.divider()
        adjust = st.number_input("Correction amount (+/- HKD)", step=50.0, key="adj_amount")
        reason = st.text_area("Reason", key="adj_reason")
        if st.button("Submit Correction Request", use_container_width=True):
            if not reason.strip():
                st.error("Reason is required.")
            elif adjust == 0:
                st.error("Amount cannot be zero.")
            else:
                rid = database.create_budget_request(adjust, "Adjustment", reason.strip())
                st.success(f"Correction request #{rid} submitted.")
                st.rerun()


def show_review_editor(df: pd.DataFrame) -> pd.DataFrame:
    edited = st.data_editor(
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
    return edited


def render_helper_portal() -> None:
    if not check_helper_password():
        return
    st.title("Helper Portal")
    render_budget_header()
    render_expense_summary_tabs()
    render_manage_budget_expander()

    st.markdown("### Upload Receipts")
    uploaded_files = st.file_uploader(
        "Upload one or more receipt photos",
        type=["jpg", "jpeg", "png", "webp"],
        accept_multiple_files=True,
        key=f"receipt_uploader_{st.session_state.uploader_nonce}",
    )

    if uploaded_files and st.button("Recognize all receipts", type="primary", use_container_width=True):
        try:
            st.session_state.items_df = recognize_all_receipts(uploaded_files)
            st.success("OCR completed. Please review and submit.")
            st.rerun()
        except Exception as exc:
            st.error(f"OCR/Upload failed: {exc}")

    if st.session_state.items_df is not None and not st.session_state.items_df.empty:
        st.markdown("### Review Before Submit")
        edited = show_review_editor(st.session_state.items_df.copy())
        st.session_state.items_df = edited
        if st.button("Confirm & Submit to Sir", type="primary", use_container_width=True):
            rows = dataframe_to_db_rows(edited)
            saved = database.save_expenses(rows)
            clear_upload_session()
            st.success(f"Saved {saved} line item(s).")
            st.rerun()

    st.markdown("---")
    render_receipt_audit_center("Helper")


def render_pending_financial_requests() -> None:
    st.subheader("Pending Financial Requests")
    pending = database.get_pending_budget_requests()
    if not pending:
        st.info("No pending requests.")
        return

    st.dataframe(pd.DataFrame(pending), use_container_width=True, hide_index=True)
    for req in pending:
        rid = req["id"]
        c1, c2 = st.columns(2)
        with c1:
            if st.button("Approve", key=f"approve_{rid}", type="primary", use_container_width=True):
                database.set_budget_request_status(rid, "Approved")
                st.rerun()
        with c2:
            if st.button("Reject", key=f"reject_{rid}", use_container_width=True):
                database.set_budget_request_status(rid, "Rejected")
                st.rerun()


def render_employer_portal() -> None:
    if not check_employer_password():
        return
    st.title("Employer Dashboard")

    alerts, _ = database.get_category_spending_anomalies()
    for msg in alerts:
        st.error(f"⚠️ {msg}")
    if not alerts:
        st.success("✅ All category spendings are within normal ranges.")

    render_pending_financial_requests()
    render_expense_summary_tabs()
    render_receipt_audit_center("Employer")


def main() -> None:
    st.set_page_config(page_title="Helper Receipt App", page_icon="🧾", layout="wide")
    init_session_state()
    database.init_db()

    with st.sidebar:
        st.header("Portal")
        portal = st.radio("Choose portal", ["Helper Portal", "Employer Portal"], label_visibility="collapsed")

    if portal == "Employer Portal":
        render_employer_portal()
    else:
        render_helper_portal()


if __name__ == "__main__":
    main()
