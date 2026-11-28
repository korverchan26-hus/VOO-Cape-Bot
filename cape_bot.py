"""
VOO CAPE buy-signal bot.

CAPE (Shiller P/E) = S&P 500 price / average of the last 10 years of
inflation-adjusted earnings. VOO tracks the S&P 500, so this is VOO's CAPE.

How the live number is built:
  - 10-year earnings and CPI come from Robert Shiller's dataset (monthly).
  - Today's S&P 500 price comes from yfinance (^GSPC), so CAPE updates daily.

Tiers: today's CAPE is compared with Shiller's monthly CAPE over the last
20 years (rolling). Each 20% band of that history is one tier.

Emails:
  - when a tier change to a CHEAPER tier is confirmed, and
  - when CAPE rises INTO Expensive or Very expensive.
  A change must hold for 2 runs in a row before it counts.

Env vars:
  SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS, EMAIL_TO   (required)
  SHILLER_URL   optional: direct download link for ie_data.xls
                (normally not needed: the bot finds the link on shillerdata.com)
  DRY_RUN=1     print emails instead of sending
  SEND_STATUS=1 email the current reading now
"""
import bisect
import csv
import io
import json
import os
import re
import smtplib
import time
from datetime import datetime, timezone
from email.mime.text import MIMEText
from pathlib import Path

import numpy as np
import pandas as pd
import requests
import yfinance as yf

# ---------------------------------------------------------------- config
WINDOW_YEARS = 20
TIER_NAMES = ["Cheap", "Below average", "Fair", "Expensive", "Very expensive"]
ALERT_WHEN_RISING_INTO = {3, 4}      # Expensive, Very expensive
CONFIRM_RUNS = 2
FAIL_ALERT_AFTER = 3                 # failed price fetches in a row
SHILLER_REFRESH_DAYS = 7             # how often to re-download Shiller data
SHILLER_STALE_DAYS = 120             # data older than this is not trusted for tiers
SHILLER_HOME = "https://shillerdata.com/"
OLD_YALE_URL = "http://www.econ.yale.edu/~shiller/data/ie_data.xls"  # stopped updating 2023

ROOT = Path(__file__).parent
STATE_FILE = ROOT / "state.json"
HISTORY_FILE = ROOT / "cape_history.csv"
SHILLER_CACHE = ROOT / "shiller_cache.csv"
HISTORY_FIELDS = ["date", "spx", "voo", "cape", "percentile", "tier"]
DRY_RUN = os.getenv("DRY_RUN") == "1"


def now_utc():
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------- storage
def load_state():
    return json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")


def append_history(row):
    rows = []
    if HISTORY_FILE.exists():
        with HISTORY_FILE.open(newline="") as f:
            rows = [r for r in csv.DictReader(f) if r["date"] != row["date"]]
    rows.append({k: row[k] for k in HISTORY_FIELDS})
    with HISTORY_FILE.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=HISTORY_FIELDS)
        w.writeheader()
        w.writerows(rows)


# ---------------------------------------------------------------- Shiller data
def _shiller_month(x):
    """Shiller dates are floats: 1871.01 = Jan, 1871.1 = Oct."""
    year = int(x)
    month = int(round((x - year) * 100))
    return f"{year:04d}-{month:02d}"


def parse_shiller(content):
    raw = pd.read_excel(io.BytesIO(content), sheet_name="Data", header=None)
    header_row = None
    for i in range(min(25, len(raw))):
        vals = [str(v).strip() for v in raw.iloc[i].tolist()]
        if {"Date", "P", "E", "CPI", "CAPE"} <= set(vals):
            header_row = i
            break
    if header_row is None:
        raise ValueError("Could not find the Date/P/E/CPI/CAPE header row")

    names = [str(v).strip() for v in raw.iloc[header_row].tolist()]
    pick = {n: names.index(n) for n in ("Date", "P", "E", "CPI", "CAPE")}
    body = raw.iloc[header_row + 1:]
    df = pd.DataFrame({n: pd.to_numeric(body.iloc[:, i], errors="coerce")
                       for n, i in pick.items()})
    df = df.dropna(subset=["Date", "P", "CPI"])
    df["month"] = df["Date"].map(_shiller_month)
    df = df[["month", "P", "E", "CPI", "CAPE"]].drop_duplicates("month", keep="last")
    if len(df) < 12 * 40:
        raise ValueError(f"Only {len(df)} rows parsed; file layout may have changed")
    return df.reset_index(drop=True)


UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) cape-bot"}


