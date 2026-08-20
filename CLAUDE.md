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
`settings`, `users`, `candidates`, `trades`, `scan_hits`, `breakouts`). Data
comes from yfinance.

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
- **Six pivot systems, one selected in settings** (`pivot_system`): Camarilla,
  Classic, Fibonacci, Woodie, DeMark, CPR. Everything follows it — the score,
  the Scanner, the Buy list, the chart. The owner explicitly chose "everything,
  Scanner included" after being told it breaks the Chartink match, so don't
  quietly narrow the scope back.
- **Each system declares its own `band` and `breakout` levels** in
  `PIVOT_SYSTEMS`, so nothing downstream hardcodes H3/L3 any more. The band is
  what the contraction test compares (Camarilla R3/S3, CPR TC/BC, others
  R1/S1); breakout is what the Buy list triggers on. Add a system by adding a
  formula plus that metadata — don't special-case it in the consumers.
- **Levels are displayed as R and S, always** (`pretty_level()`). Camarilla's
  internal keys are h1-h4/l1-l4, but Kite, Chartink and the owner all say R3
  and S3. Internal keys stay as they are; only the display name changes.
- **The pivot system is part of the `_cache` key**, and switching it clears the
  cache. Without that, changing system serves back quotes scored on the old
  levels.
- **The Scanner shows an amber warning when not on Camarilla**, because that is
  the moment it stops matching the owner's Chartink scan. Don't remove it.
- **The chart is hand-drawn SVG, no charting library.** Same reason as no React:
  four files, no build step. It also means no drawing tools or pan/zoom, which
  is an accepted trade, not an oversight. Levels are drawn from the previous
  session regardless of the bar interval, so lines don't move between
  timeframes. Labels are suppressed (not the lines) where they would collide,
  band and PP claiming slots first.
- **The footer carries a build stamp** (`build_info()`): commit from
  `RENDER_GIT_COMMIT` on Render, else `git rev-parse`, else app.py's mtime. It
  is served from `/api/health`, which is outside the login gate on purpose, so
  the owner can check which version is live without signing in.
- **The Buy list groups are a slice of the same data, not a second scan.** Rows
  are tagged with the highest breakout level price has cleared, so "broke R3
  but not R4" is just the rows tagged R3 — filtered client-side, no extra Yahoo
  requests. `Room to R4` is the distance to the next level up, which is the
  point of that group. The chips are hidden on DeMark and CPR, which have only
  one breakout level.
- **Buy list → watchlist adds the selected group**, not always the whole list —
  what is on screen is what goes on. `only_level` on the endpoint does the
  filtering server-side against the same stored rows.
- **Buy list → watchlist is additive, never destructive.** One click, but it
  skips names already there and reports them, keeps whatever else is on the
  target watchlist, and respects the per-watchlist cap — filling from the top
  of the list and naming what didn't fit. Don't "improve" it into a replace.
- **"Contraction" means the inside-band test, not a narrow band.** The Scanner
  selects stocks where this bar's H3 < the previous bar's H3 **and** this bar's
  L3 > the previous bar's L3 — the band has closed in on both sides and sits
  inside the previous one. This came from the owner's Chartink scan, whose
  `0.275` is just the Camarilla `1.1/4` coefficient. It is deliberately *not*
  the same test as the breakout strategy's `band_pct` (band width as a % of
  price); a stock can be permanently narrow without contracting. Don't collapse
  the two.
- **The scanner never reads a forming bar.** `completed_bars()` drops the last
  bar while the market is open. A contraction test against a half-built bar
  flips all day and would make the daily "next session" list meaningless.
- **The rank is additive and itemised**, like `score_signal()`. Depth, streak,
  volume dry-up, NR4/NR7 and trend each contribute a stated number of points
  with a reason attached. Liquidity is a *filter*, not a score. The rank
  measures how tightly a stock coiled and says nothing about direction — never
  present it as a buy signal.
