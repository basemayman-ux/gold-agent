"""DEMO test of the eToro auto-trade path, for a BUY and then a SHORT:
open -> move stop -> close, each. Always uses the DEMO account, whatever config.json says.
Results go to ntfy and the run log."""
import sys
import time

import agent
import autotrade as at

at.agent = agent
p = at.paths("demo")
steps = []
keep = len(sys.argv) > 1 and sys.argv[1] == "keep"


def report():
    ok = all("FAILED" not in s for s in steps)
    text = "🧪 DEMO TEST (buy + short) " + ("PASSED ✅" if ok else "FAILED ❌") + "\n" + "\n".join(steps)
    print(text)
    agent.notify(text, kind="update")
    sys.exit(0 if ok else 1)


try:
    gold = agent.etoro_gold_id()
    bid, ask = agent.etoro_quote()
    steps.append(f"Price OK: sell {bid:.2f} / buy {ask:.2f}")
except Exception as e:  # noqa: BLE001
    steps.append(f"Price FAILED: {e}")
    report()

for direction in ("LONG", "SHORT"):
    tag = "BUY" if direction == "LONG" else "SHORT"
    sign = 1 if direction == "LONG" else -1
    bid, ask = agent.etoro_quote()
    px = ask if direction == "LONG" else bid
    sl, tp = round(px - sign * 10, 2), round(px + sign * 10, 2)
    try:
        pos, fill, units = at.place(p, "demo", gold, direction, 1.0, sl, tp, 20)
        steps.append(f"{tag}: opened {units:g} oz at {fill:.2f} (stop {sl}, target {tp})")
    except Exception as e:  # noqa: BLE001
        steps.append(f"{tag}: open FAILED: {e}")
        continue
    time.sleep(5)
    try:
        new_sl = round(fill - sign * 8, 2)
        at.call("PATCH", p["edit"].format(pos), body={"stopLossRate": new_sl})
        steps.append(f"{tag}: stop moved to {new_sl} OK")
    except Exception as e:  # noqa: BLE001
        steps.append(f"{tag}: move stop FAILED: {e}")
    if keep:
        steps.append(f"{tag}: left open on purpose, close it in eToro")
        continue
    time.sleep(30)
    try:
        at.call("POST", p["close"].format(pos), body={"InstrumentID": gold})
        closed = False
        for _ in range(6):
            time.sleep(3)
            if pos not in at.open_ids(p):
                closed = True
                break
        steps.append(f"{tag}: closed OK" if closed else f"{tag}: close FAILED (still open, close it in eToro)")
    except Exception as e:  # noqa: BLE001
        steps.append(f"{tag}: close FAILED: {e} (close it in eToro)")

report()
