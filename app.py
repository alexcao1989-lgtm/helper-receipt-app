r"""
Grocery receipt upload app — helper uploads photos, Sir approves budget requests.

Run (Windows PowerShell):
  cd "c:\03. Agent APP\01. Helper Receipt APP"
  .\venv\Scripts\Activate.ps1
  $env:OPENROUTER_API_KEY="sk-or-v1-your-key"
  py -m streamlit run app.py
"""

from __future__ import annotations

import base64
import io
import json
import os
import re

import matplotlib.pyplot as plt
import pandas as pd
import streamlit as st
from openai import OpenAI
from PIL import Image

import importlib.util
from pathlib import Path


def _load_project_database():
    """Always load database.py from this app folder (avoid wrong 'database' module)."""
    db_path = Path(__file__).resolve().parent / "database.py"
    spec = importlib.util.spec_from_file_location("helper_receipt_database", db_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load database module from {db_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


database = _load_project_database()


def _secret(name: str, default: str = "") -> str:
    """Read from .streamlit/secrets.toml (local) or Streamlit Cloud Secrets."""
    try:
        return str(st.secrets[name])
    except Exception:
        return os.environ.get(name, default)


# ========== Secrets — set in .streamlit/secrets.toml or Streamlit Cloud ==========
OPENROUTER_API_KEY = _secret("OPENROUTER_API_KEY")
APP_PASSWORD = _secret("APP_PASSWORD")
EMPLOYER_PASSWORD = _secret("EMPLOYER_PASSWORD", "123456")

MODEL_NAME = "qwen/qwen-2.5-vl-72b-instruct"
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

CATEGORIES = ["Meat", "Seafood", "Vegetable", "Grocery"]
VALID_CATEGORIES = set(CATEGORIES)

EDITOR_COLUMNS = ["Item Name", "Category", "AI Price", "Actual Price"]

LOW_BALANCE_THRESHOLD_HKD = 200.0
ANOMALY_SPIKE_THRESHOLD = 0.30
AUDIT_VARIANCE_ABSOLUTE_HKD = 50.0
AUDIT_VARIANCE_PERCENT = 0.20

SYSTEM_PROMPT = """You are a grocery receipt OCR assistant for Hong Kong supermarkets and wet markets.
Read the receipt image and extract every purchasable line item.

Reply with ONLY valid English JSON, no markdown, no explanation. Use this exact schema:
{
  "items": [
    {
      "item_name": "product name in English or romanized form",
      "category": "one of: Meat, Seafood, Vegetable, Grocery",
      "price": 12.5
    }
  ]
}

Rules:
- category MUST be exactly one of: Meat, Seafood, Vegetable, Grocery
- price is a number (HKD), no currency symbol
- Skip discounts, subtotals, payment lines, and non-product rows
- If the receipt is unreadable, return {"items": []}
"""


def init_session_state() -> None:
    defaults = {
        "items_df": None,
        "uploader_nonce": 0,
        "show_insufficient_budget_dialog": False,
        "last_save_message": None,
        "helper_authenticated": False,
        "employer_authenticated": False,
        "budget_request_message": None,
    }
    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value


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
    if st.button("Enter", key="helper_enter"):
        if pwd == APP_PASSWORD:
            st.session_state.helper_authenticated = True
            st.rerun()
        else:
            st.error("Wrong password")
    return False


def check_employer_password() -> bool:
    if st.session_state.employer_authenticated:
        return True
    st.title("Employer Dashboard")
    pwd = st.text_input("Employer password", type="password", key="employer_pwd")
    if st.button("Enter", key="employer_enter"):
        if pwd == EMPLOYER_PASSWORD:
            st.session_state.employer_authenticated = True
            st.rerun()
        else:
            st.error("Wrong password")
    return False


def image_to_base64_url(image: Image.Image, fmt: str = "JPEG") -> str:
    buf = io.BytesIO()
    image.save(buf, format=fmt)
    b64 = base64.b64encode(buf.getvalue()).decode("utf-8")
    mime = "image/jpeg" if fmt.upper() == "JPEG" else f"image/{fmt.lower()}"
    return f"data:{mime};base64,{b64}"


def extract_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return json.loads(text)


def normalize_items(data: dict) -> list[dict]:
    items = data.get("items", [])
    if not isinstance(items, list):
        items = [data] if "item_name" in data else []
    result = []
    for row in items:
        if not isinstance(row, dict):
            continue
        cat = row.get("category", "Grocery")
        if cat not in VALID_CATEGORIES:
            cat = "Grocery"
        try:
            price = float(row.get("price", 0))
        except (TypeError, ValueError):
            price = 0.0
        result.append(
            {
                "item_name": str(row.get("item_name", "Unknown")).strip(),
                "category": cat,
                "price": round(price, 2),
            }
        )
    return result


def recognize_receipt(image: Image.Image) -> list[dict]:
    if not OPENROUTER_API_KEY:
        raise ValueError(
            "OPENROUTER_API_KEY is not set. "
            'Run in terminal: $env:OPENROUTER_API_KEY="your-key"'
        )

    client = OpenAI(api_key=OPENROUTER_API_KEY, base_url=OPENROUTER_BASE_URL)
    image_url = image_to_base64_url(image.convert("RGB"))

    response = client.chat.completions.create(
        model=MODEL_NAME,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": "Extract all grocery items from this receipt image.",
                    },
                    {"type": "image_url", "image_url": {"url": image_url}},
                ],
            },
        ],
        temperature=0.1,
    )

    raw = response.choices[0].message.content or "{}"
    data = extract_json(raw)
    return normalize_items(data)


