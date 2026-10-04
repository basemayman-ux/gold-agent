"""Volume profile (POC / VAH / VAL / naked POCs) and bar-based order-flow estimates.

Honest scope: free data has bar volume but no bid/ask aggressor side, so delta here is an
ESTIMATE from where each bar closed inside its range (close-location value x volume).
It approximates footprint delta; it is not footprint delta. When no volume exists, the
profile falls back to a time-based TPO profile and delta is disabled."""
import numpy as np
import pandas as pd


def gold_day(index, shift_hours=2):
    return (index + pd.Timedelta(hours=shift_hours)).floor("D")


def build_profile(df, bin_size, use_volume=True, value_pct=0.70):
    if df.empty:
        return None
    lo = np.floor(df["low"].min() / bin_size) * bin_size
    hi = np.ceil(df["high"].max() / bin_size) * bin_size
    n = max(int(round((hi - lo) / bin_size)), 1)
    hist = np.zeros(n)
    vols = df["volume"].fillna(0).values if use_volume else np.ones(len(df))
    for h, l, v in zip(df["high"].values, df["low"].values, vols):
        i0 = min(int((l - lo) // bin_size), n - 1)
        i1 = min(int((h - lo) // bin_size), n - 1)
        hist[i0:i1 + 1] += v / (i1 - i0 + 1)  # spread each bar's volume evenly over its range
    if hist.sum() <= 0:
        return None
    poc = int(hist.argmax())
    total, acc, a, b = hist.sum(), hist[poc], poc, poc
    while acc < value_pct * total:
        up = hist[b + 1] if b + 1 < n else -1
        dn = hist[a - 1] if a - 1 >= 0 else -1
        if up < 0 and dn < 0:
            break
        if up >= dn:
            b += 1
            acc += up
        else:
            a -= 1
            acc += dn
    centers = lo + (np.arange(n) + 0.5) * bin_size
    return {
        "poc": float(centers[poc]), "vah": float(lo + (b + 1) * bin_size), "val": float(lo + a * bin_size),
        "bins": [[round(float(c), 2), round(float(v), 1)] for c, v in zip(centers, hist)],
    }


def profiles(m5, bin_size, days_back=5):
    use_vol = "volume" in m5 and m5["volume"].fillna(0).sum() > 0
    day = gold_day(m5.index)
    days = sorted(day.unique())
    today = days[-1]
    developing = build_profile(m5[day == today], bin_size, use_vol)
    prior = []
    for d in days[-(days_back + 1):-1]:
        p = build_profile(m5[day == d], bin_size, use_vol)
        if p:
            p["day"] = d
            prior.append(p)
    naked = []
    for p in prior:
        after = m5[day > p["day"]]
        if after.empty:
            continue
        touched = ((after["low"] <= p["poc"]) & (after["high"] >= p["poc"])).any()
        if not touched:
            naked.append({"price": p["poc"], "day": p["day"].strftime("%a %d %b")})
    return {
        "type": "volume" if use_vol else "TPO (time, no volume)",
        "developing": developing,
        "prior": ({k: prior[-1][k] for k in ("poc", "vah", "val")} if prior else None),
        "naked_pocs": naked,
    }


def add_delta(m5):
    """Estimated per-bar delta = volume x close-location value (-1 at the low, +1 at the high)."""
    m5 = m5.copy()
    if "volume" not in m5 or m5["volume"].fillna(0).sum() <= 0:
        m5["delta"] = np.nan
        m5["cd"] = np.nan
        m5["cd_session"] = np.nan
        return m5
    rng = (m5["high"] - m5["low"]).replace(0, np.nan)
    clv = (((m5["close"] - m5["low"]) - (m5["high"] - m5["close"])) / rng).fillna(0)
    m5["delta"] = clv * m5["volume"].fillna(0)
    m5["cd"] = m5["delta"].cumsum()
    m5["cd_session"] = m5["delta"].groupby(gold_day(m5.index)).cumsum()
    return m5


def flow_read(m5, d, start_i, atr5, sweep=None):
    """Order-flow evidence for direction d (+1/-1) over the setup leg starting at start_i."""
    if m5["delta"].isna().all():
        return {"available": False}
    leg = m5.iloc[max(start_i, 0):]
    vol = leg["volume"].fillna(0).sum()
    leg_ratio = float(leg["delta"].sum() / vol) if vol > 0 else 0.0
    slope = float(m5["cd_session"].iloc[-1] - m5["cd_session"].iloc[-13]) if len(m5) > 13 else 0.0

    divergence = absorption = None
    if sweep is not None:
        divergence = absorption = False
        i = sweep["i"]
        prev = m5.iloc[max(i - 36, 0):i]
        if len(prev):
            if d > 0:
                divergence = bool(m5["low"].iat[i] < prev["low"].min() and
                                  m5["cd"].iat[i] > prev["cd"].iloc[prev["low"].values.argmin()])
            else:
                divergence = bool(m5["high"].iat[i] > prev["high"].max() and
                                  m5["cd"].iat[i] < prev["cd"].iloc[prev["high"].values.argmax()])
        for k in range(i, min(i + 3, len(m5))):
            b = m5.iloc[k]
            rv = b.get("rvol", np.nan)
            close_pos = (b["close"] - b["low"]) / max(b["high"] - b["low"], 1e-9)
            favourable = close_pos >= 0.5 if d > 0 else close_pos <= 0.5
            if not pd.isna(rv) and rv >= 1.8 and (b["high"] - b["low"]) <= 0.8 * atr5 and favourable:
                absorption = True
    return {
        "available": True,
        "leg_ratio": round(leg_ratio, 3),
        "session_delta": round(float(m5["cd_session"].iloc[-1]), 0),
        "session_slope": round(slope, 0),
        "divergence": divergence,
        "absorption": absorption,
    }


def profile_context(price, d, prof, m5, atr5, sweep_ok, sweep):
    """Where is price relative to value? Returns (points 0-5, text)."""
    dev, prior = prof.get("developing"), prof.get("prior")
    if not dev:
        return 0, "Profile unavailable"
    closes = m5["close"].iloc[-3:]
    if prior:
        if d > 0 and (closes > prior["vah"]).all():
            return 5, f"Accepted above prior VAH {prior['vah']:.2f} (initiative buying)"
        if d < 0 and (closes < prior["val"]).all():
            return 5, f"Accepted below prior VAL {prior['val']:.2f} (initiative selling)"
    edge_lo = min(dev["val"], prior["val"]) if prior else dev["val"]
    edge_hi = max(dev["vah"], prior["vah"]) if prior else dev["vah"]
    if sweep_ok and sweep:
        if d > 0 and sweep["extreme"] <= max(dev["val"], prior["val"] if prior else dev["val"]) + 0.25 * atr5:
            return 5, "Sweep happened at the value-area low (responsive buying)"
        if d < 0 and sweep["extreme"] >= min(dev["vah"], prior["vah"] if prior else dev["vah"]) - 0.25 * atr5:
            return 5, "Sweep happened at the value-area high (responsive selling)"
    overhead = [x for x in (dev["poc"], dev["vah"], prior and prior["poc"], prior and prior["vah"]) if x]
    below = [x for x in (dev["poc"], dev["val"], prior and prior["poc"], prior and prior["val"]) if x]
    if d > 0:
        if any(0 < x - price < 0.5 * atr5 for x in overhead):
            return 1, "Buying right under a POC/VAH (supply overhead)"
        if price < dev["poc"]:
            return 3, "Buying in the lower half of developing value"
    else:
        if any(0 < price - x < 0.5 * atr5 for x in below):
            return 1, "Selling right above a POC/VAL (demand below)"
        if price > dev["poc"]:
            return 3, "Selling in the upper half of developing value"
    if edge_lo < price < edge_hi:
        return 2, "Price inside value, no edge from the profile"
    return 2, "Price outside value without confirmed acceptance"


def profile_targets(prof, price):
    out = []
    prior = prof.get("prior")
    if prior:
        for name, key in (("Prior VAH", "vah"), ("Prior VAL", "val"), ("Prior POC", "poc")):
            out.append({"name": name, "price": prior[key], "side": "buy" if prior[key] > price else "sell"})
    for nk in prof.get("naked_pocs", []):
        out.append({"name": f"Naked POC ({nk['day']})", "price": nk["price"],
                    "side": "buy" if nk["price"] > price else "sell"})
    return out
