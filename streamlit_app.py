# streamlit_app.py
# Cloud-friendly Streamlit app to monitor product pages for stock changes
# Uses /tmp for ephemeral state. Configure SMTP in Streamlit Cloud Secrets.

import json
import time
import hashlib
from datetime import datetime, timezone
from typing import Dict, Any, List

import requests
from bs4 import BeautifulSoup
import streamlit as st
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

DATA_PATH = "/tmp/stock_monitor_state.json"  # writable in Streamlit Cloud (ephemeral)
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# Session for HTTP
session = requests.Session()
session.headers.update({
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.hermes.com/us/en/",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "DNT": "1",
})


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_state() -> Dict[str, Any]:
    try:
        with open(DATA_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"targets": {}, "email": {}}


def save_state(state: Dict[str, Any]) -> None:
    with open(DATA_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# Core extraction

def extract_stock_info(html: str) -> Dict[str, Any]:
    soup = BeautifulSoup(html, "html.parser")
    stock: Dict[str, Any] = {}

    # Buttons
    notify_button = soup.find("button", string=lambda s: s and "notify me" in s.lower())
    if notify_button:
        stock["notify_button"] = True

    add_btn = soup.find(
        "button",
        string=lambda s: s and any(k in s.lower() for k in ["add to cart", "add to bag", "buy now", "purchase"]),
    )
    if add_btn:
        stock["add_to_cart"] = True

    # Text indicators
    text = soup.get_text(separator=" ").lower()
    stock["full_text"] = " ".join(text.split())

    prod_info = soup.find("div", {"class": lambda c: c and "product" in c.lower()})
    if prod_info:
        stock["product_section"] = " ".join(prod_info.get_text(separator=" ").lower().split())

    return stock


def stock_status(stock: Dict[str, Any]) -> str:
    if stock.get("notify_button"):
        return "OUT_OF_STOCK"
    if stock.get("add_to_cart"):
        return "IN_STOCK"

    t = stock.get("full_text", "")
    out_keys = [
        "notify you when this product is back in stock",
        "notify me",
        "out of stock",
        "unavailable",
    ]
    in_keys = ["add to cart", "add to bag", "buy now", "purchase", "in stock"]

    if any(k in t for k in out_keys):
        return "OUT_OF_STOCK"
    if any(k in t for k in in_keys):
        return "IN_STOCK"
    return "UNKNOWN"


def content_hash(obj: Dict[str, Any]) -> str:
    blob = json.dumps(obj, sort_keys=True, ensure_ascii=False)
    return hashlib.md5(blob.encode("utf-8")).hexdigest()


# Fetch with light retry for 403/429

def fetch(url: str, timeout: int = 20) -> str:
    for attempt in range(3):
        r = session.get(url, timeout=timeout)
        if r.status_code in (403, 429):
            time.sleep(5 * (attempt + 1))
            continue
        r.raise_for_status()
        return r.text
    # Last try raises if still blocked
    r.raise_for_status()
    return r.text


# Email via Streamlit secrets

def get_email_cfg() -> Dict[str, Any]:
    # Expect secrets like:
    # [email]
    # enabled = true
    # sender = "you@example.com"
    # password = "app_password"
    # smtp_server = "smtp.gmail.com"
    # smtp_port = 587
    return dict(st.secrets.get("email", {}))


def send_email(subject: str, body: str, recipients: List[str]):
    cfg = get_email_cfg()
    if not (cfg.get("enabled") and cfg.get("sender") and cfg.get("password") and recipients):
        return

    msg = MIMEMultipart()
    msg["From"] = cfg["sender"]
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain"))

    server = smtplib.SMTP(cfg.get("smtp_server", "smtp.gmail.com"), int(cfg.get("smtp_port", 587)), timeout=30)
    try:
        server.starttls()
        server.login(cfg["sender"], cfg["password"])
        for rcpt in recipients:
            msg["To"] = rcpt
            server.sendmail(cfg["sender"], rcpt, msg.as_string())
    finally:
        server.quit()


# UI
st.set_page_config(page_title="Stock Monitor", layout="wide")
st.title("Website Stock Monitor")
st.caption("Track product pages for stock-related changes. If the site uses JavaScript or strict bot protection, results may be limited.")

state = load_state()

with st.sidebar:
    st.header("Track a URL")
    default_url = "https://www.hermes.com/us/en/product/rodeo-pegase-pm-charm-H083010CADX/"
    url = st.text_input("Product URL", value=default_url)
    interval_min = st.number_input("Check interval (minutes)", min_value=1, max_value=120, value=5)
    recipients_str = st.text_input("Email recipients (comma-separated)", value="")

    if st.button("Add or update tracked URL"):
        t = state["targets"].get(url, {})
        t.update({
            "url": url,
            "interval_sec": int(interval_min) * 60,
            "recipients": [e.strip() for e in recipients_str.split(",") if e.strip()],
            "last_checked": t.get("last_checked"),
            "last_status": t.get("last_status", "UNKNOWN"),
            "previous_hash": t.get("previous_hash"),
            "last_change": t.get("last_change"),
            "change_count": t.get("change_count", 0),
            "log": t.get("log", []),
        })
        state["targets"][url] = t
        save_state(state)
        st.success("Tracking updated")

st.subheader("Tracked URLs")
if not state["targets"]:
    st.info("No URLs tracked yet. Add one in the sidebar.")
else:
    import pandas as pd
    rows = []
    for u, t in state["targets"].items():
        rows.append({
            "URL": u,
            "Status": t.get("last_status", "UNKNOWN"),
            "Last Checked": t.get("last_checked", "-"),
            "Last Change": t.get("last_change", "-"),
            "Changes": int(t.get("change_count", 0)),
            "Interval (s)": int(t.get("interval_sec", 300)),
        })
    st.dataframe(pd.DataFrame(rows), use_container_width=True)

st.subheader("Manual Check")
choices = list(state["targets"].keys())
sel = st.selectbox("Pick a URL to check now", choices if choices else [""])

if st.button("Check now") and sel:
    t = state["targets"][sel]
    try:
        html = fetch(sel)
        s_info = extract_stock_info(html)
        h = content_hash(s_info)
        new_status = stock_status(s_info)
        changed = t.get("previous_hash") and h != t["previous_hash"]
        if changed:
            t["last_change"] = now_iso()
            t["change_count"] = int(t.get("change_count", 0)) + 1
            msg = f"Page changed. Status: {new_status}. URL: {sel}"
            send_email("Stock update", msg, t.get("recipients", []))
            t.setdefault("log", []).append({"at": t["last_change"], "status": new_status})
        t["previous_hash"] = h
        t["last_status"] = new_status
        t["last_checked"] = now_iso()
        save_state(state)
        st.success(f"Checked. Current status: {new_status}{' (changed)' if changed else ''}.")
    except Exception as e:
        st.error(f"Error: {e}")

st.divider()
st.write("Notes: State is stored in /tmp and will reset on redeploys. Email settings come from Streamlit Secrets. If you need Playwright for JS-rendered sites, Community Cloud may not support installing browsers.")


# -----------------
# requirements.txt
# -----------------
# Place this content in a separate file named requirements.txt in your repo:
# streamlit==1.37.0
# requests>=2.31.0
# beautifulsoup4>=4.12.2