def items_to_dataframe(items: list[dict]) -> pd.DataFrame:
    rows = [
        {
            "Item Name": item["item_name"],
            "Category": item["category"],
            "AI Price": item["price"],
            "Actual Price": item["price"],
        }
        for item in items
    ]
    return pd.DataFrame(rows, columns=EDITOR_COLUMNS)


def recognize_all_receipts(uploaded_files: list) -> pd.DataFrame:
    merged: list[dict] = []
    total = len(uploaded_files)

    for index, uploaded in enumerate(uploaded_files, start=1):
        with st.spinner(f"Reading receipt {index} of {total}…"):
            image = Image.open(uploaded)
            items = recognize_receipt(image)
            merged.extend(items)

    return items_to_dataframe(merged)


def dataframe_to_db_rows(df: pd.DataFrame) -> list[dict]:
    rows = []
    for _, record in df.iterrows():
        ai_price = round(float(record["AI Price"]), 2)
        actual_price = round(float(record["Actual Price"]), 2)
        category = str(record["Category"]).strip()
        if category not in VALID_CATEGORIES:
            category = "Grocery"
        rows.append(
            {
                "item_name": str(record["Item Name"]).strip() or "Unknown",
                "category": category,
                "ai_price": ai_price,
                "actual_price": actual_price,
                "is_modified": ai_price != actual_price,
            }
        )
    return rows


@st.dialog("Budget Alert")
def insufficient_budget_dialog() -> None:
    st.error("❌ Insufficient Budget. Please contact Sir.")
    if st.button("OK", use_container_width=True, key="insufficient_budget_ok"):
        st.session_state.show_insufficient_budget_dialog = False
        st.rerun()


