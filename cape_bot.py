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
  DRY_RUN=1     print emails instead of sending
  SEND_STATUS=1 email the current reading now
"""
import bisect
import csv
import io
import json
import os
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
SHILLER_STALE_DAYS = 100             # warn if Shiller's latest month is older
SHILLER_URLS = [u for u in (
    os.getenv("SHILLER_URL"),
    "http://www.econ.yale.edu/~shiller/data/ie_data.xls",
) if u]

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


def load_shiller(state, alerts):
    last = state.get("shiller_last_refresh")
    due = (not SHILLER_CACHE.exists() or not last or
           (now_utc() - datetime.fromisoformat(last)).days >= SHILLER_REFRESH_DAYS)
    if due:
        for url in SHILLER_URLS:
            try:
                r = requests.get(url, timeout=90,
                                 headers={"User-Agent": "Mozilla/5.0 (cape-bot)"})
                r.raise_for_status()
                df = parse_shiller(r.content)
                df.to_csv(SHILLER_CACHE, index=False)
                state["shiller_last_refresh"] = now_utc().isoformat()
                print(f"Shiller data refreshed from {url}: through {df['month'].iloc[-1]}")
                break
            except Exception as e:
                print(f"Shiller download failed from {url}: {e}")
        else:
            print("Using cached Shiller data.")

    if not SHILLER_CACHE.exists():
        raise SystemExit(
            "No Shiller data available. Copy the ie_data download link from "
            "shillerdata.com into a repo secret called SHILLER_URL and run again.")

    df = pd.read_csv(SHILLER_CACHE, dtype={"month": str})

    # Warn once if the data has gone stale (download keeps failing)
    latest = datetime.strptime(df["month"].iloc[-1], "%Y-%m").replace(tzinfo=timezone.utc)
    age = (now_utc() - latest).days
    if age > SHILLER_STALE_DAYS and not state.get("stale_alerted"):
        alerts.append(f"⚠️ Shiller data is old (latest month {df['month'].iloc[-1]}). "
                      "The download link has probably changed: update the SHILLER_URL "
                      "secret from shillerdata.com.")
        state["stale_alerted"] = True
    elif age <= SHILLER_STALE_DAYS:
        state["stale_alerted"] = False
    return df


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
    alerts = []

    df = load_shiller(state, alerts)
    avg_real_e, earnings_through = ten_year_real_earnings(df)
    window, w_from, w_to = rolling_window(df)
    cutoffs = [round(float(x), 2) for x in np.percentile(window, [20, 40, 60, 80])]

    spx, voo = last_close("^GSPC"), last_close("VOO")
    if spx is None:
        state["fail_count"] = state.get("fail_count", 0) + 1
        if state["fail_count"] == FAIL_ALERT_AFTER:
            alerts.append(f"⚠️ No S&P 500 price from yfinance for {FAIL_ALERT_AFTER} runs in a row.")
        if alerts:
            send("VOO CAPE bot: data problem", "\n\n".join(alerts))
        save_state(state)
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
        send(f"VOO CAPE bot is live: {TIER_NAMES[new]}", describe(reading))
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
                send(f"VOO is now {TIER_NAMES[new]} ({arrow})",
                     f"{TIER_NAMES[old]} → {TIER_NAMES[new]}\n\n" + describe(reading))

    if os.getenv("SEND_STATUS") == "1":
        send(f"VOO CAPE status: {TIER_NAMES[new]}", describe(reading))
    if alerts:
        send("VOO CAPE bot: data problem", "\n\n".join(alerts))
    save_state(state)


if __name__ == "__main__":
    main()
