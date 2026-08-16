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
- **Auto** re-runs the scoring on a timer (30s / 1m / 2m / 5m) while the tab is
  open. It pauses when the tab is hidden and catches up when you come back, so a
  forgotten tab doesn't sit there hammering Yahoo.

Three views along the top: **Signals**, **History** and **Settings**.

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

## Signing in

Two accounts ship with the app:

| User | Password |
|---|---|
| `pnk` | `123` |
| `kau` | `123` |

They are created the first time the app starts with an empty database, and the
passwords are salted and hashed rather than kept as typed. The sign-in lasts 30
days per device; **sign out** is next to the watchlist tabs.

This is a **gate, not an identity system.** Both accounts see the same
watchlists and the same settings. There is no password-change screen, no
lockout after repeated guesses, and `123` is a password in name only — change it
before this is reachable from the open internet by anything you care about.
Proper accounts with private watchlists are the backlog item below.

To change a password today, delete the `users` row and restart with the seed
values edited in `app.py` (`SEED_USERS`), or ask for the password-change screen
to be built.

---

## Telegram alerts

Open **Settings** in the app — nothing here needs a file edited.

1. On Telegram, message **@BotFather**, send `/newbot`, follow the two prompts.
   It replies with a token like `123456789:AAE...`. Paste it into **Bot token**.
2. Message your new bot once from each phone that should receive alerts (a bot
   cannot start a conversation with you).
3. Open `https://api.telegram.org/bot<your-token>/getUpdates` in a browser and
   read off `"chat":{"id":...}` for each phone. Put those numbers, comma
   separated, into **Chat IDs**.
4. Press **Save**, then **Send test message**. If both phones buzz, you're done —
   tick **Alerts on**.

The background checker then re-scores every alert-enabled watchlist on your
chosen interval and messages you **only when a stock's signal changes** —
HOLD → BUY, BUY → SELL and so on. A stock sitting on BUY does not message you
every five minutes, and a symbol seen for the first time is recorded silently so
adding thirty stocks doesn't fire thirty messages.

Other controls there:

- **Only during market hours** — restricts checks to 09:15–15:30 IST, Mon–Fri.
  Exchange holidays aren't modelled; on a holiday there are no new bars, so
  nothing changes and nothing is sent.
- **Alert these watchlists** — per-watchlist on/off, so a long-term list doesn't
  ping you all day.
- **Check now** — forces one sweep immediately, ignoring the schedule and the
  market-hours setting. The honest way to prove the whole path works.
- The status block at the bottom reports whether the checker is running, when it
  last ran, and the last error Telegram returned.

The token is stored in the database on your machine and is never sent back to
the page in full — only the last four characters, so you can tell which one is
saved.

---

## History and hit rate

**History** lists every time a stock's signal changed on that watchlist, and what
the price was doing when that signal was replaced. The tiles at the top give the
share of BUY signals where price was higher at the next change, and of SELL
signals where it was lower.

A row is written **only when a signal actually changes**, so the table is a list
of real transitions rather than one row per refresh.

Read the percentages carefully. They are computed on delayed Yahoo prices, at
bar closes you could not actually have traded, with no brokerage, taxes or
slippage, and HOLD is not scored at all. A dozen signals tells you nothing.
It is a sanity check on what the score has been doing, not a backtest, and
certainly not evidence it will keep doing it.

---

## Caps

Set in **Settings → Caps**, stored in the database, changeable whenever you like:

| Cap | Default |
|---|---|
| Watchlists | 10 |
| Stocks per watchlist | 30 |

Both are enforced by the server, not just hidden in the page. Raising the
per-watchlist cap makes each refresh slower and makes Yahoo more likely to
rate-limit you — 30 is a comfortable ceiling, not an arbitrary one.

---

## Settings (environment variables)

Set these before starting if you want to change them. Everything in the Settings
page is stored in the database instead and needs no restart.

| Variable | Default | Notes |
|---|---|---|
| `INTERVAL` | `5m` | Bar size. `1m` `2m` `5m` `15m` `30m` `60m` |
| `MA_FAST` / `MA_SLOW` | `20` / `50` | In bars, not days |
| `CACHE_TTL` | `60` | Seconds before re-fetching a symbol |
| `PORT` | `8000` | |
| `DB_PATH` | next to `app.py` | Point at a mounted volume when hosting |
| `MAX_PROFILES` / `MAX_STOCKS` | `10` / `30` | Starting caps only — the Settings page wins once saved |

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

