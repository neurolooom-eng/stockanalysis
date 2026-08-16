# CLAUDE.md

Context for anyone (or any Claude session) picking this project up.

## What this is

Pivot Desk — an intraday signal dashboard for NSE/BSE stocks, built for personal
use by two people. Scores each watched stock on Camarilla pivots, moving averages
and session VWAP, and reports BUY / SELL / HOLD with the reasoning shown.
Messages both phones over Telegram when a signal changes.

Repo: https://github.com/neurolooom-eng/stockanalysis

## Owner context

- Runs **Windows**. Give PowerShell commands, never `source venv/bin/activate`.
- Not a developer. Write the files directly; don't hand over code to copy-paste,
  and don't bury code inside documentation. This was the single biggest source of
  friction in the first build.
- Prefers requirements confirmed before code is written.
- Values accuracy over reassurance — flag uncertainty, don't invent APIs or
  numbers, say when something needs verifying against live docs.

## Architecture

Four files, deliberately. One folder, no subdirectories, no build step.

```
app.py            FastAPI: API + indicators + alert loop + login + serves the page
index.html        Whole frontend, vanilla JS, no framework
requirements.txt
run.bat           Windows launcher (venv, install, start, open browser)
render.yaml       Render blueprint, so hosting needs no form-filling
```

`app.py` serves `index.html` at `/`, so it's a single service to run and a single
service to deploy. Storage is SQLite (`profiles`, `watchlist`, `signal_log`,
`settings`, `users`). Data comes from yfinance.

Start: double-click `run.bat`, or `venv\Scripts\python.exe app.py`.

The database is **not** tracked in git (`.gitignore`). It holds live watchlists,
signal history and the Telegram bot token. It was committed once, early on; see
the README for the recovery command if a pull ever removes it.

## Decisions worth not re-litigating

- **Single service, no React.** An earlier version was React + FastAPI in two
  containers across 12 files. The owner could not get it installed. Simplicity
  beats architecture here. Don't reintroduce a build step without a strong reason.
- **Camarilla comes from the previous completed session's H/L/C**, not the
  current bar. Divisors are 1.1/2, 1.1/4, 1.1/6, 1.1/12. The first build got both
  of these wrong.
- **VWAP is cumulative within the current session and resets daily.** An earlier
  version used a 20-day rolling average and called it VWAP.
- **Moving averages run on intraday bars**, not days. MA_FAST/MA_SLOW are counts
  of bars at whatever `INTERVAL` is set to.
- **The score is additive and transparent** (three tests, −3..+3, reasons
  returned to the UI). Don't replace it with an opaque confidence number.
- **Symbols carry an exchange**: NSE → `.NS`, BSE → `.BO`.
- **`signal_log` records changes only**, never one row per refresh. The dashboard
  polls and the alert loop sweeps; logging every pass would bury the real
  transitions and make the hit rate meaningless. This is also what makes "alert
  on change" and "history" the same mechanism — `score_profile()` serves both.
- **Caps are settings, not constants.** Watchlists (10) and stocks per watchlist
  (30) are rows in `settings`, editable from the Settings page, enforced
  server-side. The env vars are starting values only.
- **A symbol's first sighting never alerts.** It is logged silently, so adding
  thirty stocks doesn't fire thirty messages.
- **The bot token is never returned by the API in full** — `/api/settings` sends
  back only the last four characters.
- **The login is a gate over shared data, by explicit decision.** Two seeded
  users (`pnk`, `kau`, both `123`), salted+hashed, signed HMAC cookie, everything
  under `/api/` closed except health/login/me. The owner asked for exactly this
  and moved real accounts to the backlog — don't gold-plate it unasked.
- **GitHub Pages cannot host this** (static only, no Python). The owner tried;
  the answer is a Python host, and `render.yaml` is committed for that.
- Fetches run in a thread pool; sequential fetching of 30 symbols was too slow.

## Known limitations

- Yahoo data is delayed and unofficial; it rate-limits and breaks periodically.
  Per-symbol failures surface on the card rather than failing the whole request.
- Alerts only run while `app.py` is running. On a host that sleeps when idle,
  they are not dependable.
- Exchange holidays are not modelled — the market-hours check is weekday plus
  09:15–15:30 IST only.
- No authentication. Anyone with the URL can see and edit everything.
- Hit rate is measured at bar closes on delayed data with no costs modelled. It
  describes what the score did; it is not a backtest.

## Next steps, in the owner's priority order

1. **Proper accounts** (owner's own backlog item): password-change screen,
   per-user private watchlists, real passwords, rate-limited sign-in.
2. **Broker feed** (Zerodha Kite / Angel One / Dhan) for genuinely live prices.
   Only `analyse()` needs to change.
3. **Holiday calendar** for the alert loop.
4. **Alert thresholds** — signal types or score levels worth messaging about.

## House rules

- Test before claiming something works. Indicator maths should be checked against
  hand-worked values.
- Don't state token counts, build times, or other invented metrics.
- Signals are arithmetic on past prices. Never describe them as predictions and
  never phrase output as financial advice.

## Testing notes

Yahoo Finance is unreachable from Claude Code's sandbox (the proxy returns 403 on
CONNECT), so `analyse()` cannot be exercised against live data there. Test the
indicator maths directly with hand-worked values, and test the API by monkey-
patching `analyse` and `send_telegram` with `fastapi.testclient.TestClient`.
Use `TestClient` as a context manager or the alert loop's lifespan never starts.
