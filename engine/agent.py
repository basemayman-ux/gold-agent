#!/usr/bin/env python3
"""XAUUSD high-selectivity signal engine.
Runs on GitHub Actions, writes docs/data/signal.json + journal.json for the dashboard.
It never fills in an entry unless every hard gate passes and the setup score clears the bar."""
import json
import math
import os
import re
import sys
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import requests

sys.path.insert(0, str(Path(__file__).parent))
from indicators import add_indicators  # noqa: E402
from structure import analyze_structure, find_sweep, mark_equal, unswept_swings  # noqa: E402
from orderflow import add_delta, flow_read, profile_context, profile_targets, profiles  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "data"
CFG = json.loads((ROOT / "config.json").read_text())
DUBAI = timezone(timedelta(hours=4))
FF_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
GOLD_KEYWORDS = ("FOMC", "Fed", "Powell", "CPI", "PCE", "Non-Farm", "NFP", "Employment", "GDP",
                 "Unemployment", "Federal Funds", "Retail Sales", "ISM", "PPI", "JOLTS")


# ───────────────────────────── data ─────────────────────────────

def _clean(df):
    df = df[["open", "high", "low", "close"] + (["volume"] if "volume" in df else [])].astype(float)
    df = df.dropna(subset=["close"])
    df = df[~df.index.duplicated(keep="last")].sort_index()
    return df


def fetch_yf(symbol, interval, period):
    import yfinance as yf
    df = yf.download(symbol, interval=interval, period=period, progress=False,
                     auto_adjust=False, prepost=True, threads=False)
    if df is None or df.empty:
        raise RuntimeError(f"yfinance returned no {interval} data for {symbol}")
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df.columns = [str(c).lower() for c in df.columns]
    df.index = pd.to_datetime(df.index, utc=True)
    return _clean(df)


def fetch_twelve(symbol, interval, size, key):
    r = requests.get("https://api.twelvedata.com/time_series", timeout=20, params={
        "symbol": symbol, "interval": interval, "outputsize": size, "timezone": "UTC", "apikey": key})
    js = r.json()
    if js.get("status") != "ok":
        raise RuntimeError(f"Twelve Data {interval}: {js.get('message', 'error')}")
    df = pd.DataFrame(js["values"])
    df.index = pd.to_datetime(df.pop("datetime"), utc=True)
    return _clean(df)


def live_price():
    """Latest spot price right now (not a closed candle). Twelve Data first, keyless gold-api.com as backup."""
    key = os.getenv("TWELVE_DATA_KEY", "").strip()
    if key:
        try:
            js = requests.get("https://api.twelvedata.com/price", timeout=8,
                              params={"symbol": CFG["spot_symbol"], "apikey": key}).json()
            if "price" in js:
                return float(js["price"]), "Twelve Data"
        except Exception:  # noqa: BLE001
            pass
    try:
        js = requests.get("https://api.gold-api.com/price/XAU", timeout=8).json()
        return float(js["price"]), "gold-api.com"
    except Exception:  # noqa: BLE001
        return None, None


def resample(df, rule):
    agg = {"open": "first", "high": "max", "low": "min", "close": "last"}
    if "volume" in df:
        agg["volume"] = "sum"
    return df.resample(rule, label="left", closed="left").agg(agg).dropna(subset=["close"])


def closed_only(df, minutes, now):
    """Drop the bar that is still forming."""
    if len(df) and df.index[-1] + pd.Timedelta(minutes=minutes) > now:
        return df.iloc[:-1]
    return df


def load_market(now):
    warnings = []
    fut5 = None
    try:
        fut5 = fetch_yf(CFG["futures_symbol"], "5m", "8d")
    except Exception as e:  # noqa: BLE001
        warnings.append(f"Futures 5m unavailable: {e}")

    key = os.getenv("TWELVE_DATA_KEY", "").strip()
    if key:
        try:
            m5 = fetch_twelve(CFG["spot_symbol"], "5min", 2000, key)
            h1 = fetch_twelve(CFG["spot_symbol"], "1h", 1500, key)
            if fut5 is not None and "volume" in fut5:
                m5["volume"] = fut5["volume"].reindex(m5.index, method="nearest",
                                                      tolerance=pd.Timedelta("4min"))
            source = "Spot XAU/USD (Twelve Data)"
            vol_source = "COMEX GC futures (Yahoo, delayed)" if fut5 is not None else "Not available"
            return finish(m5, h1, now, source, vol_source, warnings)
        except Exception as e:  # noqa: BLE001
            warnings.append(f"Twelve Data failed, falling back to futures: {e}")

    if fut5 is None:
        raise RuntimeError("No price source available")
    h1 = fetch_yf(CFG["futures_symbol"], "1h", "120d")
    source = "COMEX gold futures GC=F (Yahoo, delayed)"
    warnings.append("Futures trade at a premium to spot: levels will not match your broker's XAUUSD exactly.")
    return finish(fut5, h1, now, source, "COMEX GC futures (Yahoo)", warnings)


def finish(m5, h1, now, source, vol_source, warnings):
    m5 = closed_only(m5, 5, now)
    h1 = closed_only(h1, 60, now)
    m15 = closed_only(resample(m5, "15min"), 15, now)
    h4 = closed_only(resample(h1, "4h"), 240, now)
    return {"m5": m5, "m15": m15, "h1": h1, "h4": h4, "source": source,
            "volume_source": vol_source, "warnings": warnings}


# ───────────────────────────── context ─────────────────────────────

def session_of(now):
    h = now.hour + now.minute / 60
    if h < 7:
        return "Asia"
    if h < 12:
        return "London"
    if h < 16:
        return "London/NY overlap"
    if h < 21:
        return "New York"
    return "Late US / off-hours"


def market_closed(now):
    wd, h = now.weekday(), now.hour
    return wd == 5 or (wd == 6 and h < 22) or (wd == 4 and h >= 21)


