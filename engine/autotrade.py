"""Places the engine's 🟢/🔴 signals on eToro automatically, and manages them.

Each signal opens ONE position with the signal's stop and its target at 1R (eToro closes it
there by itself). When price reaches 0.7R, the stop is moved to the entry price.
(This rule won the replay of the first 22 live signals.)

Safety:
  * config.json "autotrade.mode": "off" | "demo" | "real".  Real ALSO needs the secret
    ETORO_ALLOW_REAL=yes, so one typo can never put real money at risk.
  * max trades per day, a daily loss stop (in R), one trade at a time, a size cap,
    a spread cap, and a stop loss on every position.
State is kept in docs/data/autotrade.json (public repo: no balances or dollar P/L stored).
"""
import json
import math
import os
import time
import uuid
from datetime import timedelta

import pandas as pd
import requests

import agent

API = "https://public-api.etoro.com"
STATE = agent.OUT / "autotrade.json"
DEFAULTS = {"mode": "off", "leverage": 20, "max_trades_per_day": 4, "daily_loss_stop_r": 2.0,
            "max_units": 10.0, "max_spread": 1.0}


def cfg():
    c = {**DEFAULTS, **agent.CFG.get("autotrade", {})}
    mode = str(c["mode"]).lower()
    if mode == "real" and os.getenv("ETORO_ALLOW_REAL", "").strip().lower() != "yes":
        print("Auto-trade: config says real but ETORO_ALLOW_REAL secret is not 'yes' -> using demo")
        mode = "demo"
    if mode not in ("demo", "real") or not agent.etoro_on():
        mode = "off"
    c["mode"] = mode
    return c


def paths(mode):
    d = mode == "demo"
    return {
        "open": "/api/v2/trading/execution/demo/orders" if d else "/api/v2/trading/execution/orders",
        "lookup": "/api/v2/trading/info/demo/orders:lookup" if d else "/api/v2/trading/info/orders:lookup",
        "close": ("/api/v1/trading/execution/demo/market-close-orders/positions/{}" if d
                  else "/api/v1/trading/execution/market-close-orders/positions/{}"),
        "edit": "/api/v2/trading/demo/positions/{}" if d else "/api/v2/trading/positions/{}",
        "pnl": "/api/v1/trading/info/demo/pnl" if d else "/api/v1/trading/info/real/pnl",
    }


def call(method, path, body=None, params=None, ref=None):
    r = requests.request(method, API + path, json=body, params=params, timeout=20, headers={
        "x-request-id": ref or str(uuid.uuid4()),
        "x-api-key": os.getenv("ETORO_API_KEY", "").strip(),
        "x-user-key": os.getenv("ETORO_USER_KEY", "").strip()})
    if r.status_code not in (200, 201, 202):
        raise RuntimeError(f"{method} {path}: HTTP {r.status_code} {r.text[:700]}")
    return r.json() if r.text else {}


def load():
    try:
        return json.loads(STATE.read_text())
    except Exception:  # noqa: BLE001
        return {"trades": []}


def save(st):
    st["trades"] = st["trades"][-200:]
    STATE.write_text(json.dumps(st, indent=1))
    try:
        open("/tmp/force_commit", "a").close()
    except Exception:  # noqa: BLE001
        pass


def positions(p):
    js = call("GET", p["pnl"])
    pos = (js.get("clientPortfolio") or js).get("positions", [])
    out = {}
    for x in pos:
        pid = int(x.get("positionID", x.get("positionId", 0)) or 0)
        if pid:
            out[pid] = x
    return out


def open_ids(p):
    return set(positions(p))


def place(p, mode, gold, direction, units, sl, tp, lev):
    """Market order; returns (positionId, fill price, units).
    Confirms the fill two ways: eToro's order lookup, and a new GOLD position appearing in the portfolio."""
    ref = str(uuid.uuid4())
    body = {"action": "open", "transaction": "buy" if direction == "LONG" else "sellShort",
            "instrumentId": gold, "settlementType": "cfd",          # eToro: symbol OR instrumentId, not both
            "orderType": "mkt", "leverage": lev, "units": units, "orderCurrency": "usd",
            "stopLossRate": round(sl, 2), "takeProfitRate": round(tp, 2), "stopLossType": "fixed"}
    try:
        before = open_ids(p)
    except Exception:  # noqa: BLE001
        before = None
    resp = call("POST", p["open"], body=body, ref=ref) or {}
    oid = resp.get("orderId") or resp.get("orderID")
    last = f"order accepted (orderId {oid})"
    for _ in range(15):                                 # up to ~30 s
        time.sleep(2)
        # 1) order lookup (by orderId when eToro gave one, else by our request id)
        try:
            o = call("GET", p["lookup"], params={"orderId": oid} if oid else {"referenceId": ref})
            st = o.get("status") or {}
            name = str(st.get("name", st.get("id", "")))
            ex = o.get("positionExecutions") or []
            if ex:
                od = ex[0].get("openingData") or {}
                return int(ex[0]["positionId"]), float(od.get("avgPrice") or 0), float(od.get("units") or units)
            if name.lower() in ("rejected", "canceled", "cancelled", "expired", "4", "7", "8"):
                raise RuntimeError(f"order {name}: {st.get('errorMessage') or st.get('errorCode') or ''}")
            last = f"lookup status '{name}'"
        except RuntimeError as e:
            if str(e).startswith("order "):
                raise
            last = f"lookup error: {e}"
        # 2) portfolio: a new GOLD position that was not there before the order
        if before is not None:
            try:
                for pid, x in positions(p).items():
                    inst = int(x.get("instrumentID", x.get("instrumentId", -1)) or -1)
                    if pid not in before and inst == gold:
                        return pid, float(x.get("openRate") or 0), float(x.get("units") or units)
            except Exception as e:  # noqa: BLE001
                last += f" | portfolio error: {e}"
    raise RuntimeError(f"order not confirmed within 30 s ({last[:400]})")