- **The F&O universe is typed into `app.py`, not fetched.** nseindia.com is not
  reachable from the sandbox (403 at the proxy) and blocks scripted clients
  generally. The list drifts as NSE revises the segment, so it is editable from
  Settings and documented as needing periodic manual checking. Per-symbol
  failures are reported and don't fail the scan.
- **Symbols are fetched in batches**, ~40 per Yahoo request via
  `batch_history()`. The older one-request-per-symbol path is fine for 60 names
  and will get rate-limited at 190.
- **The breakout strategy is a separate mechanism from the score**, sharing only
  `analyse()`. Owner's spec, from a handwritten note: scan for a narrow H3−L3
  band, enter 0.1% above H3 *or* on a candle close above it, stop 0.3% below the
  *level* (not below entry), target +1.5%, stepped trail `0.5:0,1.0:0.5,1.5:1.0`.
  Owner explicitly chose the narrow-band definition, no broad-market filter, and
  the stepped trail over the alternatives — don't quietly substitute others.
- **It alerts, it never places orders.** No broker connection exists. The trade
  rows are a journal of what the rules said, at delayed prices; say so plainly
  rather than presenting them as fills or P&L.
- **Caps are settings, not constants.** Watchlists (10) and stocks per watchlist
  (30) are rows in `settings`, editable from the Settings page, enforced
  server-side. The env vars are starting values only.
- **`seed_users()` fills in missing accounts, one by one.** It used to bail if
  the table had any rows, which meant adding a name to `SEED_USERS` only worked
  on a brand new database. It now checks per user, so a new seed account
  appears on the next restart of an existing database. It never touches an
  existing row, so a changed password is not reset — but deleting a seeded user
  brings it back unless you also remove it from `SEED_USERS`.
- **A symbol's first sighting never alerts.** It is logged silently, so adding
  thirty stocks doesn't fire thirty messages.
- **The bot token is never returned by the API in full** — `/api/settings` sends
  back only the last four characters.
- **The login is a gate over shared data, by explicit decision.** Three seeded
  users (`pnk`, `kau`, `kaushik`, all `123`), salted+hashed, signed HMAC cookie, everything
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
- The login is a gate, not real account security: two shared accounts, weak
  passwords, no lockout, both users see the same data.
- Hit rate is measured at bar closes on delayed data with no costs modelled. It
  describes what the score did; it is not a backtest.

## Backlog

Not active work. The owner sets the order; don't start any of these unasked.

1. **Scanner Phase 2 — unattended scanning and alerts** (owner moved this to
   the backlog after Phase 1 shipped, to test the scanner by hand first).
   Agreed shape: re-run the intraday scan every 15 minutes and message on new
   contractions. It was written and then deliberately backed out, so the repo
   carries no dormant half-feature. When picking it up:
   - Alert on **newly appearing** symbols only. A stock that stays coiled must
     not message every 15 minutes — same reason `signal_log` records changes
     rather than every pass.
   - Keep the first-sighting-is-silent rule, or the very first run fires one
     message per hit.
   - Distinguish "no previous scan exists" from "previous scan lacked this
     symbol"; they are not the same and only the second should alert.
   - **Hosting is the real blocker**, not the code. A free host that sleeps
     when idle cannot do dependable 15-minute alerts.
2. **SELL and HOLD lists.** The owner asked for BUY first and explicitly moved
   these to the backlog. BUY is "any F&O name above R3 or R4"; SELL is the
   mirror (below L3 / L4) and HOLD is everything inside the band. Reuse
   `scan_breakouts()` with the levels flipped rather than writing a second
   scanner.
3. **Proper accounts** (owner's own backlog item): password-change screen,
   per-user private watchlists, real passwords, rate-limited sign-in.
4. **Broker feed** (Zerodha Kite / Angel One / Dhan) for genuinely live prices.
   Only `analyse()` needs to change.
5. **Holiday calendar** for the alert loop.
6. **Alert thresholds** — signal types or score levels worth messaging about.
7. **Refresh the F&O list** against NSE's published segment list. It is typed
   into `app.py` and drifts as NSE revises it.

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