def load_news(now):
    cache = OUT / "calendar.json"
    events, status = None, "unknown"
    try:
        r = requests.get(FF_URL, timeout=15, headers={"User-Agent": "gold-agent/1.0"})
        r.raise_for_status()
        events = r.json()
        cache.write_text(json.dumps({"fetched_at": now.isoformat(), "events": events}))
        status = "live"
    except Exception:  # noqa: BLE001
        if cache.exists():
            c = json.loads(cache.read_text())
            if now - datetime.fromisoformat(c["fetched_at"]) < timedelta(hours=12):
                events, status = c["events"], "cached"
    if events is None:
        return {"risk": "Unknown", "status": "unavailable", "upcoming": [], "detail": "Economic calendar unavailable"}

    relevant = []
    for ev in events:
        if ev.get("country") != "USD":
            continue
        impact = ev.get("impact", "")
        title = ev.get("title", "")
        key = any(k.lower() in title.lower() for k in GOLD_KEYWORDS)
        if impact == "High" or (impact == "Medium" and key):
            t = datetime.fromisoformat(ev["date"]).astimezone(timezone.utc)
            relevant.append({"title": title, "impact": impact, "time": t})

    before = timedelta(minutes=CFG["news_block_before_min"])
    after = timedelta(minutes=CFG["news_block_after_min"])
    risk, detail = "Low", "No high-impact USD event in the next 3 hours"
    for ev in sorted(relevant, key=lambda e: e["time"]):
        dt = ev["time"] - now
        if -after <= dt <= before:
            risk, detail = "High", f"{ev['title']} at {ev['time'].astimezone(DUBAI):%H:%M} Dubai"
            break
        if before < dt <= timedelta(hours=3) and risk == "Low":
            risk, detail = "Medium", f"{ev['title']} at {ev['time'].astimezone(DUBAI):%H:%M} Dubai"
    upcoming = [{"title": e["title"], "impact": e["impact"],
                 "time": e["time"].astimezone(DUBAI).strftime("%a %H:%M")}
                for e in sorted(relevant, key=lambda e: e["time"]) if e["time"] > now - after][:6]
    return {"risk": risk, "status": status, "upcoming": upcoming, "detail": detail}


def day_levels(m5, h1, now):
    """PDH/PDL, Asian and London ranges (UTC session clock)."""
    today = now.floor("D")
    levels = []
    prev = h1[h1.index < today]
    if len(prev):
        last_day = prev.index[-1].floor("D")
        d = prev[prev.index >= last_day]
        levels += [
            {"name": "Previous day high", "price": float(d["high"].max()), "side": "buy", "valid_from": today},
            {"name": "Previous day low", "price": float(d["low"].min()), "side": "sell", "valid_from": today},
        ]
    for name, a, b in (("Asian", 0, 7), ("London", 7, 12)):
        seg = m5[(m5.index >= today + timedelta(hours=a)) & (m5.index < today + timedelta(hours=b))]
        if len(seg) and now >= today + timedelta(hours=b):
            vf = today + timedelta(hours=b)
            levels += [
                {"name": f"{name} high", "price": float(seg["high"].max()), "side": "buy", "valid_from": vf},
                {"name": f"{name} low", "price": float(seg["low"].min()), "side": "sell", "valid_from": vf},
            ]
    return levels


def dedupe(levels, tol):
    """Same price seen on several timeframes counts once; keep the most significant name."""
    rank = ("Previous day", "Naked POC", "Prior", "London", "Asian", "H1", "M15")
    order = sorted(levels, key=lambda l: next((i for i, r in enumerate(rank) if l["name"].startswith(r)), 9))
    kept = []
    for l in order:
        if all(abs(l["price"] - k["price"]) > tol for k in kept):
            kept.append(l)
    return kept


def ema_bias(row):
    """Price and fast EMAs stacked: close > EMA20 > EMA50 is bullish, the reverse bearish."""
    if row["close"] > row["ema20"] > row["ema50"]:
        return "Bullish"
    if row["close"] < row["ema20"] < row["ema50"]:
        return "Bearish"
    return "Neutral"


def combine_bias(structure, emas):
    """Structure and EMAs must not disagree. If one is neutral the other decides; if they clash, neutral."""
    if structure == emas or emas == "Neutral":
        return structure
    if structure == "Neutral":
        return emas
    return "Neutral"


def h4_regime(h4, st):
    last = h4.iloc[-1]
    a = last["atr14"]
    above = h4["close"] > h4["ema200"]
    crosses = int((above != above.shift()).iloc[-30:].sum())
    if crosses >= 3:
        return "Neutral", f"Price crossed H4 EMA200 {crosses}× in 30 bars (transition)"
    gap = last["close"] - last["ema200"]
    if gap > 0.25 * a and st["bias"] != "Bearish":
        return "Bullish", "Price clearly above H4 EMA200"
    if gap < -0.25 * a and st["bias"] != "Bullish":
        return "Bearish", "Price clearly below H4 EMA200"
    return "Neutral", "H4 price near EMA200 or structure disagrees"


def vwap_crosses(m5, bars=24):
    side = np.sign(m5["close"] - m5["vwap"]).iloc[-bars:]
    return int((side != side.shift()).iloc[1:].sum())


# ───────────────────────────── journal ─────────────────────────────

def load_journal():
    p = OUT / "journal.json"
    return json.loads(p.read_text()) if p.exists() else {"signals": []}