def render_budget_header() -> float:
    """Remaining = Approved budget_requests - all actual_price spent."""
    remaining = database.get_remaining_budget()
    pending_total = database.get_pending_budget_total()

    color = "#16a34a" if remaining >= LOW_BALANCE_THRESHOLD_HKD else "#ca8a04"
    if remaining < 0:
        color = "#dc2626"

    st.markdown(
        f"""
        <div style="
            background: linear-gradient(135deg, #0f172a 0%, #1e3a5f 100%);
            border-radius: 12px;
            padding: 1rem 1.25rem;
            margin-bottom: 0.75rem;
            text-align: center;
            border: 2px solid {color};
        ">
            <div style="color: #ffffff; font-size: 1.6rem; font-weight: 800;">
                Remaining Budget: ${remaining:,.2f} HKD
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    if pending_total != 0:
        st.caption(
            f"Pending approval (not counted yet): ${pending_total:,.2f} HKD — "
            "Sir must approve before balance updates."
        )

    if remaining < LOW_BALANCE_THRESHOLD_HKD:
        st.warning("⚠️ Low Balance: Please remind Sir to top up soon.")

    return remaining


def render_manage_budget_expander() -> None:
    with st.expander("Manage Budget / Correct Error"):
        st.caption(
            "All requests need Sir's approval before your balance changes. "
            "You cannot edit the balance directly."
        )

        st.markdown("**A. Request cash top-up (offline cash from Sir)**")
        topup_amount = st.number_input(
            "Cash amount received (HKD)",
            min_value=0.01,
            step=50.0,
            format="%.2f",
            key="topup_amount_input",
        )
        if st.button(
            "Submit Top-up Request",
            use_container_width=True,
            key="submit_topup_request",
        ):
            try:
                request_id = database.create_budget_request(
                    amount=topup_amount,
                    request_type="Top-up",
                )
            except Exception as exc:
                st.error(f"Could not submit request: {exc}")
            else:
                st.session_state.budget_request_message = (
                    f"Top-up request #{request_id} submitted. "
                    "Waiting for Sir to approve."
                )
                st.rerun()

        st.divider()
        st.markdown("**B. Correct a previous mistake (over/under entry)**")
        st.info(
            "If you entered too much, use a **negative** amount (e.g. -500). "
            "If you entered too little, use a **positive** amount (e.g. 500)."
        )
        adjust_amount = st.number_input(
            "Correction amount (HKD, + or -)",
            step=50.0,
            format="%.2f",
            key="adjust_amount_input",
        )
        adjust_reason = st.text_area(
            "Reason (required)",
            placeholder="Explain what was wrong and which receipt…",
            key="adjust_reason_input",
        )
        if st.button(
            "Submit Correction Request",
            use_container_width=True,
            key="submit_correction_request",
        ):
            if not adjust_reason.strip():
                st.error("Please enter a reason for the correction.")
            elif adjust_amount == 0:
                st.error("Correction amount cannot be zero.")
            else:
                try:
                    request_id = database.create_budget_request(
                        amount=adjust_amount,
                        request_type="Adjustment",
                        reason=adjust_reason.strip(),
                    )
                except Exception as exc:
                    st.error(f"Could not submit request: {exc}")
                else:
                    st.session_state.budget_request_message = (
                        f"Correction request #{request_id} submitted. "
                        "Waiting for Sir to approve."
                    )
                    st.rerun()


def render_confirm_button_css() -> None:
    st.markdown(
        """
        <style>
        button[data-testid="stBaseButton-confirm_submit"] {
            background-color: #22c55e !important;
            color: #ffffff !important;
            border: 1px solid #16a34a !important;
            font-size: 1.2rem !important;
            font-weight: 700 !important;
            padding: 0.85rem 1rem !important;
            min-height: 3.25rem !important;
        }
        button[data-testid="stBaseButton-confirm_submit"]:hover {
            background-color: #16a34a !important;
            border-color: #15803d !important;
            color: #ffffff !important;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def show_review_editor(df: pd.DataFrame) -> pd.DataFrame:
    st.subheader("Review & edit items")
    st.caption(
        "Check each row. **AI Price** is locked (original scan). "
        "Change **Actual Price** if the receipt amount is different."
    )

    modified_mask = df["AI Price"].round(2) != df["Actual Price"].round(2)
    modified_count = int(modified_mask.sum())
    if modified_count:
        st.info(
            f"{modified_count} item(s) have a different Actual Price than AI Price "
            "(will be saved as modified)."
        )

    edited = st.data_editor(
        df,
        column_config={
            "Item Name": st.column_config.TextColumn(
                "Item Name",
                required=True,
                width="large",
            ),
            "Category": st.column_config.SelectboxColumn(
                "Category",
                options=CATEGORIES,
                required=True,
            ),
            "AI Price": st.column_config.NumberColumn(
                "AI Price",
                format="HK$ %.2f",
                disabled=True,
            ),
            "Actual Price": st.column_config.NumberColumn(
                "Actual Price",
                format="HK$ %.2f",
                min_value=0.0,
                step=0.1,
                required=True,
            ),
        },
        hide_index=True,
        use_container_width=True,
        num_rows="dynamic",
        key="items_editor",
    )

    col_ai, col_actual = st.columns(2)
    with col_ai:
        st.metric("Total (AI Price)", f"HK$ {edited['AI Price'].sum():.2f}")
    with col_actual:
        st.metric("Total (Actual Price)", f"HK$ {edited['Actual Price'].sum():.2f}")

    return edited


def render_helper_portal() -> None:
    if not check_helper_password():
        return

    if st.session_state.get("show_insufficient_budget_dialog"):
        insufficient_budget_dialog()

    if st.session_state.last_save_message:
        st.success(st.session_state.last_save_message)
        st.session_state.last_save_message = None

    if st.session_state.budget_request_message:
        st.success(st.session_state.budget_request_message)
        st.session_state.budget_request_message = None

    render_budget_header()
    render_manage_budget_expander()

    st.title("🧾 Grocery Receipt")
    st.caption("Upload one or more receipt photos. AI reads each photo; you confirm before saving.")

    uploaded_files = st.file_uploader(
        "Upload receipt photos",
        type=["jpg", "jpeg", "png", "webp"],
        accept_multiple_files=True,
        help="Select multiple photos from your gallery if needed",
        key=f"receipt_uploader_{st.session_state.uploader_nonce}",
    )

    if uploaded_files:
        cols = st.columns(min(len(uploaded_files), 3))
        for idx, file in enumerate(uploaded_files):
            with cols[idx % len(cols)]:
                st.image(
                    Image.open(file),
                    caption=file.name,
                    use_container_width=True,
                )
                file.seek(0)

    has_uploads = bool(uploaded_files)
    has_pending = st.session_state.items_df is not None

    if has_uploads and st.button(
        "Recognize all receipts",
        type="primary",
        use_container_width=True,
        disabled=not has_uploads,
    ):
        try:
            df = recognize_all_receipts(uploaded_files)
        except json.JSONDecodeError:
            st.error("AI returned invalid JSON. Please try clearer photos.")
        except Exception as exc:
            st.error(f"Error: {exc}")
        else:
            if df.empty:
                st.warning("No items found. Try clearer photos.")
            else:
                st.session_state.items_df = df
                st.success(f"Found {len(df)} item(s) from {len(uploaded_files)} photo(s).")
                st.rerun()

    if st.session_state.items_df is not None:
        edited_df = show_review_editor(st.session_state.items_df.copy())
        st.session_state.items_df = edited_df

        render_confirm_button_css()
        if st.button(
            "Confirm & Submit to Sir",
            type="primary",
            use_container_width=True,
            key="confirm_submit",
        ):
            if edited_df.empty:
                st.warning("No items to save. Add rows or upload new receipts.")
            else:
                remaining_before = database.get_remaining_budget()
                submit_total = round(float(edited_df["Actual Price"].sum()), 2)
                insufficient = submit_total > remaining_before

                db_rows = dataframe_to_db_rows(edited_df)
                saved = database.save_expenses(db_rows)
                modified_saved = sum(1 for r in db_rows if r["is_modified"])
                clear_upload_session()

                st.session_state.last_save_message = (
                    f"Saved {saved} item(s) to the database "
                    f"({modified_saved} with price corrections)."
                )
                if insufficient:
                    st.session_state.show_insufficient_budget_dialog = True

                st.balloons()
                st.rerun()

    if not has_uploads and not has_pending:
        st.info("Upload receipt photo(s), then tap **Recognize all receipts**.")


def render_employer_dashboard_styles() -> None:
    st.markdown(
        """
        <style>
        .employer-hero {
            background: linear-gradient(120deg, #0f172a 0%, #1e40af 55%, #312e81 100%);
            border-radius: 16px;
            padding: 1.5rem 1.75rem;
            margin-bottom: 1.25rem;
            color: #f8fafc;
        }
        .employer-hero h1 {
            margin: 0;
            font-size: 1.85rem;
            font-weight: 800;
        }
        .employer-hero p {
            margin: 0.35rem 0 0 0;
            opacity: 0.9;
        }
        .section-card {
            border: 1px solid #e2e8f0;
            border-radius: 14px;
            padding: 1.25rem 1.35rem;
            margin-bottom: 1.25rem;
            background: #ffffff;
            box-shadow: 0 1px 3px rgba(15, 23, 42, 0.06);
        }
        div[data-testid="stMetric"] {
            background: #f8fafc;
            border: 1px solid #e2e8f0;
            border-radius: 12px;
            padding: 0.65rem 0.85rem;
        }
        button[data-testid*="approve_req_"] {
            background-color: #16a34a !important;
            color: white !important;
            font-weight: 700 !important;
            min-height: 2.75rem !important;
        }
        button[data-testid*="reject_req_"] {
            font-weight: 700 !important;
            min-height: 2.75rem !important;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def render_employer_section_header(number: str, title: str, subtitle: str) -> None:
    st.markdown(f"## {number}. {title}")
    st.markdown(f"*{subtitle}*")
    st.markdown("")


def is_high_price_variance(ai_price: float, actual_price: float) -> bool:
    variance = abs(actual_price - ai_price)
    if variance >= AUDIT_VARIANCE_ABSOLUTE_HKD:
        return True
    if ai_price > 0 and variance / ai_price >= AUDIT_VARIANCE_PERCENT:
        return True
    return False


def _display_spending_alert(alert) -> None:
    """Show alert from database (string or legacy dict)."""
    if isinstance(alert, str):
        st.error(f"⚠️ **{alert}** Potential fraud or waste detected!")
        return
    st.error(
        f"⚠️ **WARNING:** {alert['category']} spending spiked by "
        f"**{alert['change_pct']:.0f}%** this week "
        f"(HK$ {alert['this_week']:,.2f} vs baseline "
        f"HK$ {alert['baseline_weekly_avg']:,.2f}/week). "
        "Potential fraud or waste detected!"
    )


def render_employer_anomaly_banner() -> None:
    alerts, current_week_spend = database.get_category_spending_anomalies()
    if alerts:
        for alert in alerts:
            _display_spending_alert(alert)
    else:
        st.success("✅ All category spendings are within normal ranges.")


def render_financial_requests_section() -> None:
    render_employer_section_header(
        "1",
        "Financial Requests Dashboard",
        "Approve or reject pending top-up and correction requests.",
    )

    pending = database.get_pending_budget_requests()
    if not pending:
        st.info("No pending requests. All clear.")
        return

    summary_rows = []
    for req in pending:
        amount = float(req["amount"])
        summary_rows.append(
            {
                "ID": req["id"],
                "Date": req["date"],
                "Type": req["type"],
                "Amount (HKD)": amount,
                "Reason": req["reason"] or "—",
            }
        )
    st.dataframe(
        pd.DataFrame(summary_rows),
        use_container_width=True,
        hide_index=True,
    )

    st.markdown("**Actions**")
    for req in pending:
        amount = float(req["amount"])
        sign = "+" if amount >= 0 else ""
        type_label = "Top-up" if req["type"] == "Top-up" else "Correction"
        with st.container(border=True):
            st.markdown(
                f"**Request #{req['id']}** · {req['date']} · **{type_label}** · "
                f"**{sign}${amount:,.2f} HKD**"
            )
            if req["reason"]:
                st.caption(f"Reason: {req['reason']}")

            btn_approve, btn_reject, _spacer = st.columns([2, 2, 3])
            with btn_approve:
                if st.button(
                    "✅ Approve",
                    type="primary",
                    use_container_width=True,
                    key=f"approve_req_{req['id']}",
                ):
                    if database.set_budget_request_status(req["id"], "Approved"):
                        st.toast(f"Request #{req['id']} approved.", icon="✅")
                        st.rerun()
                    else:
                        st.error("Could not approve (already processed?).")
            with btn_reject:
                if st.button(
                    "❌ Reject",
                    use_container_width=True,
                    key=f"reject_req_{req['id']}",
                ):
                    if database.set_budget_request_status(req["id"], "Rejected"):
                        st.toast(f"Request #{req['id']} rejected.", icon="❌")
                        st.rerun()
                    else:
                        st.error("Could not reject (already processed?).")


def render_expense_analytics_section() -> None:
    render_employer_section_header(
        "2",
        "Expense Summaries & Analytics",
        "Month-to-date, week-to-date, and category breakdown.",
    )

    month_total = database.get_current_month_spending()
    week_total = database.get_current_week_spending()
    remaining = database.get_remaining_budget()

    m1, m2, m3 = st.columns(3)
    with m1:
        st.metric(
            "Current Month Total Spending",
            f"HK$ {month_total:,.2f}",
        )
    with m2:
        st.metric(
            "Current Week Total Spending",
            f"HK$ {week_total:,.2f}",
        )
    with m3:
        st.metric(
            "Remaining Budget",
            f"HK$ {remaining:,.2f}",
            delta=None,
        )

    st.markdown("#### Spending by category (all time)")
    category_totals = database.get_spending_by_category()
    chart_df = pd.DataFrame(
        {
            "Spending (HKD)": [
                category_totals.get(cat, 0.0) for cat in CATEGORIES
            ],
        },
        index=list(CATEGORIES),
    )

    if chart_df["Spending (HKD)"].sum() > 0:
        left, right = st.columns([3, 2])
        with left:
            st.bar_chart(chart_df, height=320)
        with right:
            st.markdown("**Share of wallet**")
            pie_df = chart_df[chart_df["Spending (HKD)"] > 0]
            st.pyplot(
                _category_pie_chart(pie_df),
                use_container_width=True,
                clear_figure=True,
            )
    else:
        st.info("No grocery spending recorded yet.")


def _category_pie_chart(df: pd.DataFrame):
    """Simple pie chart for category share (matplotlib)."""
    fig, ax = plt.subplots(figsize=(4, 4))
    colors = ["#ef4444", "#0ea5e9", "#22c55e", "#f59e0b"]
    ax.pie(
        df["Spending (HKD)"],
        labels=df.index,
        autopct="%1.0f%%",
        startangle=90,
        colors=colors[: len(df)],
    )
    ax.axis("equal")
    fig.patch.set_facecolor("#ffffff")
    return fig


def render_ai_helper_audit_section() -> None:
    render_employer_section_header(
        "3",
        "AI vs Helper Audit",
        "Line items where the helper manually changed AI-scanned prices.",
    )

    records = database.get_modified_expense_records()
    if not records:
        st.success("No manual price edits detected. All entries match AI scans.")
        return

    audit_rows = []
    flagged = []
    for row in records:
        ai_price = round(float(row["ai_price"]), 2)
        actual_price = round(float(row["actual_price"]), 2)
        variance = round(actual_price - ai_price, 2)
        high_risk = is_high_price_variance(ai_price, actual_price)
        audit_rows.append(
            {
                "Date": row["date"],
                "Item Name": row["item_name"],
                "Category": row["category"],
                "AI Price (HKD)": ai_price,
                "Actual Price (HKD)": actual_price,
                "Variance (HKD)": variance,
                "Flag": "⚠️ Review" if high_risk else "OK",
            }
        )
        if high_risk:
            flagged.append((row, variance))

    st.dataframe(
        pd.DataFrame(audit_rows),
        use_container_width=True,
        hide_index=True,
    )

    if flagged:
        st.markdown("#### ⚠️ High-variance alerts")
        for row, variance in flagged:
            st.markdown(
                f'<div style="color:#b91c1c;font-weight:600;padding:0.35rem 0;">'
                f"**{row['date']}** · {row['item_name']} — "
                f"AI HK$ {float(row['ai_price']):,.2f} → "
                f"Actual HK$ {float(row['actual_price']):,.2f} "
                f"(variance **{variance:+,.2f}**). "
                f"Please verify the original handwritten receipt."
                f"</div>",
                unsafe_allow_html=True,
            )


def render_anomaly_detection_section() -> None:
    render_employer_section_header(
        "4",
        "AI Anomaly Detection",
        "Weekly spend vs 4-week historical baseline (30% spike threshold).",
    )

    alerts, current_week_spend = database.get_category_spending_anomalies()
    baseline_weekly = database.get_category_weekly_baseline_dict()

    spike_categories = set()
    for alert in alerts:
        _display_spending_alert(alert)
        if isinstance(alert, str):
            for cat in CATEGORIES:
                if cat in alert:
                    spike_categories.add(cat)
        elif isinstance(alert, dict) and "category" in alert:
            spike_categories.add(alert["category"])

    if not alerts:
        st.success("✅ All category spendings are within normal ranges.")

    expense_categories = list(database.EXPENSE_CATEGORIES)
    baseline_df = pd.DataFrame(
        [
            {
                "Category": cat,
                "This Week (HKD)": current_week_spend.get(cat, 0.0),
                "Baseline Weekly Avg (HKD)": baseline_weekly.get(cat, 0.0),
                "Status": "SPIKE" if cat in spike_categories else "Normal",
            }
            for cat in expense_categories
        ]
    )
    st.dataframe(baseline_df, use_container_width=True, hide_index=True)

    compare_df = pd.DataFrame(
        {
            "This Week": [current_week_spend.get(cat, 0.0) for cat in expense_categories],
            "Baseline Weekly Avg": [
                baseline_weekly.get(cat, 0.0) for cat in expense_categories
            ],
        },
        index=expense_categories,
    )
    st.markdown("#### Week vs baseline comparison")
    st.bar_chart(compare_df, height=300)


def render_employer_dashboard() -> None:
    if not check_employer_password():
        return

    render_employer_dashboard_styles()

    st.markdown(
        """
        <div class="employer-hero">
            <h1>Employer Dashboard</h1>
            <p>Financial control center — approvals, analytics, and fraud alerts.</p>
        </div>
        """,
        unsafe_allow_html=True,
    )

    render_employer_anomaly_banner()
    st.markdown("---")

    with st.container():
        render_financial_requests_section()

    st.markdown("---")
    with st.container():
        render_expense_analytics_section()

    st.markdown("---")
    with st.container():
        render_ai_helper_audit_section()

    st.markdown("---")
    with st.container():
        render_anomaly_detection_section()


def main() -> None:
    st.set_page_config(
        page_title="Helper Receipt App",
        page_icon="🧾",
        layout="wide",
        initial_sidebar_state="expanded",
    )

    init_session_state()
    database.init_db()

    with st.sidebar:
        st.header("Portal")
        portal = st.radio(
            "Choose portal",
            ["Helper Portal", "Employer Dashboard"],
            label_visibility="collapsed",
        )
        st.caption("Helper: upload receipts & request budget.")
        st.caption("Employer: approve financial requests.")

    if portal == "Employer Dashboard":
        render_employer_dashboard()
    else:
        render_helper_portal()


if __name__ == "__main__":
    main()