def find_shiller_links():
    """Find the current ie_data download link(s) on shillerdata.com."""
    try:
        html = requests.get(SHILLER_HOME, timeout=60, headers=UA).text
    except Exception as e:
        print(f"Could not open {SHILLER_HOME}: {e}")
        return []
    links = []
    for href in re.findall(r'href=["\']([^"\']+)["\']', html, flags=re.I):
        href = href.replace("&amp;", "&")
        if "ie_data" in href.lower() and "trcape" not in href.lower():
            if href.startswith("//"):
                href = "https:" + href
            elif href.startswith("/"):
                href = SHILLER_HOME.rstrip("/") + href
            if href not in links:
                links.append(href)
    print(f"Found on shillerdata.com: {links or 'no ie_data link'}")
    return links


def data_age_days(df):
    latest = datetime.strptime(df["month"].iloc[-1], "%Y-%m").replace(tzinfo=timezone.utc)
    return (now_utc() - latest).days


def download_freshest():
    """Try every source; return the parsed data with the latest month."""
    urls = [u for u in [os.getenv("SHILLER_URL"), *find_shiller_links(), OLD_YALE_URL] if u]
    best = None
    for url in urls:
        try:
            r = requests.get(url, timeout=90, headers=UA)
            r.raise_for_status()
            df = parse_shiller(r.content)
            print(f"  {url[:90]} -> data through {df['month'].iloc[-1]}")
            if best is None or df["month"].iloc[-1] > best["month"].iloc[-1]:
                best = df
            if data_age_days(df) <= SHILLER_STALE_DAYS:
                break               # fresh enough, stop looking
        except Exception as e:
            print(f"  {url[:90]} -> failed: {e}")
    return best


def load_shiller(state):
    """Returns (data, is_fresh)."""
    cached = pd.read_csv(SHILLER_CACHE, dtype={"month": str}) if SHILLER_CACHE.exists() else None
    last = state.get("shiller_last_refresh")
    due = (cached is None or not last or data_age_days(cached) > SHILLER_STALE_DAYS or
           (now_utc() - datetime.fromisoformat(last)).days >= SHILLER_REFRESH_DAYS)
    if due:
        new = download_freshest()
        if new is not None and (cached is None or new["month"].iloc[-1] >= cached["month"].iloc[-1]):
            new.to_csv(SHILLER_CACHE, index=False)
            cached = new
            state["shiller_last_refresh"] = now_utc().isoformat()
    if cached is None:
        return None, False
    return cached, data_age_days(cached) <= SHILLER_STALE_DAYS


def ten_year_real_earnings(df):
    """Average of the last 120 months of earnings, in latest-CPI dollars."""
    cpi_now = df["CPI"].dropna().iloc[-1]
    e = df.dropna(subset=["E", "CPI"]).tail(120)
    if len(e) < 120:
        raise ValueError("Not enough earnings data for a 10-year average")
    real_e = e["E"] * cpi_now / e["CPI"]
    return float(real_e.mean()), e["month"].iloc[-1]


def rolling_window(df):
    cape = df.dropna(subset=["CAPE"])
    cape = cape[cape["CAPE"] > 0].tail(WINDOW_YEARS * 12)
    return cape["CAPE"].to_numpy(), cape["month"].iloc[0], cape["month"].iloc[-1]


# ---------------------------------------------------------------- prices
def last_close(symbol, retries=3):
    for attempt in range(retries):
        try:
            h = yf.Ticker(symbol).history(period="5d", auto_adjust=False)
            if not h.empty:
                return round(float(h["Close"].dropna().iloc[-1]), 2)
            print(f"[{symbol}] empty price history (attempt {attempt + 1})")
        except Exception as e:
            print(f"[{symbol}] fetch error (attempt {attempt + 1}): {e}")
        time.sleep(5 * (attempt + 1))
    return None


# ---------------------------------------------------------------- tiers
def tier_for(cape, cutoffs):
    return bisect.bisect_right(cutoffs, cape)


def should_alert(old, new):
    return new < old or (new > old and new in ALERT_WHEN_RISING_INTO)


def ordinal(n):
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def describe(r):
    c = r["cutoffs"]
    return (
        f"VOO ${r['voo']}   |   S&P 500 {r['spx']:,}\n"
        f"Live CAPE: {r['cape']}  ({ordinal(r['percentile'])} percentile of the last {WINDOW_YEARS} years)\n"
        f"Tier: {TIER_NAMES[r['tier']]}\n\n"
        f"Tier cut-offs ({r['window_from']} to {r['window_to']}, monthly CAPE):\n"
        f"  Cheap           below {c[0]}\n"
        f"  Below average   {c[0]} – {c[1]}\n"
        f"  Fair            {c[1]} – {c[2]}\n"
        f"  Expensive       {c[2]} – {c[3]}\n"
        f"  Very expensive  above {c[3]}\n\n"
        f"Earnings data through {r['earnings_through']}."
    )