def tag(mode):
    return "🧪 DEMO" if mode == "demo" else "💰 REAL"


def on_signal(rec, plan, size, now):
    """Called right after a 🟢/🔴 alert. Opens the A/B positions if every safety check passes."""
    c = cfg()
    if c["mode"] == "off":
        return
    st, p, mode = load(), paths(c["mode"]), c["mode"]
    today = (now + timedelta(hours=4)).date().isoformat()
    todays = [t for t in st["trades"] if t["day"] == today and t["mode"] == mode]
    lost_r = -sum(t.get("r", 0) for t in todays if t.get("r", 0) < 0)
    why = None
    if not size or size.get("oz", 0) <= 0:
        why = "no position size (ACCOUNT_EQUITY not set)"
    elif any(t["status"] == "open" and t["mode"] == mode for t in st["trades"]):
        why = "a previous auto-trade is still open"
    elif len(todays) >= c["max_trades_per_day"]:
        why = f"daily limit of {c['max_trades_per_day']} trades reached"
    elif lost_r >= c["daily_loss_stop_r"]:
        why = f"daily loss stop hit ({lost_r:.1f}R lost today)"
    else:
        try:
            bid, ask = agent.etoro_quote()
            if ask - bid > c["max_spread"]:
                why = f"spread too wide (${ask - bid:.2f})"
        except Exception as e:  # noqa: BLE001
            why = f"no eToro quote ({e})"
    if why:
        agent.notify(f"{tag(mode)} auto-trade skipped: {why}.", kind="update")
        return

    units = math.floor(min(size["oz"], c["max_units"]) * 100) / 100
    gold = agent.etoro_gold_id()
    d = rec["direction"]
    sign = 1 if d == "LONG" else -1
    t = {"sig": rec["time_utc"], "day": today, "mode": mode, "dir": d, "sl": plan["sl"], "tp": plan["tp1"],
         "R": plan["R"], "status": "open", "be": False, "opened": now.isoformat()}
    try:
        t["pos"], fill, t["units"] = place(p, mode, gold, d, units, plan["sl"], plan["tp1"], c["leverage"])
    except Exception as e:  # noqa: BLE001
        agent.notify(f"⚠️ {tag(mode)} auto-trade not opened: {e}", kind="signal")
        return
    t["entry"] = round(fill or plan["entry"], 2)
    t["be_px"] = round(t["entry"] + sign * 0.7 * plan["R"], 2)
    st["trades"].append(t)
    save(st)
    agent.notify(f"{tag(mode)} 🤖 opened {d} GOLD {t['units']:.2f} oz at {t['entry']:.2f}\n"
                 f"Stop {plan['sl']:.2f} · target {plan['tp1']:.2f} (1R) · stop → entry at {t['be_px']:.2f}.",
                 kind="update")


def manage(journal):
    """Every run: move the stop to entry once price reaches 0.7R, and record closed trades."""
    c = cfg()
    st = load()
    live = [t for t in st["trades"] if t["status"] == "open"]
    if not live:
        return
    changed = False
    by_sig = {s["time_utc"]: s for s in journal.get("signals", [])}
    for t in live:
        mode = t["mode"]
        p = paths(mode)
        try:
            ids = open_ids(p)
        except Exception as e:  # noqa: BLE001
            print(f"Auto-trade manage: {e}")
            continue
        sig = by_sig.get(t["sig"], {})
        if t["pos"] in ids:
            if t["be"]:
                continue
            reached = bool(sig.get("tp1_hit"))           # engine saw 0.7R on the chart
            try:
                bid, ask = agent.etoro_quote()
                reached = reached or (bid >= t["be_px"] if t["dir"] == "LONG" else ask <= t["be_px"])
            except Exception:  # noqa: BLE001
                pass
            if reached:
                try:
                    call("PATCH", p["edit"].format(t["pos"]), body={"stopLossRate": round(t["entry"], 2)})
                    t["be"] = changed = True
                    agent.notify(f"{tag(mode)} 📍 0.7R reached: stop moved to entry {t['entry']:.2f}.", kind="update")
                except Exception as e:  # noqa: BLE001
                    agent.notify(f"⚠️ {tag(mode)} could not move stop to entry: {e}. Do it manually in eToro.",
                                 kind="signal")
        else:
            t["status"] = "closed"
            t["closed"] = pd.Timestamp.now(tz="UTC").isoformat()
            t["r"] = sig.get("r", 0) if sig.get("status") == "closed" else (0.0 if t["be"] else -1.0)
            changed = True
            agent.notify(f"{tag(mode)} 🤖 auto-trade {t['dir']} closed (≈{t['r']:+.2f}R).", kind="update")
    if changed:
        save(st)
