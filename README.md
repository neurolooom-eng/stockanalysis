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

Six views along the top: **Signals**, **Buy list**, **Scanner**, **Strategy**,
**History** and **Settings**.

Cards show a **Since added** figure alongside the moving averages: growth
measured from the previous session's close on the day you added the stock,
frozen at that point. The percentage beside the price is only today's move; this
one is how the pick has done since you made it. Names added before this existed
have no reference and show nothing.

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
| Pivot | broken above the band | broken below the band |

Partial credit (±0.5) for weaker versions of each. **BUY** at +2 or above,
**SELL** at −2 or below, otherwise **HOLD**. "The band" is R3/S3 on Camarilla,
TC/BC on CPR and R1/S1 elsewhere — see *Choosing a pivot system* below.

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

Four accounts ship with the app:

| User | Password |
|---|---|
| `pnk` | `123` |
| `kau` | `123` |
| `kaushik` | `123` |
| `jana` | `123` |

Each one is created on startup if it isn't in the database already, so adding a
name to `SEED_USERS` in `app.py` gives you that account on the next restart —
you don't need a fresh database. Existing accounts are never touched, so a
password you have changed is not reset by a restart. Passwords are salted and
hashed rather than kept as typed. The sign-in lasts 30
days per device; **sign out** is next to the watchlist tabs.

This is a **gate, not an identity system.** Every account sees the same
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

## Choosing a pivot system

**Settings → Pivot system** picks the maths everything runs on. The Signals
cards, the Buy list, the Scanner and the chart all follow it.

| System | Central pivot | What it gives you |
|---|---|---|
| **Camarilla** (default) | (H+L+C)/3 | R1–R4 and S1–S4, tight around the close |
| **Classic** | (H+L+C)/3 | R1–R3 and S1–S3, spread much wider |
| **Fibonacci** | (H+L+C)/3 | Levels at 38.2%, 61.8% and 100% of the range |
| **Woodie** | (H+L+2C)/4 | Weights the previous close more heavily |
| **DeMark** | conditional | One level each side; branches on whether the session closed above or below its open |
| **CPR** | (H+L+C)/3 | Pivot with a TC/BC band around it |

All of them use the **previous completed session's** high, low and close (DeMark
also needs the open). Levels are named **R** and **S** throughout — Camarilla
calls them H and L internally, but Kite, Chartink and every trader say R3 and
S3, so that is what the app shows.

Two things change with the system:

- **The band** — the two levels the contraction test compares between bars.
  Camarilla uses R3/S3, CPR uses TC/BC, the rest use R1/S1.
- **The breakout levels** — what the Buy list triggers on. Camarilla R3 and R4,
  Classic and Fibonacci R2 and R3, Woodie R1 and R2, DeMark and CPR just the one.

### A warning about the Scanner

Your Chartink scan is **Camarilla** — its `0.275` is that system's R3/S3
coefficient. Switch to anything else and the Scanner still does the honest
thing (finds bands closing in on both sides), but **it stops matching
Chartink**. The Scanner page says so in amber whenever a non-Camarilla system is
selected, so you can't lose track of it by accident.

CPR is the interesting one to try. A *narrow* CPR is traditionally read as a
trending day coming and a wide one as sideways — the same instinct as the
contraction scan, measured a different way.

---

## Charts

Click any symbol — on a Signals card, in the Buy list, or in the Scanner — to
open a candlestick chart with your pivot levels drawn across it. **5m**, **15m**
and **Daily** are the available intervals, and **60 / 120 / 250** chooses how
many bars to draw. Both choices are remembered between sessions.

What 250 covers depends on the interval — an NSE session is 375 minutes, so it
is about three days of 5m bars, ten days of 15m, or a year of daily.

### Reading the x axis

Times run along the bottom: `HH:MM` on the intraday intervals, dates on daily.
On 5m and 15m a faint vertical rule marks each **session break**, and that tick
is labelled with the date rather than the time — without it a multi-day
intraday chart is an undifferentiated wall of candles. Labels are dropped where
they would collide rather than overprinting each other.

### When it switches to a line

