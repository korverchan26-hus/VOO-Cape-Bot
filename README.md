# VOO CAPE bot

A small GitHub Actions bot that tracks how expensive VOO (the S&P 500) is, using
the **CAPE ratio** (Shiller P/E), and emails you when the valuation tier changes.

It's built as a **buy signal**: you hear about it when VOO gets cheaper, and when
it moves into Expensive or Very expensive territory.

---

## What is CAPE?

CAPE (Cyclically Adjusted Price-to-Earnings) is:

```
S&P 500 price  ÷  average of the last 10 years of earnings, adjusted for inflation
```

Normal P/E uses only the last 12 months of earnings, which jump around with the
business cycle. CAPE averages 10 years, so it gives a steadier read on whether the
market is cheap or expensive. VOO tracks the S&P 500, so the S&P 500's CAPE is VOO's CAPE.

## How the bot works

1. **Earnings and inflation data** come from Robert Shiller's free dataset
   (`ie_data.xls`, monthly, back to 1871). The bot finds the current download link on
   shillerdata.com, re-downloads it once a week, and keeps a copy in the repo
   (`shiller_cache.csv`). If the data is more than ~4 months old, the bot **pauses tiers**
   (an out-of-date CAPE would be wrong) and tells you once.
2. **Today's S&P 500 price** comes from yfinance (`^GSPC`). VOO's price is fetched
   too, for the email.
3. **Live CAPE** = today's S&P 500 price ÷ the 10-year average real earnings.
   Because the price is live, CAPE updates every weekday instead of once a month.
4. **Tier**: today's CAPE is compared with Shiller's monthly CAPE over the
   **last 20 years** (rolling, so the window moves forward over time). That history
   is split into five 20% bands:

   | Tier | Where today's CAPE sits in the last 20 years |
   |---|---|
   | Cheap | bottom 20% |
   | Below average | 20–40% |
   | Fair | 40–60% |
   | Expensive | 60–80% |
   | Very expensive | top 20% |

5. **Confirmation**: a new tier must hold for **2 runs in a row** before it counts.
   This stops alerts when CAPE wobbles around a cut-off.

## When you get an email

| Situation | Email? |
|---|---|
| First ever run | ✅ "Bot is live" with today's reading and cut-offs |
| Moves into any **cheaper** tier (e.g. Fair → Below average, Very expensive → Expensive) | ✅ |
| Rises into **Expensive** or **Very expensive** | ✅ |
| Rises into Below average or Fair | ❌ |
| Tier unchanged | ❌ |
| No S&P 500 price for 3 runs in a row | ⚠️ data warning (once) |
| Shiller data hasn't updated for ~100 days | ⚠️ data warning (once) |

Each alert shows VOO's price, the S&P 500 level, the live CAPE, its percentile in
the last 20 years, and the current CAPE cut-off for every tier.

## Schedule

Runs Monday–Friday at **22:30 UTC**, after the US market closes. That's about
**9:30am Melbourne** in summer (AEDT) and **8:30am** in winter (AEST), the next day.

---

## Setup

### 1. Create the repo
Create a new GitHub repository (private is fine) and upload everything in this
folder. Make sure the `.github/workflows/cape-bot.yml` file keeps its folder path.

### 2. Add secrets
Go to **Settings → Secrets and variables → Actions → New repository secret** and add:

| Secret | Example | Notes |
|---|---|---|
| `SMTP_HOST` | `smtp.gmail.com` | |
| `SMTP_PORT` | `587` | Optional, defaults to 587 |
| `SMTP_USER` | `you@gmail.com` | The account that sends the email |
| `SMTP_PASS` | `abcd efgh ijkl mnop` | For Gmail, use an **App Password**, not your normal password |
| `EMAIL_TO` | `you@gmail.com` | Where alerts go |
| `SHILLER_URL` | *(optional)* | Direct download link for **ie_data** from shillerdata.com. Only needed if the bot can't find the link itself (see Troubleshooting) |

These are the same SMTP secrets your other bots use.

