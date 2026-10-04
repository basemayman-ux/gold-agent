"""Market structure and liquidity: swing pivots, HH/HL/LH/LL, BOS/CHOCH, pools, sweeps."""
import pandas as pd


def pivots(df, left, right):
    """Fractal swing points. A pivot at bar p is only *known* at bar p + right."""
    H, L = df["high"].values, df["low"].values
    highs, lows = [], []
    for i in range(left, len(df) - right):
        if H[i] > H[i - left:i].max() and H[i] >= H[i + 1:i + right + 1].max():
            highs.append(i)
        if L[i] < L[i - left:i].min() and L[i] <= L[i + 1:i + right + 1].min():
            lows.append(i)
    return highs, lows


def bos_choch(df, highs, lows, right):
    """Walk bars in time; a close beyond the last confirmed swing is BOS (with trend)
    or CHOCH (against the prior trend)."""
    C = df["close"].values
    hs, ls = set(highs), set(lows)
    trend, last_h, last_l = None, None, None
    h_used = l_used = True
    events = []
    for i in range(len(df)):
        p = i - right
        if p in hs:
            last_h, h_used = (p, float(df["high"].iat[p])), False
        if p in ls:
            last_l, l_used = (p, float(df["low"].iat[p])), False
        if last_h and not h_used and C[i] > last_h[1]:
            kind = "CHOCH" if trend == "bear" else "BOS"
            trend, h_used = "bull", True
            events.append({"i": i, "time": df.index[i], "kind": kind, "dir": "bull", "level": last_h[1]})
        if last_l and not l_used and C[i] < last_l[1]:
            kind = "CHOCH" if trend == "bull" else "BOS"
            trend, l_used = "bear", True
            events.append({"i": i, "time": df.index[i], "kind": kind, "dir": "bear", "level": last_l[1]})
    return trend, events


def analyze_structure(df, left, right):
    highs, lows = pivots(df, left, right)
    trend, events = bos_choch(df, highs, lows, right)
    swing = "Range"
    labels = []
    if len(highs) >= 2 and len(lows) >= 2:
        h1, h2 = df["high"].iat[highs[-2]], df["high"].iat[highs[-1]]
        l1, l2 = df["low"].iat[lows[-2]], df["low"].iat[lows[-1]]
        labels = ["HH" if h2 > h1 else "LH", "HL" if l2 > l1 else "LL"]
        if h2 > h1 and l2 > l1:
            swing = "Bullish"
        elif h2 < h1 and l2 < l1:
            swing = "Bearish"
    if trend == "bull" and swing != "Bearish":
        bias = "Bullish"
    elif trend == "bear" and swing != "Bullish":
        bias = "Bearish"
    else:
        bias = "Neutral"
    return {
        "bias": bias, "swing": swing, "labels": labels, "trend": trend,
        "events": events, "last_event": events[-1] if events else None,
        "highs": highs, "lows": lows,
    }


def unswept_swings(df, idx_list, side, right, tf_label, limit=6):
    """Swing levels not yet traded through: these are resting liquidity targets."""
    out = []
    n = len(df)
    for p in reversed(idx_list):
        if p + right >= n:
            continue
        if side == "buy":
            lvl = float(df["high"].iat[p])
            if p + 1 < n and df["high"].iloc[p + 1:].max() > lvl:
                continue
        else:
            lvl = float(df["low"].iat[p])
            if p + 1 < n and df["low"].iloc[p + 1:].min() < lvl:
                continue
        out.append({"name": f"{tf_label} swing {'high' if side == 'buy' else 'low'}", "price": lvl, "side": side})
        if len(out) >= limit:
            break
    return out


def mark_equal(levels, tol):
    """Two resting levels within tol of each other become 'Equal highs/lows'."""
    for a in levels:
        for b in levels:
            if a is not b and a["side"] == b["side"] and abs(a["price"] - b["price"]) <= tol:
                a["name"] = "Equal highs" if a["side"] == "buy" else "Equal lows"
    return levels


def find_sweep(m5, pools, atr5, lookback):
    """Most recent valid sweep: first penetration of a meaningful level inside the lookback,
    a reclaim within 2 bars, and a rejection wick or displacement close beyond the sweep bar."""
    n = len(m5)
    start = n - lookback
    O, H, L, C = (m5[k].values for k in ("open", "high", "low", "close"))
    found = []
    for pool in pools:
        price, side = pool["price"], pool["side"]
        valid_idx = m5.index.searchsorted(pool["valid_from"])
        for i in range(valid_idx, n):
            breached = L[i] < price if side == "sell" else H[i] > price
            if not breached:
                continue
            if i < start:
                break
            pen = (price - L[i]) if side == "sell" else (H[i] - price)
            if pen < 0.05 * atr5:
                break
            j_end = min(i + 3, n)
            reclaim = next((j for j in range(i, j_end) if (C[j] > price if side == "sell" else C[j] < price)), None)
            if reclaim is None:
                break
            rng = max(H[i] - L[i], 1e-9)
            if side == "sell":
                wick = (min(O[i], C[i]) - L[i]) / rng
                disp = any(C[k] > H[i] for k in range(reclaim, min(reclaim + 4, n)))
                extreme = float(L[i:reclaim + 1].min())
            else:
                wick = (H[i] - max(O[i], C[i])) / rng
                disp = any(C[k] < L[i] for k in range(reclaim, min(reclaim + 4, n)))
                extreme = float(H[i:reclaim + 1].max())
            if wick >= 0.4 or disp:
                found.append({
                    "dir": "bull" if side == "sell" else "bear", "name": pool["name"], "level": price,
                    "extreme": extreme, "i": i, "time": m5.index[i],
                    "confirm": "displacement" if disp else "rejection wick",
                })
            break
    return max(found, key=lambda s: s["i"]) if found else None