def update_journal(journal, m5):
    """Standard management: take half at TP1 and move the stop to entry; the rest runs to TP2.
    Results in R: stop before TP1 = -1; TP1 then back to entry = +0.5; TP1 then TP2 = 0.5 + 0.5 x RR.
    A bar that touches both stop and target before TP1 counts as a loss (conservative)."""
    closed_now, tp1_now = [], []
    for s in journal["signals"]:
        # one-time rescore of signals closed under the old all-or-nothing method
        if s.get("status") == "closed" and s.get("mgmt") != "half_tp1_be":
            if s.get("tp1_hit") and s.get("result") == "Loss":
                s.update(result="TP1 then breakeven", r=0.5)
            elif s.get("tp1_hit") and s.get("result") == "Win":
                s.update(r=round(0.5 + 0.5 * s["rr"], 2))
            s["mgmt"] = "half_tp1_be"
            continue
        if s["status"] != "open":
            continue
        s["mgmt"] = "half_tp1_be"
        t0 = pd.Timestamp(s["time_utc"])
        bars = m5[m5.index > t0]
        R = abs(s["entry"] - s["sl"])
        long = s["direction"] == "LONG"
        for k, (ts, b) in enumerate(bars.iterrows()):
            hi, lo = b["high"], b["low"]
            if not s.get("tp1_hit"):
                if (lo <= s["sl"]) if long else (hi >= s["sl"]):
                    s.update(status="closed", result="Loss", r=-1.0, closed_utc=ts.isoformat())
                elif (hi >= s["tp1"]) if long else (lo <= s["tp1"]):
                    s["tp1_hit"] = True
                    s["tp1_utc"] = ts.isoformat()
                    tp1_now.append(s)
                    if (hi >= s["tp2"]) if long else (lo <= s["tp2"]):
                        s.update(status="closed", result="Win", r=round(0.5 + 0.5 * s["rr"], 2), closed_utc=ts.isoformat())
            else:
                if (hi >= s["tp2"]) if long else (lo <= s["tp2"]):
                    s.update(status="closed", result="Win", r=round(0.5 + 0.5 * s["rr"], 2), closed_utc=ts.isoformat())
                elif (lo <= s["entry"]) if long else (hi >= s["entry"]):
                    s.update(status="closed", result="TP1 then breakeven", r=0.5, closed_utc=ts.isoformat())
            if s["status"] == "open" and k + 1 >= CFG["signal_expiry_bars"]:
                move = ((b["close"] - s["entry"]) if long else (s["entry"] - b["close"])) / R
                r = 0.5 + 0.5 * move if s.get("tp1_hit") else move
                s.update(status="closed", result="Expired", r=round(float(r), 2), closed_utc=ts.isoformat())
            if s["status"] == "closed":
                closed_now.append(s)
                break
    return closed_now, tp1_now


def journal_stats(journal):
    done = [s for s in journal["signals"] if s["status"] == "closed"]
    if not done:
        return {"trades": 0}
    rs = [s["r"] for s in done]
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r <= 0]
    eq = np.cumsum(rs)
    dd = float((np.maximum.accumulate(np.concatenate([[0], eq])) - np.concatenate([[0], eq])).max())
    streak_w = streak_l = cur_w = cur_l = 0
    for r in rs:
        cur_w, cur_l = (cur_w + 1, 0) if r > 0 else (0, cur_l + 1)
        streak_w, streak_l = max(streak_w, cur_w), max(streak_l, cur_l)

    def group(key):
        g = {}
        for s in done:
            g.setdefault(key(s), []).append(s["r"])
        return {k: {"n": len(v), "win_rate": round(100 * sum(r > 0 for r in v) / len(v)),
                    "avg_r": round(float(np.mean(v)), 2)} for k, v in g.items()}

    return {
        "trades": len(done),
        "win_rate": round(100 * len(wins) / len(done), 1),
        "avg_r": round(float(np.mean(rs)), 2),
        "expectancy_r": round(float(np.mean(rs)), 2),
        "profit_factor": round(sum(wins) / abs(sum(losses)), 2) if losses and sum(losses) else None,
        "total_r": round(float(sum(rs)), 2),
        "max_drawdown_r": round(dd, 2),
        "max_consec_wins": streak_w, "max_consec_losses": streak_l,
        "by_session": group(lambda s: s["session"]),
        "by_score": group(lambda s: "90+" if s["score"] >= 90 else "85-89"),
        "by_direction": group(lambda s: s["direction"]),
    }


def risk_lock(journal, now):
    today = now.floor("D")
    todays = [s for s in journal["signals"] if s["status"] == "closed"
              and pd.Timestamp(s["closed_utc"]) >= today]
    total = sum(s["r"] for s in todays)
    last_two = [s["r"] for s in todays][-2:]
    if total <= -2:
        return f"Daily loss limit reached ({total:.1f}R)"
    if len(last_two) == 2 and all(r < 0 for r in last_two):
        return "Two consecutive losses today, stop trading for the session"
    return None


# ───────────────────────────── analysis ─────────────────────────────

def analyze(now):
    mk = load_market(now)
    m5 = add_delta(add_indicators(mk["m5"], with_vwap=True))
    m15 = add_indicators(mk["m15"])
    h1 = add_indicators(mk["h1"])
    h4 = add_indicators(mk["h4"])
    for name, df, need in (("M5", m5, 220), ("M15", m15, 120), ("H1", h1, 220), ("H4", h4, 210)):
        if len(df) < need:
            raise RuntimeError(f"Not enough {name} history ({len(df)} bars)")

    last_close_time = m5.index[-1] + pd.Timedelta(minutes=5)
    age_min = (now - last_close_time).total_seconds() / 60

    st = {
        "H4": analyze_structure(h4, 3, 3),
        "H1": analyze_structure(h1, 2, 2),
        "M15": analyze_structure(m15, 2, 2),
        "M5": analyze_structure(m5, 2, 2),
    }
    for key, df in (("H1", h1), ("M15", m15)):
        st[key]["structure_bias"] = st[key]["bias"]
        st[key]["ema_bias"] = ema_bias(df.iloc[-1])
        st[key]["bias"] = combine_bias(st[key]["bias"], st[key]["ema_bias"])
    h4_bias, h4_note = h4_regime(h4, st["H4"])
    b5, b15, bh1 = m5.iloc[-1], m15.iloc[-1], h1.iloc[-1]
    a5 = float(b5["atr14"])
    price = float(b5["close"])

    # liquidity pools
    pools = day_levels(m5, h1, now)
    for side, idx in (("sell", st["M15"]["lows"]), ("buy", st["M15"]["highs"])):
        for p in idx[-8:]:
            t_conf = m15.index[p] + pd.Timedelta(minutes=15 * 4)
            lvl = float(m15["low"].iat[p] if side == "sell" else m15["high"].iat[p])
            pools.append({"name": f"M15 swing {'low' if side == 'sell' else 'high'}", "price": lvl,
                          "side": side, "valid_from": t_conf})
    sweep = find_sweep(m5, pools, a5, CFG["sweep_lookback_bars"])

    def untouched(l):
        after = m5[m5.index >= l["valid_from"]]
        if after.empty:
            return True
        return after["high"].max() <= l["price"] if l["side"] == "buy" else after["low"].min() >= l["price"]

    day = [l for l in pools if "M15" not in l["name"]]
    taken = [dict(l, swept=True) for l in day if not untouched(l)]
    targets_buy = [l for l in day if l["side"] == "buy" and untouched(l)]
    targets_sell = [l for l in day if l["side"] == "sell" and untouched(l)]
    targets_buy += unswept_swings(h1, st["H1"]["highs"], "buy", 3, "H1") + \
        unswept_swings(m15, st["M15"]["highs"], "buy", 3, "M15")
    targets_sell += unswept_swings(h1, st["H1"]["lows"], "sell", 3, "H1") + \
        unswept_swings(m15, st["M15"]["lows"], "sell", 3, "M15")
    prof = profiles(m5, CFG["profile_bin"])
    for t in profile_targets(prof, price):
        (targets_buy if t["side"] == "buy" else targets_sell).append(t)
    targets_buy = mark_equal(dedupe(targets_buy, 0.05 * a5), 0.15 * a5)
    targets_sell = mark_equal(dedupe(targets_sell, 0.05 * a5), 0.15 * a5)

    news = load_news(now)
    session = session_of(now)
    crosses = vwap_crosses(m5)
    atr_pct = float((m5["atr14"].iloc[-288 * 5:] < a5).mean() * 100)

    ctx = dict(now=now, mk=mk, m5=m5, m15=m15, h1=h1, h4=h4, st=st, h4_bias=h4_bias, h4_note=h4_note,
               price=price, a5=a5, sweep=sweep, targets_buy=targets_buy, targets_sell=targets_sell,
               news=news, session=session, taken=taken, prof=prof, crosses=crosses, atr_pct=atr_pct, age_min=age_min,
               b5=b5, b15=b15, bh1=bh1, pools=pools)
    return ctx


