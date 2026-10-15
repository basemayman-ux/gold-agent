"""One-off DEMO test of the eToro auto-trade path: open -> move stop -> close.
Always uses the DEMO account, whatever config.json says. Results go to ntfy and the run log."""
import sys
import time

import agent
import autotrade as at

at.agent = agent
p = at.paths("demo")
steps = []


def report(ok):
    text = ("🧪 DEMO TEST " + ("PASSED ✅" if ok else "FAILED ❌") + "\n" + "\n".join(steps))
    print(text)
    agent.notify(text, kind="update")
    sys.exit(0 if ok else 1)


try:
    gold = agent.etoro_gold_id()
    bid, ask = agent.etoro_quote()
    steps.append(f"1. eToro price OK: sell {bid:.2f} / buy {ask:.2f}")
except Exception as e:  # noqa: BLE001
    steps.append(f"1. eToro price FAILED: {e}")
    report(False)

keep = len(sys.argv) > 1 and sys.argv[1] == "keep"
sl, tp = round(ask - 10, 2), round(ask + 10, 2)
try:
    pos, fill, units = at.place(p, "demo", gold, "LONG", 1.0, sl, tp, 20)
    steps.append(f"2. Opened DEMO LONG {units:g} oz at {fill:.2f} (stop {sl}, target {tp}), position {pos}")
except Exception as e:  # noqa: BLE001
    steps.append(f"2. Open FAILED: {e}")
    report(False)

time.sleep(5)
try:
    new_sl = round(fill - 8, 2)
    at.call("PATCH", p["edit"].format(pos), body={"stopLossRate": new_sl})
    steps.append(f"3. Stop moved to {new_sl} OK")
except Exception as e:  # noqa: BLE001
    steps.append(f"3. Move stop FAILED: {e}")

if keep:
    steps.append("4. Left open on purpose: check it in your eToro virtual portfolio, then close it there.")
else:
    time.sleep(60)
    try:
        at.call("POST", p["close"].format(pos), body={"InstrumentID": gold})
        time.sleep(5)
        still = pos in at.open_ids(p)
        steps.append("4. Closed OK" if not still else "4. Close sent, but position still shows open: close it in eToro")
    except Exception as e:  # noqa: BLE001
        steps.append(f"4. Close FAILED: {e} (close it in eToro)")

report(all("FAILED" not in s for s in steps))
