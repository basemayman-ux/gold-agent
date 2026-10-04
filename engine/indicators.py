"""Approved indicators only: EMA 20/50/200, VWAP, ATR14, ADX14, RSI7/14, relative volume."""
import numpy as np
import pandas as pd


def ema(s, n):
    return s.ewm(span=n, adjust=False).mean()


def rma(s, n):
    """Wilder smoothing (same as TradingView RSI/ATR/ADX)."""
    return s.ewm(alpha=1 / n, adjust=False).mean()


def rsi(close, n):
    d = close.diff()
    up = rma(d.clip(lower=0), n)
    dn = rma(-d.clip(upper=0), n)
    rs = up / dn.replace(0, np.nan)
    return (100 - 100 / (1 + rs)).fillna(50)


def true_range(df):
    pc = df["close"].shift()
    return pd.concat(
        [df["high"] - df["low"], (df["high"] - pc).abs(), (df["low"] - pc).abs()], axis=1
    ).max(axis=1)


def atr(df, n=14):
    return rma(true_range(df), n)


def adx(df, n=14):
    up = df["high"].diff()
    dn = -df["low"].diff()
    plus = pd.Series(np.where((up > dn) & (up > 0), up, 0.0), index=df.index)
    minus = pd.Series(np.where((dn > up) & (dn > 0), dn, 0.0), index=df.index)
    tr = rma(true_range(df), n).replace(0, np.nan)
    pdi = 100 * rma(plus, n) / tr
    mdi = 100 * rma(minus, n) / tr
    dx = (100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan)).fillna(0)
    return rma(dx, n)


def vwap(df, anchor_shift_hours=2):
    """Session VWAP reset at the gold trading-day open (~22:00 UTC).
    Uses real volume when present; otherwise equal weights (labelled as a TWAP proxy)."""
    tp = (df["high"] + df["low"] + df["close"]) / 3
    has_vol = "volume" in df and df["volume"].fillna(0).sum() > 0
    w = df["volume"].fillna(0) if has_vol else pd.Series(1.0, index=df.index)
    day = (df.index + pd.Timedelta(hours=anchor_shift_hours)).floor("D")
    pv = (tp * w).groupby(day).cumsum()
    ww = w.groupby(day).cumsum().replace(0, np.nan)
    return (pv / ww).fillna(tp), bool(has_vol)


def add_indicators(df, with_vwap=False):
    df = df.copy()
    c = df["close"]
    df["ema20"], df["ema50"], df["ema200"] = ema(c, 20), ema(c, 50), ema(c, 200)
    df["rsi7"], df["rsi14"] = rsi(c, 7), rsi(c, 14)
    df["atr14"] = atr(df, 14)
    df["adx14"] = adx(df, 14)
    if "volume" in df and df["volume"].fillna(0).sum() > 0:
        med = df["volume"].rolling(50, min_periods=20).median().replace(0, np.nan)
        df["rvol"] = df["volume"] / med
    else:
        df["rvol"] = np.nan
    if with_vwap:
        df["vwap"], df.attrs["vwap_volume_weighted"] = vwap(df)
    return df