def plan_trade(ctx, d):
    """Structural stop, liquidity target. d = +1 long, -1 short."""
    m5, a5, price, sweep = ctx["m5"], ctx["a5"], ctx["price"], ctx["sweep"]
    st5 = ctx["st"]["M5"]
    if sweep and sweep["dir"] == ("bull" if d > 0 else "bear"):
        base, why = sweep["extreme"], f"beyond the {sweep['name']} sweep"
    else:
        idx = st5["lows"] if d > 0 else st5["highs"]
        cands = [float(m5["low"].iat[p]) if d > 0 else float(m5["high"].iat[p]) for p in idx[-6:]]
        cands = [c for c in cands if (c < price if d > 0 else c > price)]
        if not cands:
            return None
        base = cands[-1]
        why = "beyond the last M5 swing " + ("low" if d > 0 else "high")
    sl = base - d * 0.25 * a5
    if abs(price - sl) < 0.6 * a5:
        sl = price - d * 0.6 * a5
    R = abs(price - sl)
    tg = ctx["targets_buy"] if d > 0 else ctx["targets_sell"]
    ahead = sorted([t for t in tg if (t["price"] > price + 0.1 * a5 if d > 0 else t["price"] < price - 0.1 * a5)],
                   key=lambda t: abs(t["price"] - price))
    if ahead:
        tp2, tp2_name = ahead[0]["price"], ahead[0]["name"]
    else:
        tp2, tp2_name = price + d * 2 * R, "2R projection (no mapped liquidity ahead)"
    return {"entry": price, "sl": sl, "tp1": price + d * R, "tp2": tp2, "tp2_name": tp2_name,
            "R": R, "rr": abs(tp2 - price) / R, "stop_reason": why, "stop_atr": R / a5}


def score_setup(ctx, d, plan):
    """100-point setup quality score (Section 20). Not a win probability."""
    st, b5, b15, bh1, m5, m15 = ctx["st"], ctx["b5"], ctx["b15"], ctx["bh1"], ctx["m5"], ctx["m15"]
    want = "Bullish" if d > 0 else "Bearish"
    edir = "bull" if d > 0 else "bear"
    sc, notes = {}, []

    s = 0
    s += 7 if ctx["h4_bias"] == want else 3 if ctx["h4_bias"] == "Neutral" else 0
    h1_side_ok = (bh1["close"] > bh1["ema200"]) if d > 0 else (bh1["close"] < bh1["ema200"])
    s += (8 if h1_side_ok else 5) if st["H1"]["bias"] == want else (3 if ctx.get("h1_transition") else 0)
    s += 5 if st["M15"]["bias"] == want else 2 if st["M15"]["bias"] == "Neutral" else 0
    recent = [e for e in st["M5"]["events"] if e["dir"] == edir and e["i"] >= len(m5) - 36]
    sweep_ok = ctx["sweep"] and ctx["sweep"]["dir"] == edir
    confirm = [e for e in recent if not sweep_ok or e["i"] >= ctx["sweep"]["i"]]
    s += 5 if confirm else 2 if recent else 0
    sc["structure"] = s

    s = 0
    pullback = False
    if confirm:
        e = confirm[-1]
        after = m5.iloc[e["i"] + 1:]
        if len(after):
            ref = np.maximum(after["ema20"], after["vwap"]) if d > 0 else np.minimum(after["ema20"], after["vwap"])
            pullback = bool(((after["low"] <= ref + 0.2 * ctx["a5"]) if d > 0
                             else (after["high"] >= ref - 0.2 * ctx["a5"])).any())
    if sweep_ok:
        s += 12
    elif confirm and pullback:
        s += 7
    if plan:
        s += 8 if plan["rr"] >= 2 else 5 if plan["rr"] >= 1.5 else 0
    sc["liquidity"] = s

    s = 0
    r7_prev = m5["rsi7"].iloc[-4]
    if (b5["rsi7"] > 50) if d > 0 else (b5["rsi7"] < 50):
        s += 3 if ((b5["rsi7"] > r7_prev) if d > 0 else (b5["rsi7"] < r7_prev)) else 2
    s += 3 if ((b5["rsi14"] > 50) if d > 0 else (b5["rsi14"] < 50)) else \
        1 if ((b5["rsi14"] > 45) if d > 0 else (b5["rsi14"] < 55)) else 0
    e5 = (b5["ema20"] > b5["ema50"]) if d > 0 else (b5["ema20"] < b5["ema50"])
    e15 = (b15["ema20"] > b15["ema50"]) if d > 0 else (b15["ema20"] < b15["ema50"])
    s += 3 if e5 and e15 else 1 if (e5 or e15) else 0
    adx_rising = b15["adx14"] > m15["adx14"].iloc[-4]
    s += 3 if b15["adx14"] >= 20 and adx_rising else 2 if b15["adx14"] >= 20 else 1 if b15["adx14"] >= 15 and adx_rising else 0
    sc["momentum"] = s

    s = 0
    if (b5["close"] > b5["vwap"]) if d > 0 else (b5["close"] < b5["vwap"]):
        s += 5
    s += 3 if ctx["crosses"] <= 2 else 1 if ctx["crosses"] <= 3 else 0
    sc["vwap"] = s

    since = ctx["sweep"]["i"] if sweep_ok else (confirm[-1]["i"] if confirm else len(m5) - 6)
    # Futures volume (Yahoo) runs ~10 min behind spot prices, so the newest bars often have none.
    # Measure volume and delta over the latest bars that do have it (at least 6 bars).
    has_vol = m5["volume"].notna() & (m5["volume"] > 0) if "volume" in m5 else pd.Series(False, index=m5.index)
    if has_vol.any():
        last_vol = int(has_vol.values.nonzero()[0][-1])
        if last_vol - since < 5:
            since = max(0, min(since, last_vol - 5))
    rv = m5["rvol"].iloc[since:].max()
    ctx["rvol"] = None if pd.isna(rv) else float(rv)
    v = 0 if pd.isna(rv) else 5 if rv >= 1.5 else 4 if rv >= 1.2 else 2 if rv >= 1.0 else 0
    pc_pts, pc_txt = profile_context(ctx["price"], d, ctx["prof"], m5, ctx["a5"], sweep_ok,
                                     ctx["sweep"] if sweep_ok else None)
    v += pc_pts
    flow = flow_read(m5, d, since, ctx["a5"], ctx["sweep"] if sweep_ok else None)
    if flow["available"]:
        lr, sl = d * flow["leg_ratio"], d * flow["session_slope"]
        f = 3 if lr >= 0.15 else 2 if lr >= 0.05 else 0
        f += 1 if sl > 0 else 0
        f += 1 if (flow["divergence"] or flow["absorption"]) else 0  # None (no sweep) counts as no
        v += min(f, 5)
    ctx["flow"], ctx["profile_ctx"] = flow, pc_txt
    sc["volume_flow"] = v

    p = ctx["atr_pct"]
    sc["volatility"] = 5 if 15 <= p <= 90 else 2 if 5 <= p <= 97 else 0
    sc["session"] = {"London/NY overlap": 5, "London": 4, "New York": 4, "Asia": 1}.get(ctx["session"], 0)
    sc["news"] = {"Low": 10, "Medium": 4}.get(ctx["news"]["risk"], 0)
    ctx["confirm"], ctx["pullback"], ctx["sweep_ok"] = confirm, pullback, sweep_ok
    return sum(sc.values()), sc