### Back up your database before your next pull

`stock_monitor.db` used to be committed to this repo and is now deliberately
untracked (it holds your watchlists, your signal history and your Telegram bot
token — none of which belong on GitHub). Because git had been tracking it,
**pulling the commit that untracks it will delete your local copy.** Take a copy
first:

```powershell
cd C:\Users\1234\Documents\GitHub\stockanalysis
copy stock_monitor.db stock_monitor_backup.db
git pull
copy stock_monitor_backup.db stock_monitor.db
```

If you pull before reading this, the file is still in git history and can be
brought back:

```powershell
git show 413b1c6:stock_monitor.db > stock_monitor.db
```

From now on `.gitignore` keeps the database, `venv/`, `__pycache__/` and `*.zip`
out of every commit.

---

## Publishing it to the web

Because the server serves the page too, this deploys as **one** service, not two.

### GitHub Pages will not work

Pages serves static files only — there is no Python behind it. It would hand out
`index.html` and then every `/api/...` call would 404: no watchlists, no signals,
no settings, no alerts. Set **Settings → Pages → Source** to *None* so you don't
leave a broken page published. You need a host that runs Python.

### Render (this repo has a blueprint for it)

`render.yaml` is committed, so you don't have to fill in a form:

1. Sign in at <https://render.com> with your GitHub account.
2. **New → Blueprint**, pick `neurolooom-eng/stockanalysis`, choose this branch,
   **Apply**.
3. Wait for the first build. You get a URL like
   `https://pivot-desk.onrender.com` — open it on either phone and sign in.

To make alerts dependable, switch the service to a paid plan and uncomment the
`disk:` block and `DB_PATH` in `render.yaml` — see below for why. Set the
environment variable `HTTPS_ONLY=1` once you are on https, so the session cookie
is only ever sent over an encrypted connection.

Three things to know before relying on the free plan:

- **Free services sleep** after roughly 15 minutes idle. A sleeping service runs
  no alert loop, so alerts stop until someone opens the page, and the first
  request after a quiet spell takes 30–60 seconds to wake. Free hosting and
  reliable alerts are mutually exclusive.
- **The free disk is wiped on every redeploy**, taking watchlists, signal history
  and your bot token with it. A paid plan plus a persistent disk with `DB_PATH`
  pointed at it fixes this.
- **The hosted copy is a separate database** from the one on your PC. Watchlists
  do not carry across, and if both copies run with alerts on, you get every
  message twice — turn alerts off in one of them.

Pricing and free-tier behaviour change; check Render's current terms rather than
taking the above as gospel.

### Or just tunnel from your PC

If you only want to see it on your phone and your PC is on anyway (which alerts
require regardless), skip hosting entirely:

```powershell
winget install --id Cloudflare.cloudflared
cloudflared tunnel --url http://localhost:8000
```

It prints a public `https://…trycloudflare.com` address. No account, no deploy,
and it uses the database you already have. The address changes each restart.

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
- **Alerts depend on the server staying up.** The checker runs inside `app.py`,
  so alerts arrive only while the app is running — on a free host that sleeps
  when idle, that means not reliably. A machine that stays on, or a paid host,
  is what makes them dependable.
- **The login is a gate, not real account security.** Two shared accounts, one
  weak password each, no lockout, no password-change screen, and both users see
  the same data. It keeps a passer-by out of a public URL. It is not protection
  against anyone actually trying.
- **The signal is arithmetic on past prices.** It has no view on what a stock
  will do next, and a 3/3 score is not a strong claim about the future. Paper
  trade it for a few weeks and check the history before risking money.

---

## Sensible next steps

1. **Proper accounts** (backlog): password-change screen, per-user private
   watchlists, sensible passwords, and rate-limiting on the sign-in form. What
   exists today is a gate over shared data.
2. **Broker feed** (Zerodha Kite, Angel One, Dhan) for genuinely live prices.
   Only `analyse()` needs to change — everything downstream of it is
   source-agnostic.
3. **Exchange holiday calendar**, so the checker doesn't bother polling on days
   the market never opened.
4. **Alert thresholds** — e.g. only message on BUY/SELL, never on HOLD, or only
   above a score you choose.

Done since the first build: Telegram alerts, the history and hit-rate view,
configurable caps, auto-refresh, a sign-in gate, and a Render blueprint.