### 3. Let the workflow save its data
Go to **Settings → Actions → General → Workflow permissions** and choose
**Read and write permissions**. The bot commits `state.json`, `cape_history.csv`
and `shiller_cache.csv` back to the repo after each run.

### 4. Do a first run
Go to **Actions → VOO CAPE bot → Run workflow**. Within a few minutes you should get
the "VOO CAPE bot is live" email. Check that the CAPE and cut-off numbers look sensible.

From then on it runs by itself.

---

## Getting the current reading any time

**Actions → VOO CAPE bot → Run workflow**, tick **Email me the current reading**, then
**Run workflow**. You'll get a status email even if the tier hasn't changed.

## Files

| File | What it is |
|---|---|
| `cape_bot.py` | The bot |
| `.github/workflows/cape-bot.yml` | Schedule and GitHub Actions setup |
| `requirements.txt` | Python packages |
| `state.json` | *(created by the bot)* Current tier, pending change, failure counts |
| `cape_history.csv` | *(created by the bot)* One row per run: date, S&P 500, VOO, CAPE, percentile, tier |
| `shiller_cache.csv` | *(created by the bot)* Saved copy of Shiller's monthly data |

`cape_history.csv` builds up a daily CAPE record you can open in Excel or Google Sheets.

## Settings

All at the top of `cape_bot.py`:

| Setting | Default | Meaning |
|---|---|---|
| `WINDOW_YEARS` | `20` | How many years of history the percentiles use |
| `ALERT_WHEN_RISING_INTO` | `{3, 4}` | Rising tiers that also email (3 = Expensive, 4 = Very expensive) |
| `CONFIRM_RUNS` | `2` | Runs a new tier must hold before it counts |
| `FAIL_ALERT_AFTER` | `3` | Failed price fetches in a row before a warning |
| `SHILLER_REFRESH_DAYS` | `7` | How often to re-download Shiller's data |
| `SHILLER_STALE_DAYS` | `100` | Warn if Shiller's latest month is older than this |

## Testing without sending email

Run locally with `DRY_RUN=1` to print emails instead of sending them:

```bash
pip install -r requirements.txt
DRY_RUN=1 python cape_bot.py
```

Delete `state.json` afterwards if you don't want the local test to count as the first run.

---

## Troubleshooting

**"Shiller's data is out of date" email**
The bot couldn't get current data (the old Yale file stopped updating in 2023), so it has
paused tiers. Go to shillerdata.com, right-click the **ie_data** download button, copy the
link, and save it as the `SHILLER_URL` secret. Run the workflow again; you'll get the
"bot is live" email once it works.

**"No S&P 500 price from yfinance"**
Usually a temporary Yahoo problem; it often fixes itself. If it keeps happening, update
yfinance (`yfinance>=` in `requirements.txt`).

**The "Commit state and history" step fails**
Check step 3 of Setup (workflow permissions must be Read and write). The step already
retries if you edited the repo while the bot was running.

**Emails**
Each run sends **at most one email**. If several things happen in one run, they're
combined into that one email.

**No email arrives**
Check the Actions log for the run. For Gmail, make sure `SMTP_PASS` is an App Password.

---

## Things to keep in mind

- **The live CAPE is an estimate.** Shiller's earnings lag a few months, and the price
  uses latest-available inflation data. It will be close to, but not exactly match,
  sites like multpl.com.
- **"Cheap" is relative to the last 20 years.** CAPE has been high for most of that
  period, so a "Cheap" reading here can still be well above the long-run average
  (around 17). That's by design: a 145-year window would almost never call anything cheap.
- **Cut-offs move.** Because the 20-year window rolls forward, the CAPE number for each
  tier changes slowly over time. Each email shows the current cut-offs.
- **This is a valuation signal, not a timing tool.** Markets can stay expensive or cheap
  for years. CAPE is better at hinting at long-term returns than short-term moves.

Data: Robert Shiller, *Irrational Exuberance* dataset (shillerdata.com); prices via Yahoo Finance (yfinance).