def quality(score):
    return "A+" if score >= 90 else "A" if score >= 85 else "B+" if score >= 80 else "WATCH" if score >= 70 else "NO TRADE"


MODES = {
    # strict:   your original rulebook
    # balanced: also trades early trend turns (H1 + M15 agree against H4); quicker re-entry
    # active:   balanced plus a lower score bar; more trades, more losers
    "strict":   {"min_score": 85, "asia_min": 90, "early_turn": False, "cooldown": 30, "max_per_session": 3},
    "balanced": {"min_score": 85, "asia_min": 90, "early_turn": True,  "cooldown": 20, "max_per_session": 3},
    "active":   {"min_score": 80, "asia_min": 85, "early_turn": True,  "cooldown": 15, "max_per_session": 4},
}


def mode_cfg():
    m = MODES.get(CFG.get("mode", "strict"), MODES["strict"]).copy()
    m["name"] = CFG.get("mode", "strict") if CFG.get("mode") in MODES else "strict"
    return m


def decide(ctx, journal):
    now, st = ctx["now"], ctx["st"]
    M = mode_cfg()
    ctx["mode"], ctx["min_score"] = M["name"], M["min_score"]
    ctx["early_turn"] = ctx["conflict_only"] = False
    blockers, why = [], []
    decision, d, plan, score, sc = "NO TRADE", 0, None, 0, {}

    h1 = st["H1"]["bias"]
    ctx["h1_transition"] = False
    if h1 == "Bullish":
        d = 1
    elif h1 == "Bearish":
        d = -1
    else:
        lower = {ctx["h4_bias"], st["M15"]["bias"], st["M5"]["bias"]}
        if len(lower) == 1 and "Neutral" not in lower:
            d = 1 if lower == {"Bullish"} else -1
            ctx["h1_transition"] = True

    adx_weak = ctx["b15"]["adx14"] < 18
    vwap_chop = ctx["crosses"] >= 4
    ema_flat = abs(ctx["b15"]["ema20"] - ctx["b15"]["ema50"]) < 0.15 * ctx["b15"]["atr14"]
    chop = sum([adx_weak, vwap_chop, ema_flat]) >= 2

    open_sig = [s for s in journal["signals"] if s["status"] == "open"]
    sess_sigs = [s for s in journal["signals"] if pd.Timestamp(s["time_utc"]) >= now - timedelta(hours=5)]
    last_sig = journal["signals"][-1] if journal["signals"] else None
    lock = risk_lock(journal, now)

    if ctx["age_min"] > CFG["stale_minutes"]:
        blockers.append(f"STALE DATA — last closed M5 bar is {ctx['age_min']:.0f} min old")
    if lock:
        blockers.append(lock)
    if ctx["news"]["risk"] == "High":
        blockers.append(f"High-impact news window: {ctx['news']['detail']}")
    if d == 0:
        blockers.append("No clear direction: H1 is neutral and H4, M15 and M5 do not all agree")
    elif h1 != "Neutral" and ctx["h4_bias"] != "Neutral" and ctx["h4_bias"] != h1:
        if M["early_turn"] and st["M15"]["bias"] == h1:
            ctx["early_turn"] = True      # H1 and M15 have turned together; H4 has not caught up yet
        else:
            blockers.append(f"H4 ({ctx['h4_bias']}) and H1 ({h1}) conflict")
            ctx["conflict_only"] = True
    if chop:
        bits = [t for t, f in (("ADX weak", adx_weak), (f"VWAP crossed {ctx['crosses']}× in 2h", vwap_chop),
                               ("EMA20/50 flat", ema_flat)) if f]
        blockers.append("MARKET CHOP — " + ", ".join(bits))

    if d:
        plan = plan_trade(ctx, d)
        score, sc = score_setup(ctx, d, plan)

    if len(blockers) > 1:
        ctx["conflict_only"] = False
    if blockers:
        decision = "NO TRADE"
        why += blockers
    elif open_sig:
        decision = "WAIT"
        why.append(f"A {open_sig[0]['direction']} signal is still active, manage it before a new one")
    elif len(sess_sigs) >= M["max_per_session"]:
        decision = "WAIT"
        why.append("Session signal cap reached")
    elif last_sig and now - pd.Timestamp(last_sig["time_utc"]) < timedelta(minutes=M["cooldown"]):
        decision = "WAIT"
        why.append("Cooling down after the last signal")
    elif plan is None:
        decision = "NO TRADE"
        why.append("No logical structural stop available")
    elif score < 70:
        decision = "NO TRADE"
    elif score < M["min_score"]:
        decision = "WAIT"
        why.append(f"Score {score}/100 is below the {M['min_score']} live-trade minimum")
    elif ctx["session"] == "Asia" and score < M["asia_min"]:
        decision = "WAIT"
        why.append(f"Asian session needs {M['asia_min']}+ (score {score}/100); liquidity is thinner before London")
    else:
        ext = abs(ctx["price"] - ctx["b5"]["ema20"]) / ctx["a5"]
        if plan["stop_atr"] > 3:
            decision = "NO TRADE"
            why.append(f"Structural stop is {plan['stop_atr']:.1f}× ATR away, too wide for a scalp")
        elif plan["rr"] < 1.5:
            decision = "NO TRADE"
            why.append(f"Next liquidity target ({plan['tp2_name']}) gives only 1:{plan['rr']:.2f}")
        elif ext > 1.5:
            decision = "WAIT"
            why.append(f"Price is {ext:.1f}× ATR from M5 EMA20, wait for a pullback")
        elif not ctx["confirm"]:
            decision = "WAIT"
            why.append("No M5 BOS/CHOCH confirmation yet")
        elif ctx["flow"].get("available") and d * ctx["flow"]["leg_ratio"] < -0.15:
            decision = "WAIT"
            why.append(f"Estimated order flow opposes the trade (leg delta {ctx['flow']['leg_ratio']:+.2f})")
        else:
            decision = "LONG" if d > 0 else "SHORT"

    # explain the score
    if d and not blockers:
        dirw = "bullish" if d > 0 else "bearish"
        if ctx["sweep_ok"]:
            sw = ctx["sweep"]
            why.append(f"{sw['name']} at {sw['level']:.2f} swept, then {sw['confirm']}")
        elif ctx["confirm"] and ctx["pullback"]:
            why.append("Structural continuation: M5 BOS followed by a pullback into EMA20/VWAP")
        else:
            why.append("No liquidity sweep or clean continuation setup")
        if ctx["confirm"]:
            e = ctx["confirm"][-1]
            why.append(f"M5 {dirw} {e['kind']} through {e['level']:.2f}")
        if ctx.get("early_turn"):
            why.append(f"Early trend turn: H1 and M15 turned {'up' if d > 0 else 'down'} before H4 (allowed in {M['name']} mode)")
        if ctx.get("h1_transition"):
            why.append("H1 is in transition; H4, M15 and M5 all agree on the direction")
        why.append(f"H4 {ctx['h4_bias'].lower()} ({ctx['h4_note']}), H1 {h1.lower()}, "
                   f"M15 {st['M15']['bias'].lower()}")
        rv = ctx.get("rvol")
        why.append(f"RSI7 {ctx['b5']['rsi7']:.0f}, RSI14 {ctx['b5']['rsi14']:.0f}, M15 ADX {ctx['b15']['adx14']:.0f}"
                   + (f", RVOL {rv:.1f}×" if rv else ", volume unavailable"))
        fl = ctx.get("flow", {})
        flow_txt = ctx.get("profile_ctx", "")
        if fl.get("available"):
            extra = [x for x, on in (("delta divergence at the sweep", fl["divergence"]),
                                     ("absorption at the sweep", fl["absorption"])) if on]
            flow_txt += f"; setup-leg delta {fl['leg_ratio']:+.2f}" + (", " + ", ".join(extra) if extra else "")
        why.append("Profile & flow: " + flow_txt)
        weak = [k.replace("_", " & ") for k, v in sc.items() if v <= {"structure": 12, "liquidity": 7, "momentum": 5,
                "vwap": 3, "volume_flow": 6, "volatility": 1, "session": 1, "news": 3}[k]]
        if weak:
            why.append("Weak components: " + ", ".join(weak))
    elif d and blockers and sc:
        why.append(f"Setup score would be {score}/100 (not tradable while blocked)")

    return decision, d, plan, score, sc, why


