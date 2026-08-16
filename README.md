# Pivot Desk

Intraday signal dashboard for NSE and BSE stocks, scored on Camarilla pivots,
moving averages and session VWAP. Profiles hold up to 30 stocks each.

**Four files, one folder, no npm.** The Python server also serves the web page,
so there is one thing to run and one thing to deploy.

---

## Run it on Windows

1. Put all four files in one folder: `app.py`, `index.html`, `requirements.txt`, `run.bat`
2. Double-click **`run.bat`**

That's it. It creates the Python environment, installs what it needs, starts the
server and opens your browser at `http://localhost:8000`. Leave the black window
open while you use the app; press `Ctrl+C` in it to stop.

If you'd rather type the commands:

```powershell
cd C:\Users\1234\Documents\GitHub\stockanalysis
python -m venv venv
venv\Scripts\python.exe -m pip install -r requirements.txt
venv\Scripts\python.exe app.py
```

Note `venv\Scripts\...` — the `source venv/bin/activate` you tried earlier is the
Mac/Linux form, which is why PowerShell rejected it.

---

## Using it

- **+ Profile** creates a named watchlist. Each one is independent.
- Type any symbol and press Enter, or tap one of the suggested chips.
- Pick **NSE** or **BSE** — they're different tickers on Yahoo (`.NS` vs `.BO`)
  and the prices differ slightly.
- **Run signals** scores everything on the active watchlist.

Each card shows the pivot ladder with live price marked on it, and a breakdown of
exactly which conditions produced the score. Nothing is a black box — if a stock
reads BUY at +2.5, the card tells you which three tests it passed.

---

## How the score works

Three independent tests, each contributing to a score from −3 to +3:

| Test | +1 | −1 |
|---|---|---|
| Trend | price > MA20 > MA50 | price < MA20 < MA50 |
| Volume | price above session VWAP | price below session VWAP |
| Pivot | broken above H3 | broken below L3 |

Partial credit (±0.5) for weaker versions of each. **BUY** at +2 or above,
**SELL** at −2 or below, otherwise **HOLD**.

Camarilla levels come from the **previous completed session's** high/low/close,
which is how pivots are meant to be used — yesterday's range defines today's
levels. Formula used:

```
H4 = C + (H−L) × 1.1/2        L1 = C − (H−L) × 1.1/12
H3 = C + (H−L) × 1.1/4        L2 = C − (H−L) × 1.1/6
H2 = C + (H−L) × 1.1/6        L3 = C − (H−L) × 1.1/4
H1 = C + (H−L) × 1.1/12       L4 = C − (H−L) × 1.1/2
PP = (H + L + C) / 3
```

Minor variants of this formula circulate. Check it against whatever source you
normally use before you trade off it.

VWAP is cumulative **within the current session only** and resets each day, which
is what VWAP means. Moving averages run on intraday bars (default 5-minute), so
MA20 is roughly the last 100 minutes, not 20 days.

---

## Settings

Set these as environment variables before starting if you want to change them:

| Variable | Default | Notes |
|---|---|---|
| `INTERVAL` | `5m` | Bar size. `1m` `2m` `5m` `15m` `30m` `60m` |
| `MA_FAST` / `MA_SLOW` | `20` / `50` | In bars, not days |
| `CACHE_TTL` | `60` | Seconds before re-fetching a symbol |
| `PORT` | `8000` | |
| `DB_PATH` | next to `app.py` | Point at a mounted volume when hosting |

Yahoo restricts how far back intraday data goes — roughly 7 days for 1-minute
bars and 60 days for other intraday intervals. Worth confirming, since Yahoo
changes these limits without notice.

---

## Push to GitHub

Your earlier `git push` failed with *"src refspec main does not match any"*,
which means there were no commits yet — nothing to push. Full sequence:

```powershell
cd C:\Users\1234\Documents\GitHub\stockanalysis

git init
git remote add origin https://github.com/neurolooom-eng/stockanalysis.git
git add .
git commit -m "Pivot Desk: intraday signals on Camarilla, MA and VWAP"
git branch -M main
git push -u origin main
```

If `git remote add` says the remote already exists, use
`git remote set-url origin https://github.com/neurolooom-eng/stockanalysis.git`.

After the first push, updates are just:

```powershell
git add .
git commit -m "what changed"
git push
```

Add a `.gitignore` containing `venv/`, `__pycache__/` and `*.db` so you don't
commit your environment or your database.

---

## Hosting it

Because the server serves the page too, this deploys as **one** service, not two.

**Render (free tier, no card):** new Web Service → connect the repo →
Build `pip install -r requirements.txt` → Start `python app.py`. The app already
reads Render's `PORT`, so nothing else to configure.

Two things to know before you rely on it:

- **Free tiers use a disposable disk.** Your profiles and watchlists live in a
  SQLite file that gets wiped on every redeploy, and on Render's free plan when
  the service sleeps. Attach a persistent disk and set `DB_PATH` to it, or move
  to Postgres, if losing them would annoy you.
- **Free services sleep** after idling, so the first request after a quiet spell
  takes 30–60 seconds to wake.

---

## Honest limitations

- **Yahoo's data is delayed**, and I can't tell you precisely by how much for
  Indian exchanges — commonly cited as around 15 minutes, but verify it yourself
  against your broker's terminal. For genuinely live prices you need a broker
  feed (Zerodha Kite, Angel One, Dhan). The scoring code doesn't care where
  prices come from, so swapping the source later means rewriting one function.
- **Yahoo is unofficial.** It rate-limits, occasionally returns nothing, and
  yfinance breaks whenever Yahoo changes something. Failures show up per-stock on
  the card rather than taking down the whole page. If everything fails at once,
  run `pip install -U yfinance` first.
- **No alerts yet.** Mobile push needs either a browser tab left open or a
  server-side scheduler; Telegram is the easy version of this (see below).
- **No authentication.** Anyone with the URL sees and edits every profile. Fine
  for you and one friend on a URL you don't publish; not fine beyond that.
- **The signal is arithmetic on past prices.** It has no view on what a stock
  will do next, and a 3/3 score is not a strong claim about the future. Paper
  trade it for a few weeks and check the history before risking money.

---

## Sensible next steps

1. **Telegram alerts** — a bot token, a background loop, and a message when a
   stock's signal changes. Roughly 40 lines, no app store, works on both phones.
   Best value of anything on this list.
2. **Signal history view** — the `signal_log` table already records every score.
   Surfacing "how often did BUY signals actually go up" is what tells you whether
   any of this is worth trading.
3. **Broker feed** for real-time prices.
4. **Login** so you and your friend have separate, private profiles.