At 250 bars the candles would be about two pixels wide, which is a smear rather
than a chart, and there is no zoom to recover from it. Past that point the chart
draws a single line through the closes instead, and the caption says so. The
switch is decided on the actual drawn width, so it adapts if the panel size ever
changes.

The levels come from the previous completed session whatever interval you pick,
so the lines don't move as you switch timeframe. That is how pivots work and how
Kite plots them. Solid lines are the band, dashed are the rest; where levels sit
too close to label legibly the line is still drawn but the label is dropped, band
and PP first.

This is drawn as plain SVG with no charting library, deliberately — the app is
four files with no build step, and a CDN or npm dependency would end that. So it
is a chart for seeing where price sits against your levels, not a trading
terminal. No drawing tools, no pan and zoom, and **Yahoo's data is delayed**, so
it will not tick along with Kite.

---

## Knowing which version you are on

The footer shows a build stamp:

```
Build 341890b · code 20 Aug, 09:00 · running since 20 Aug, 09:02
```

- **Build** is the git commit when it can find one. On Render that comes from
  the deploy itself, so it tells you exactly which commit is live.
- **code** is when `app.py` last changed.
- **running since** is when the server started.

If you have just pushed a change and the build stamp hasn't moved, you are
looking at an old deploy or a cached page — hard-refresh with `Ctrl+F5`. It is
visible before you sign in, so you can check it without logging in.

---

## Buy list

The **Buy list** view screens the whole F&O list for stocks trading **above R3**,
and flags the stronger ones **above R4**.

R3 and R4 are the Camarilla H3 and H4. They come from the **previous completed
session**, which is the same convention the Signals cards use — so the two views
can never disagree about where a level sits.

This is **not** filtered to contracting stocks. Any F&O name above R3 appears.
Names that are *also* contracting get a **coiled** chip, because a break out of a
coil is the more interesting of the two.

**The list is sorted by how much room is left to R4** — furthest still to run at
the top. Names already through R4 have no room left to measure and fall to the
bottom.

That is deliberately not the same as sorting by how recently a stock broke.
The R3→R4 gap is proportional to the previous day's range, so it differs from
stock to stock: a name barely over R3 on a narrow-range stock can have *less*
room ahead of it than one further above R3 on a wide-range stock. Room is the
one that answers "how much is left in this move".

Each row shows the price, which level it cleared and at what price, how far above
it is, both R3 and R4 for context, and the stock's average turnover.

### Checking it against your broker

