# XAUUSD desk — high-selectivity gold signal engine

Runs every 10 minutes on GitHub Actions (Mon–Fri, 09:00–02:00 Dubai). Each run:

1. Pulls free data: COMEX gold futures (Yahoo `GC=F`, M5 + H1), and optionally spot XAU/USD from Twelve Data.
2. Builds H4 → H1 → M15 → M5 using only the approved indicators (EMA 20/50/200, VWAP, ATR14, ADX14, RSI 7/14, relative volume).
3. Maps market structure (HH/HL/LH/LL, BOS, CHOCH) and liquidity (PDH/PDL, Asian and London ranges, unswept swings, equal highs/lows), and detects valid sweeps.
4. Builds a volume profile (today's developing POC/VAH/VAL, yesterday's value area, naked POCs from the last 5 days) and estimates order flow (per-bar delta, session cumulative delta, delta divergence and absorption at sweeps).
5. Checks the Forex Factory calendar for high-impact USD events.
6. Applies the hard gates (stale data, news, H4/H1 conflict, chop, risk limits), then the 100-point setup score.
7. Writes `docs/data/signal.json` and `docs/data/journal.json`, and sends a WhatsApp alert on any LONG or SHORT, and when a signal closes.

The dashboard at `docs/index.html` reads those files and refreshes every minute.

## Setup (about 15 minutes)

1. **Create a public repo** on GitHub named `gold-agent`. Public repos get unlimited Actions minutes. A private repo's 2,000 free minutes would run out at this schedule, so if you go private, change the cron in `.github/workflows/gold-agent.yml` to `*/20`.
2. **Upload the files.** Drag the unzipped folder's contents into the repo page. On a Mac, the `.github` folder is hidden: press `Cmd + Shift + .` in Finder to show it, and make sure it uploads, or the schedule never runs.
3. **Allow the workflow to save results:** Settings → Actions → General → Workflow permissions → *Read and write permissions* → Save.
4. **Turn on the website:** Settings → Pages → Source *Deploy from a branch* → Branch `main`, folder `/docs` → Save.
5. **Add optional secrets** (Settings → Secrets and variables → Actions → New repository secret):

   | Secret | What it does |
   |---|---|
   | `TWELVE_DATA_KEY` | Free key from twelvedata.com. Switches prices to spot XAU/USD so levels match your broker more closely. Uses about 200 of the 800 free daily calls. |
   | `ACCOUNT_EQUITY` | e.g. `10000`. Enables lot sizing (equity × 0.5% ÷ stop distance × 100 oz). Kept as a secret so it isn't public. |
   | `CALLMEBOT_PHONE` | Your WhatsApp number in international format, e.g. `+9715XXXXXXXX` |
   | `CALLMEBOT_APIKEY` | The key CallMeBot sends you on WhatsApp (see callmebot.com for the activation number and message) |

6. **First run:** Actions tab → *Gold agent* → *Run workflow*. When it turns green, open `https://<your-username>.github.io/gold-agent/`.

## Volume profile and order flow

Explicitly authorised additions to the approved indicator list.

**Score weights (total still 100):** structure 25, liquidity 20, momentum 12 (was 15), VWAP 8 (was 10), volume & flow 15 (was volume 10), volatility 5, session 5, news 10.

Volume & flow points:
- Relative volume on the setup leg: up to 5
- Profile context: up to 5. Full marks go to responsive trades (a sweep at the value-area edge) or initiative trades (acceptance outside yesterday's value). One point if you'd be buying right under a POC/VAH or selling right above a POC/VAL.
- Estimated delta: up to 5 (setup-leg delta with the trade, session delta rising/falling with it, divergence or absorption at the sweep)

New rules:
- Prior VAH/VAL/POC and naked POCs count as liquidity targets, so TP2 can be a profile level and the 1.5 R:R check applies to it.
- If the setup-leg delta clearly opposes the trade (beyond ±0.15), the decision becomes WAIT even when everything else passes.

**What "estimated" means.** Real footprint/delta data needs tick data with the aggressor side (bid vs ask), which no free gold feed provides. Here, each M5 bar's COMEX volume is assigned to buyers or sellers by where the bar closed in its range. It tracks real delta loosely and can be wrong on individual bars, so it only contributes points and a WAIT veto; it never creates a trade. The profile spreads each bar's volume evenly across its high–low range, which is a standard approximation of a tick-built profile. Without volume, the profile falls back to a time-based (TPO) profile and delta is switched off, which makes 85+ scores very rare.

`profile_bin` in `config.json` sets the profile row size in dollars (default 0.50).

## Settings you can change (`config.json`)

| Key | Default | Meaning |
|---|---|---|
| `risk_pct` | 0.5 | Risk per trade, % of equity |
| `min_trade_score` | 85 | Minimum score for a live LONG/SHORT |
| `stale_minutes` | 20 | Older data → NO TRADE |
| `news_block_before_min` / `after_min` | 45 / 20 | No new scalp in this window around high-impact USD news |
| `max_signals_per_session` | 3 | Signal cap per ~5 hours |
| `cooldown_minutes` | 30 | Gap between signals |
| `signal_expiry_bars` | 48 | Journal closes an unresolved signal after 4 hours |
| `profile_bin` | 0.5 | Volume profile row size in $ |

## Honest limitations

- **Delay.** Yahoo futures data is delayed (often around 10 minutes), and GitHub may start scheduled runs several minutes late. The engine refuses to signal on data older than `stale_minutes`, and the dashboard blanks any plan that has since gone stale. Treat signals as alerts to check your own chart, not as instant execution orders.
- **Futures vs spot.** Without a Twelve Data key, prices are COMEX futures, which trade above spot. Read the setup from the dashboard, then take actual entry, stop and target levels from your broker's chart.
- **Spread is unknown.** Free data has no broker spread. Check it before entering.
- **Volume** comes from COMEX futures (as your rules prefer), mapped onto spot bars when spot is used.
- **Unvalidated.** The rules are coded faithfully, but they have not been backtested on real data. The journal tracks every signal's outcome (win rate, expectancy, profit factor, drawdown, by session/score/direction). Judge the system only after 30+ closed signals, and ideally run a proper walk-forward backtest first.
- **Journal resolution is conservative.** If a single M5 bar touches both stop and target, it counts as a loss.
- GitHub Pages allows about 10 site rebuilds per hour, so don't schedule runs more often than every 10 minutes.

Decision support only, not financial advice.