# ---------------------------------------------------------------- email
def send(subject, body):
    if DRY_RUN:
        print(f"\n=== DRY RUN EMAIL ===\nSubject: {subject}\n\n{body}\n")
        return
    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = os.environ["SMTP_USER"]
    msg["To"] = os.environ["EMAIL_TO"]
    with smtplib.SMTP(os.environ["SMTP_HOST"], int(os.getenv("SMTP_PORT") or 587)) as s:
        s.starttls()
        s.login(os.environ["SMTP_USER"], os.environ["SMTP_PASS"])
        s.send_message(msg)


# ---------------------------------------------------------------- main
def main():
    state = load_state()
    emails = []                      # (subject, body); combined into ONE email per run

    df, fresh = load_shiller(state)
    if not fresh:
        # Don't work out tiers from out-of-date earnings: the CAPE would be wrong.
        latest = df["month"].iloc[-1] if df is not None else "none"
        if not state.get("stale_alerted") or os.getenv("SEND_STATUS") == "1":
            emails.append(("data problem",
                f"⚠️ Shiller's data is out of date (latest month: {latest}), so the bot "
                "is not working out tiers until it can get current data.\n\n"
                "Fix: go to shillerdata.com, right-click the ie_data download button, "
                "copy the link, and save it as a repo secret called SHILLER_URL. "
                "You'll get the 'bot is live' email once it works."))
            state["stale_alerted"] = True
        finish(state, emails)
        return
    state["stale_alerted"] = False

    avg_real_e, earnings_through = ten_year_real_earnings(df)
    window, w_from, w_to = rolling_window(df)
    cutoffs = [round(float(x), 2) for x in np.percentile(window, [20, 40, 60, 80])]

    spx, voo = last_close("^GSPC"), last_close("VOO")
    if spx is None:
        state["fail_count"] = state.get("fail_count", 0) + 1
        if state["fail_count"] == FAIL_ALERT_AFTER:
            emails.append(("data problem",
                f"⚠️ No S&P 500 price from yfinance for {FAIL_ALERT_AFTER} runs in a row."))
        finish(state, emails)
        return
    state["fail_count"] = 0

    cape = round(spx / avg_real_e, 2)
    reading = {
        "date": now_utc().strftime("%Y-%m-%d"), "spx": spx, "voo": voo, "cape": cape,
        "percentile": round(100 * float((window <= cape).mean())),
        "tier": tier_for(cape, cutoffs), "cutoffs": cutoffs,
        "window_from": w_from, "window_to": w_to, "earnings_through": earnings_through,
    }
    append_history(reading)
    state["last"] = {k: reading[k] for k in HISTORY_FIELDS}
    print(f"CAPE {cape} | {TIER_NAMES[reading['tier']]} | cut-offs {cutoffs}")

    new = reading["tier"]
    if state.get("tier") is None:                       # first run
        state.update(tier=new, pending_tier=None, pending_count=0)
        emails.append((f"bot is live: {TIER_NAMES[new]}", describe(reading)))
    elif new == state["tier"]:
        state.update(pending_tier=None, pending_count=0)
    else:
        if new == state.get("pending_tier"):
            state["pending_count"] += 1
        else:
            state.update(pending_tier=new, pending_count=1)
        if state["pending_count"] >= CONFIRM_RUNS:
            old = state["tier"]
            state.update(tier=new, pending_tier=None, pending_count=0)
            if should_alert(old, new):
                arrow = "📉 cheaper" if new < old else "📈 more expensive"
                emails.append((f"now {TIER_NAMES[new]} ({arrow})",
                               f"{TIER_NAMES[old]} → {TIER_NAMES[new]}\n\n" + describe(reading)))

    if os.getenv("SEND_STATUS") == "1" and not emails:
        emails.append((f"status: {TIER_NAMES[new]}", describe(reading)))
    finish(state, emails)


def finish(state, emails):
    """Save state FIRST, then send at most one email."""
    save_state(state)
    if not emails and os.getenv("SEND_STATUS") == "1":
        emails = [("status", "No reading available this run (see the Actions log).")]
    if emails:
        subject = "VOO CAPE " + " + ".join(s for s, _ in emails)
        body = ("\n\n" + "-" * 40 + "\n\n").join(b for _, b in emails)
        send(subject, body)


if __name__ == "__main__":
    main()