Kite plots the identical levels — Indicators → **Pivot Points Standard**, then
set the type to **Camarilla**. Zerodha uses the same coefficients this app does
(0.55, 0.275, 0.183, 0.0916, anchored on the previous day's H/L/C), and `0.275`
is the same number in the Chartink filter below.

Expect *near* matches rather than exact ones: this app reads Yahoo, Kite reads
the exchange feed, and a few paise of difference in yesterday's high shifts every
level slightly. A large divergence means a data problem, not a formula problem.

### The three groups

Chips above the table split the list:

| Group | What it is |
|---|---|
| **All** | Everything above the first breakout level |
| **Broke R3, not R4** | Cleared the first level but not the second |
| **Above R4** | Through both |

The middle one is usually the interesting group: the break has happened, and the
next level is still ahead as an obvious first target. The **Room to R4** column
says how far that is from the current price.

Those two columns read against each other. A stock **+0.09% above R3** with
**+5.11% room to R4** has only just broken and has the whole move ahead of it.
One **+5.12% above R3** with **+0.09% room** has already travelled and is about
to run into the next level instead.

Once a stock is through R4 there is no level above left to measure, so the
column reads *past it*.

On DeMark and CPR there is only one breakout level, so no groups are shown —
there is no "broke one but not the other" to separate.

### Today vs since flagged

Two growth columns, and they answer different questions.

**Today** is the move against the previous session's close. It resets every
morning.

**Since flagged** is measured from the close of the day *before the stock first
appeared on this list*, and keeps counting for as long as it stays on. On the
first sighting the two are identical; from the next day they diverge, and the
gap is what the signal has actually produced since it fired.

The reference is frozen when a name first appears, so a rescan does not move it.
If a stock drops back below the trigger level, the reference is cleared — so if
it breaks out again next week the clock starts again rather than reporting
growth from a run that already ended.

A stock whose price failed to load keeps its reference. No data is not the same
thing as "no longer a buy", and a transient Yahoo hiccup shouldn't lose the
history.

### Turning the Buy list into a watchlist

**Add to watchlist** puts the whole list onto a watchlist in one click, so the
score, the Telegram alerts and the history start tracking those names.

**It adds whichever group you have selected**, so what you see is what goes on.
The dropdown next to it chooses where they go. The default makes a new,
date-stamped watchlist (`Buy list 20 Aug 14:35`); pick an existing one to add
them there instead.

It is safe to press twice. Names already on the watchlist are skipped and
reported rather than duplicated, and nothing already there is removed. If the
Buy list is longer than a watchlist can hold, the names go on **in the order
shown** — freshest breaks first — and the ones that didn't fit are named in the
message rather than dropped silently. Raise the cap in Settings if you want all
of them.

SELL and HOLD lists are backlog items — see below.

---

## Contraction scanner

The **Scanner** view screens the whole NSE F&O list for stocks whose Camarilla
band has **contracted** — the classic coil before a move.

### What "contraction" means here

This is the Chartink scan, written out. Chartink's filter reads:

```
0.275 * (prevH - prevL) + prevC   >   0.275 * (H - L) + C
prevC - 0.275 * (prevH - prevL)   <   C - 0.275 * (H - L)
```

`0.275` is `1.1/4` — the Camarilla H3 and L3 coefficient. So the first line says
*this bar's H3 is lower than the previous bar's H3*, and the second says *this
bar's L3 is higher than the previous bar's L3*. Put together:

> The H3–L3 band has closed in on **both** sides, and now sits entirely inside
> the previous bar's band.

That is a different test from the one the Breakout strategy uses below, which
asks only whether the band is *narrow* as a percentage of price. A stock can sit
permanently narrow without ever contracting, and a wide-range stock can contract
hard. Both numbers are shown; the Scanner selects on contraction.

### The three timeframes

| Tab | What it means |
|---|---|
| **Daily** | The list for the **next session**. Levels come from the last completed daily bar, which is exactly what tomorrow trades against. Run it after 15:30 IST. |
| **15 min** | Coils forming inside today's session. |
| **5 min** | The same, finer — more hits, more noise. |

A bar that is still forming is never used. While the market is open the latest
bar's high, low and close are still moving, and a contraction test against half a
bar flips back and forth all day. Once the market closes, every bar counts.

### How much history it looks at

The contraction test itself uses **two bars** — the last completed one and the
one before it. That is all your Chartink filter compares, and nothing older can
make a stock contract or not.

The *rank* looks back further:

| Component | Lookback |
|---|---|
| Contraction test | **2 bars** |
| Contracting streak | as far back as it keeps contracting |
| Volume dry-up | 20 bars |
| NR4 / NR7 | 4 and 7 bars |
| 20-bar average | 20 bars |
| Turnover filter | 20 days |

Six months of daily bars are fetched (one month for 15m and 5m), but only as
headroom so the 20-bar figures exist. The decision is still two bars.

### The rank

Every hit is a genuine contraction. The rank says how *interesting* one is, and
it is additive and fully itemised — each point is listed with its reason, so a
high score can be argued with rather than taken on trust. Same idea as the
BUY/SELL/HOLD score.

| Points | Rule |
|---|---|
| +1 / +0.5 | Band ≥25% / ≥10% tighter than the previous bar |
| +1 / +0.5 | Contracting 3+ bars / 2 bars in a row |
| +1 | Volume dried up below 80% of its average |
| +1 / +0.5 | Narrowest range of the last 7 (NR7) / last 4 (NR4) bars |
| +0.5 | Holding above its 20-bar average |

Maximum 4.5. Liquidity is a filter rather than a score: anything trading under
the **Min turnover** setting (default ₹50 crore/day, averaged over 20 days) is
dropped, because a breakout in a thin name is hard to actually get filled on.

**The rank measures how tightly a stock has coiled. It says nothing about which
way it will break.**

### The F&O list

The ~190 NSE derivatives symbols are **typed into `app.py`, not fetched.** NSE
publishes the official list and revises it every few months — names get added,
dropped, and renamed after mergers — and nothing in the app calls nseindia.com to
refresh it. Check it against NSE's own list now and then, and paste corrections
into *Settings → Contraction scanner → My own list*. A symbol that no longer
exists simply fails its own fetch and gets reported; it doesn't break the scan.

Symbols are fetched about 40 per Yahoo request rather than one at a time, which
is what makes a 190-name scan practical at all.

---

## Breakout strategy

The **Strategy** view runs a rule set instead of a score: find coiled stocks,
wait for the break, then manage the position.

**It alerts. It does not place orders.** There is no broker connection — you get
a Telegram message saying a level went, and you place the trade yourself.

1. **Scan.** *Run scan* measures each symbol's Camarilla band — H3 minus L3 as a
   percentage of price — and lists everything under your threshold (default
   1.5%). A narrow band means the stock coiled up inside yesterday's range. The
   levels come from the previous session, so the scan doesn't change during the
   day; run it once before the open.
2. **Entry.** A candidate triggers when price gets **0.1% above H3**, *or* when a
   **5-minute candle closes above H3** — whichever happens first, configurable to
   either test alone. The level (H3 or H4) is a setting.
3. **Stop.** Placed **0.3% below the trigger level**, not below your entry, so the
   stop sits where the breakout would be proved wrong.
4. **Target.** **+1.5% above entry.**
5. **Trailing stop, in steps.** Default `0.5:0, 1.0:0.5, 1.5:1.0` — read as "at
   +0.5% move the stop to entry; at +1% move it to entry +0.5%; at +1.5% move it
   to entry +1%". Edit the pairs to change the ladder.

Every one of those numbers is a field in **Settings → Breakout strategy**,
including the symbol list the scan runs over. Each symbol is one Yahoo request
per scan, so a list of hundreds will be slow and may get rate-limited.

Alerts fire on entry, on each trail step, and on target or stop. The Strategy
view keeps the open positions and a journal of closed ones, with how far each
moved.

**What the journal is not.** Entries are recorded at the price the app saw when
it noticed the trigger — on data delayed by roughly 15 minutes, through a poll
that runs every few minutes. Your fills will differ, and with a 0.1% trigger and
a 0.3% stop that difference is larger than the edge being measured. Until there
is a broker feed, treat this as a record of what the rules said, not what you
would have made.

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
- **The F&O list is hand-maintained.** It is typed into the code, not fetched
  from NSE, so it drifts out of date as NSE revises the segment. If a name you
  expect never appears, check it is still in the list and still spelled the way
  NSE spells it.
- **The scanner has no unattended mode yet.** *Sync now* is a button you press.
  Scheduled 15-minute refreshes and alerts on new contractions are Phase 2.

---

## Sensible next steps

1. **Scanner Phase 2** (backlog): let the scanner run itself — re-scan every 15
   minutes and send a Telegram message when a *new* name starts contracting,
   instead of pressing *Sync now*. The blocker is hosting rather than the code:
   a free host that sleeps when idle can't deliver 15-minute alerts reliably.
2. **SELL and HOLD lists** (backlog): the mirror of the Buy list — F&O stocks
   that have broken *below* L3 and L4 — plus a HOLD view for names sitting
   inside the band. Same code with the levels flipped.
3. **Proper accounts** (backlog): password-change screen, per-user private
   watchlists, sensible passwords, and rate-limiting on the sign-in form. What
   exists today is a gate over shared data.
4. **Broker feed** (Zerodha Kite, Angel One, Dhan) for genuinely live prices.
   Only `analyse()` needs to change — everything downstream of it is
   source-agnostic.
5. **Exchange holiday calendar**, so the checker doesn't bother polling on days
   the market never opened.
6. **Alert thresholds** — e.g. only message on BUY/SELL, never on HOLD, or only
   above a score you choose.
7. **Refresh the F&O list** against NSE's own published list now and then — it
   is typed into the code and drifts as NSE revises the segment.

Done since the first build: Telegram alerts, the history and hit-rate view,
configurable caps, auto-refresh, a sign-in gate, and a Render blueprint.