def invalidation(decision, plan, d, ctx):
    if decision in ("LONG", "SHORT"):
        word = "below" if d > 0 else "above"
        return (f"Trade through {plan['sl']:.2f} ({plan['stop_reason']}), or an M15 close {word} "
                f"the M15 EMA50 ({ctx['b15']['ema50']:.2f}) before TP1.")
    return None


# ───────────────────────────── alerts ─────────────────────────────

def _clean_phone(raw):
    """'+971 50-123 4567', '00971501234567' or '971501234567' -> '+971501234567'."""
    p = "".join(ch for ch in raw if ch.isdigit() or ch == "+")
    if p.startswith("00"):
        p = "+" + p[2:]
    if not p.startswith("+"):
        p = "+" + p
    return p


def notify(text):
    """WhatsApp alert via CallMeBot. Logs CallMeBot's own reply so failures are visible."""
    try:
        Path("/tmp/force_commit").touch()            # tells the workflow to publish this run immediately
    except Exception:  # noqa: BLE001
        pass
    phone, key = os.getenv("CALLMEBOT_PHONE", "").strip(), os.getenv("CALLMEBOT_APIKEY", "").strip()
    if not (phone and key):
        print("WhatsApp alert skipped: CALLMEBOT_PHONE / CALLMEBOT_APIKEY not set")
        return
    phone = _clean_phone(phone)
    masked = phone[:4] + "*" * max(len(phone) - 7, 0) + phone[-3:]
    try:
        r = requests.get("https://api.callmebot.com/whatsapp.php", timeout=30,
                         params={"phone": phone, "text": text, "apikey": key})
        body = re.sub(r"<[^>]+>", " ", r.text)
        body = re.sub(r"\s+", " ", body).strip()[:300]
        print(f"WhatsApp to {masked}: HTTP {r.status_code} | CallMeBot says: {body}")
    except Exception as e:  # noqa: BLE001
        print(f"WhatsApp alert failed: {e}")


# ───────────────────────────── main ─────────────────────────────

def fmt(x, n=2):
    return None if x is None or (isinstance(x, float) and math.isnan(x)) else round(float(x), n)


def main():
    now = pd.Timestamp(datetime.now(timezone.utc))
    OUT.mkdir(parents=True, exist_ok=True)
    journal = load_journal()
    if os.getenv("TEST_ALERT", "").lower() == "true":
        notify("XAUUSD desk is connected. Trade alerts will arrive here.")
    base = {"generated_utc": now.isoformat(), "dubai_time": now.tz_convert(DUBAI).strftime("%a %d %b %Y, %H:%M"),
            "session": session_of(now)}

    if market_closed(now):
        out = {**base, "decision": "NO TRADE", "headline": "Market closed",
               "why": ["Gold spot and futures are closed for the weekend"], "journal": journal_stats(journal)}
        (OUT / "signal.json").write_text(json.dumps(out, indent=1))
        return

    try:
        ctx = analyze(now)
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        out = {**base, "decision": "NO TRADE", "headline": "LIVE DATA REQUIRED — NO TRADE",
               "why": [f"Data could not be loaded: {e}"], "journal": journal_stats(journal)}
        (OUT / "signal.json").write_text(json.dumps(out, indent=1))
        return

    closed, tp1_hits = update_journal(journal, ctx["m5"])
    for s in tp1_hits:
        if s["status"] == "open":
            notify(f"🎯 XAUUSD {s['direction']} hit Target 1 ({s['tp1']:.2f})\n"
                   f"Take half the position and move your stop to entry ({s['entry']:.2f}).\n"
                   f"The rest aims for Target 2 ({s['tp2']:.2f}).")
    for s in closed:
        notify(f"{'✅' if s['r'] > 0 else '❌'} XAUUSD {s['direction']} closed: {s['result']} ({s['r']:+.2f}R)")

    decision, d, plan, score, sc, why = decide(ctx, journal)
    trade = decision in ("LONG", "SHORT")
    if trade:
        dd = 1 if decision == "LONG" else -1
        lp, lsrc = live_price()
        if lp:
            R0 = plan["R"]
            R1 = (lp - plan["sl"]) * dd                      # stop distance from where price is NOW
            rr1 = (plan["tp2"] - lp) * dd / R1 if R1 > 0 else 0
            problem = None
            if R1 < 0.4 * R0:
                problem = f"price has moved to {lp:.2f}, too close to the stop at {plan['sl']:.2f}"
            elif R1 > 1.5 * R0:
                problem = f"price has run to {lp:.2f}; the stop would be {R1 / R0:.1f}x wider than planned"
            elif rr1 < 1.5:
                problem = f"at the live price {lp:.2f} the reward to risk is only 1:{rr1:.2f}"
            if problem:
                why.insert(0, f"Setup found, but the entry is no longer valid: {problem}")
                decision, trade = "WAIT", False
            else:
                plan.update(entry=lp, R=R1, tp1=lp + dd * R1, rr=rr1, stop_atr=R1 / ctx["a5"])
                plan["live_src"] = lsrc
        if trade:
            plan["max_entry"] = plan["entry"] + dd * 0.25 * plan["R"]
            plan["valid_until"] = now + timedelta(minutes=CFG.get("entry_valid_minutes", 10))
    b5, b15 = ctx["b5"], ctx["b15"]
    st = ctx["st"]

    regime = ("High Volatility" if ctx["atr_pct"] > 90 else
              "Range" if any("CHOP" in w for w in why) else
              "Transition" if ctx["h4_bias"] == "Neutral" or st["H1"]["bias"] == "Neutral" else
              "Breakout" if (ctx.get("rvol") or 0) >= 1.5 and ctx.get("confirm") else "Trend")

    def rel(df_row, fast, slow):
        return f"{'above' if df_row[fast] > df_row[slow] else 'below'}"

    equity = os.getenv("ACCOUNT_EQUITY")
    size = None
    if trade and equity:
        risk_cash = float(equity) * CFG["risk_pct"] / 100
        lots = math.floor(risk_cash / (plan["R"] * CFG["contract_oz"]) * 100) / 100
        size = {"risk_cash": round(risk_cash, 2), "lots": lots}

    rvol = ctx.get("rvol")
    m5 = ctx["m5"].iloc[-150:]
    out = {
        **base,
        "price": fmt(ctx["price"]),
        "source": ctx["mk"]["source"], "volume_source": ctx["mk"]["volume_source"],
        "data_age_min": round(ctx["age_min"], 1), "last_bar_utc": ctx["m5"].index[-1].isoformat(),
        "warnings": ctx["mk"]["warnings"],
        "regime": regime,
        "bias": {"H4": ctx["h4_bias"], "H1": st["H1"]["bias"], "M15": st["M15"]["bias"], "M5": st["M5"]["bias"]},
        "swings": {k: v["labels"] for k, v in st.items()},
        "ema200": {"H4": "above" if ctx["h4"].iloc[-1]["close"] > ctx["h4"].iloc[-1]["ema200"] else "below",
                   "H1": "above" if ctx["bh1"]["close"] > ctx["bh1"]["ema200"] else "below"},
        "ema20_50": {"M5": "EMA20 " + rel(b5, "ema20", "ema50") + " EMA50",
                     "M15": "EMA20 " + rel(b15, "ema20", "ema50") + " EMA50"},
        "vwap": {"value": fmt(b5["vwap"]), "side": "Choppy" if ctx["crosses"] >= 4 else
                 ("Above" if b5["close"] > b5["vwap"] else "Below"), "crosses_2h": ctx["crosses"],
                 "volume_weighted": bool(ctx["m5"].attrs.get("vwap_volume_weighted", False))},
        "rsi7": fmt(b5["rsi7"], 1), "rsi14": fmt(b5["rsi14"], 1),
        "adx14": {"M5": fmt(b5["adx14"], 1), "M15": fmt(b15["adx14"], 1)},
        "atr14": {"M5": fmt(b5["atr14"]), "M15": fmt(b15["atr14"]), "percentile": round(ctx["atr_pct"])},
        "volume": (f"RVOL {rvol:.2f}× on the setup leg" if rvol else "Not available") + f" — {ctx['mk']['volume_source']}",
        "profile": {**{k: v for k, v in ctx["prof"].items() if k != "developing"},
                    "developing": ctx["prof"]["developing"], "context": ctx.get("profile_ctx")},
        "orderflow": ctx.get("flow") or {"available": not ctx["m5"]["delta"].isna().all()},
        "liquidity_event": (f"{ctx['sweep']['name']} {ctx['sweep']['level']:.2f} swept "
                            f"({'sell-side' if ctx['sweep']['dir'] == 'bull' else 'buy-side'}), {ctx['sweep']['confirm']}"
                            if ctx["sweep"] else "No valid sweep in the last 2.5 hours"),
        "bos_choch": (lambda e: f"M5 {e['kind']} {e['dir']}ish through {e['level']:.2f} at "
                      f"{e['time'].tz_convert(DUBAI):%H:%M}" if e else "None")(st["M5"]["last_event"]),
        "news": ctx["news"],
        "spread": "Not available from free data — check your broker before entry",
        "score": score, "score_parts": sc, "candidate": "LONG" if d > 0 else "SHORT" if d < 0 else None,
        "decision": decision,
        "headline": {"LONG": "Long setup confirmed", "SHORT": "Short setup confirmed", "WAIT": "Wait",
                     "NO TRADE": "No trade"}[decision],
        "quality": quality(score) if trade or decision == "WAIT" else "NO TRADE",
        "mode": ctx.get("mode", "strict"), "min_score": ctx.get("min_score", 85),
        "plan": ({"entry": fmt(plan["entry"]), "sl": fmt(plan["sl"]), "tp1": fmt(plan["tp1"]),
                  "tp2": fmt(plan["tp2"]), "tp2_name": plan["tp2_name"], "rr": f"1:{plan['rr']:.2f}",
                  "risk_pct": CFG["risk_pct"], "size": size,
                  "max_entry": fmt(plan.get("max_entry")),
                  "valid_until": plan["valid_until"].isoformat() if plan.get("valid_until") is not None else None,
                  "valid_until_dubai": plan["valid_until"].tz_convert(DUBAI).strftime("%H:%M") if plan.get("valid_until") is not None else None,
                  "live_src": plan.get("live_src")} if trade else None),
        "why": why[:8],
        "invalidation": invalidation(decision, plan, d, ctx),
        "levels": sorted([{"name": l["name"], "price": round(l["price"], 2), "side": l["side"],
                           "swept": l.get("swept", False)}
                          for l in ctx["targets_buy"] + ctx["targets_sell"] + ctx["taken"]],
                         key=lambda l: -l["price"])[:16],
        "chart": [{"t": int(ts.timestamp()), "o": fmt(r.open), "h": fmt(r.high), "l": fmt(r.low), "c": fmt(r.close),
                   "e20": fmt(r.ema20), "e50": fmt(r.ema50), "vw": fmt(r.vwap),
                   "dl": fmt(r.delta, 0)} for ts, r in m5.iterrows()],
    }

    if trade:
        rec = {"time_utc": now.isoformat(), "dubai": base["dubai_time"], "session": ctx["session"],
               "direction": decision, "entry": fmt(plan["entry"]), "sl": fmt(plan["sl"]), "tp1": fmt(plan["tp1"]),
               "tp2": fmt(plan["tp2"]), "rr": round(plan["rr"], 2), "score": score, "risk_pct": CFG["risk_pct"],
               "regime": regime, "reason": "; ".join(why[:3]), "status": "open", "tp1_hit": False}
        journal["signals"].append(rec)
        notify(f"{'🟢' if decision == 'LONG' else '🔴'} XAUUSD {decision} — score {score}/100 ({quality(score)})\n"
               f"Entry {plan['entry']:.2f} ({'live price' if plan.get('live_src') else 'last candle close'})  SL {plan['sl']:.2f}\n"
               f"TP1 {plan['tp1']:.2f}  TP2 {plan['tp2']:.2f} (1:{plan['rr']:.1f})\n"
               f"{'Buy only up to' if decision == 'LONG' else 'Sell only down to'} {plan['max_entry']:.2f}. "
               f"Valid until {plan['valid_until'].tz_convert(DUBAI):%H:%M} Dubai.\n"
               f"Use a limit order. Skip if price is past that level or the time has passed.\n"
               f"At TP1: take half and move the stop to entry.")

    # 🟡 heads-up: a setup is close (score 75+) but not tradable yet. At most once an hour per direction.
    if (decision == "WAIT" or ctx.get("conflict_only")) and score >= CFG.get("watch_min_score", 75) and d:
        side = "LONG" if d > 0 else "SHORT"
        last = journal.get("watch", {}).get(side)
        if not last or now - pd.Timestamp(last) >= timedelta(minutes=CFG.get("watch_cooldown_minutes", 60)):
            journal.setdefault("watch", {})[side] = now.isoformat()
            notify(f"🟡 XAUUSD WATCH — possible {side}, score {score}/100\n"
                   f"Price {ctx['price']:.2f}. Not a trade yet: {why[0] if why else 'conditions incomplete'}\n"
                   f"Watch your chart; a {side} alert follows only if every check passes.")

    journal["signals"] = journal["signals"][-500:]
    out["journal"] = journal_stats(journal)
    out["recent_signals"] = journal["signals"][-10:][::-1]
    (OUT / "journal.json").write_text(json.dumps(journal, indent=1))
    (OUT / "signal.json").write_text(json.dumps(out, indent=1, default=str))
    print(f"{base['dubai_time']}  {decision}  score={score}  price={ctx['price']:.2f}")


if __name__ == "__main__":
    main()
