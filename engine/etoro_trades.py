"""Reads your closed GOLD trades from eToro and rebuilds the P/L workbook.

Output: docs/data/Gold_PL_tracker.xlsx, password-protected with the XLSX_PASSWORD secret
(the repo is public, so the file is encrypted before it is saved there).
Only rewritten when the trade list changes. Needs ETORO_API_KEY / ETORO_USER_KEY with
'Trading - Real: Read' permission.
"""
import hashlib
import io
import json
import os
import sys
from datetime import datetime, timedelta, timezone

import pandas as pd
from openpyxl import Workbook
from openpyxl.formatting.rule import CellIsRule
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

sys.path.insert(0, os.path.dirname(__file__))
import agent  # noqa: E402  (reuses eToro auth, GOLD id lookup, paths, config)

OUT = agent.OUT / "Gold_PL_tracker.xlsx"
HASH = agent.OUT / "pl_tracker.hash"
TRADE_API = "https://public-api.etoro.com/api/v1/trading/info"


def _get(path, params=None):
    import uuid
    import requests
    r = requests.get(f"{TRADE_API}{path}", params=params, timeout=20, headers={
        "x-request-id": str(uuid.uuid4()),
        "x-api-key": os.getenv("ETORO_API_KEY", "").strip(),
        "x-user-key": os.getenv("ETORO_USER_KEY", "").strip()})
    if r.status_code != 200:
        raise RuntimeError(f"eToro {path}: HTTP {r.status_code} {r.text[:200]}")
    return r.json()


def g(d, *names, default=None):
    for n in names:
        if n in d and d[n] is not None:
            return d[n]
    return default


def closed_gold_trades():
    since = agent.CFG.get("pl_history_from", "2026-10-01")
    gold = agent.etoro_gold_id()
    rows, page = [], 1
    while page <= 50:
        js = _get("/trade/history", {"minDate": since, "page": page, "pageSize": 100})
        batch = js if isinstance(js, list) else g(js, "items", "trades", "data", "history", default=[])
        rows += batch
        if len(batch) < 100:
            break
        page += 1
    out = []
    for t in rows:
        if int(g(t, "instrumentId", "instrumentID", default=-1)) != gold:
            continue
        out.append({
            "id": str(g(t, "positionId", "positionID")),
            "dir": "Long" if g(t, "isBuy", default=True) else "Short",
            "units": float(g(t, "units", default=0)),
            "lev": int(g(t, "leverage", default=1)),
            "inv": float(g(t, "initialInvestment", "investment", default=0)),
            "open_t": pd.Timestamp(g(t, "openTimestamp", "openDateTime")).tz_convert("UTC"),
            "close_t": pd.Timestamp(g(t, "closeTimestamp", "closeDateTime")).tz_convert("UTC"),
            "open": float(g(t, "openRate", default=0)), "close": float(g(t, "closeRate", default=0)),
            "sl": float(g(t, "stopLossRate", default=0) or 0), "tp": float(g(t, "takeProfitRate", default=0) or 0),
            "net": float(g(t, "netProfit", default=0)), "fees": float(g(t, "fees", default=0) or 0),
        })
    return sorted(out, key=lambda x: x["open_t"])


def engine_match(trades):
    """A trade counts as an engine trade if it was opened in the signal's direction
    within 20 minutes after a 🟢/🔴 alert."""
    try:
        sigs = json.loads((agent.OUT / "journal.json").read_text()).get("signals", [])
    except Exception:  # noqa: BLE001
        sigs = []
    sigs = [(pd.Timestamp(s["time_utc"]), "Long" if s["direction"] == "LONG" else "Short") for s in sigs]
    for t in trades:
        t["engine"] = "Yes" if any(d == t["dir"] and timedelta(minutes=-2) <= t["open_t"] - st <= timedelta(minutes=20)
                                   for st, d in sigs) else "No"


# ───────────────────────── workbook ─────────────────────────
F = "Arial"
BLUE, BLACK, BOLD = Font(name=F, size=10, color="0000FF"), Font(name=F, size=10), Font(name=F, size=10, bold=True)
SMALL = Font(name=F, size=9, color="666666")
HF, HFILL = Font(name=F, size=10, bold=True, color="FFFFFF"), PatternFill("solid", fgColor="26211A")
THIN = Border(bottom=Side(style="thin", color="D9D9D9"))
GF, RF = PatternFill("solid", fgColor="D9EAD3"), PatternFill("solid", fgColor="F4CCCC")
USD = '$#,##0.00;[Red]($#,##0.00);"-"'
PRICE = "#,##0.00"


def build(trades, stamp):
    wb = Workbook()
    sm = wb.active
    sm.title = "Summary"
    tr, dy, rm = wb.create_sheet("Trades"), wb.create_sheet("Daily"), wb.create_sheet("How to use")
    n = max(len(trades), 1)
    T = lambda c: f"Trades!${c}$2:${c}${n + 1}"  # noqa: E731

    cols = [("Position ID", 13), ("Direction", 9), ("Units (oz)", 10), ("Leverage", 8), ("Amount invested ($)", 12),
            ("Open time (UTC)", 17), ("Close time (UTC)", 17), ("Open time (Dubai)", 17), ("Date (Dubai)", 11),
            ("Minutes held", 9), ("Open price", 10), ("Close price", 10), ("Stop loss", 10), ("Take profit", 10),
            ("Price move ($/oz)", 11), ("Gross P/L ($)", 11), ("Net P/L from eToro ($)", 12), ("of which fees ($)", 11),
            ("Costs: net − gross ($)", 12), ("Result", 8), ("Engine signal?", 9)]
    for i, (h, w) in enumerate(cols, 1):
        c = tr.cell(row=1, column=i, value=h)
        c.font, c.fill = HF, HFILL
        c.alignment = Alignment(wrap_text=True, horizontal="center", vertical="center")
        tr.column_dimensions[c.column_letter].width = w
    tr.row_dimensions[1].height = 32
    for i, t in enumerate(trades):
        x = i + 2
        vals = {1: t["id"], 2: t["dir"], 3: t["units"], 4: t["lev"], 5: t["inv"],
                6: t["open_t"].tz_localize(None).to_pydatetime(), 7: t["close_t"].tz_localize(None).to_pydatetime(),
                11: t["open"], 12: t["close"], 13: t["sl"] or None, 14: t["tp"] or None,
                17: t["net"], 18: t["fees"], 21: t["engine"]}
        for col, v in vals.items():
            tr.cell(row=x, column=col, value=v).font = BLUE
        f = {8: f"=F{x}+4/24", 9: f"=INT(H{x})", 10: f"=ROUND((G{x}-F{x})*1440,1)",
             15: f'=(L{x}-K{x})*IF(B{x}="Short",-1,1)', 16: f"=O{x}*C{x}", 19: f"=Q{x}-P{x}",
             20: f'=IF(Q{x}>0,"Win","Loss")'}
        for col, v in f.items():
            tr.cell(row=x, column=col, value=v).font = BLACK
        for col in range(1, len(cols) + 1):
            tr.cell(row=x, column=col).border = THIN
        for col in (6, 7, 8):
            tr.cell(row=x, column=col).number_format = "dd mmm yyyy hh:mm"
        tr.cell(row=x, column=9).number_format = "dd mmm yyyy"
        for col in (11, 12, 13, 14, 15):
            tr.cell(row=x, column=col).number_format = PRICE
        for col in (5, 16, 17, 18, 19):
            tr.cell(row=x, column=col).number_format = USD
        tr.cell(row=x, column=3).number_format = "0.000000"
    tr.freeze_panes = "C2"
    tr.auto_filter.ref = f"A1:U{n + 1}"
    tr.conditional_formatting.add(f"Q2:Q{n + 1}", CellIsRule(operator="greaterThan", formula=["0"], fill=GF))
    tr.conditional_formatting.add(f"Q2:Q{n + 1}", CellIsRule(operator="lessThan", formula=["0"], fill=RF))

    # Summary
    for col, w in zip("ABCDE", (40, 16, 16, 16, 52)):
        sm.column_dimensions[col].width = w
    sm["A1"] = "Gold (XAUUSD) trading P/L"
    sm["A1"].font = Font(name=F, size=14, bold=True)
    sm["A2"] = f"Read automatically from your eToro account. Last updated {stamp} (Dubai)."
    sm["A2"].font = SMALL

    def hdr(row, names):
        for i, nm in enumerate(names, 1):
            c = sm.cell(row=row, column=i, value=nm)
            c.font, c.fill = HF, HFILL

    items = [
        ("Trades", f'=COUNT({T("Q")})', "0", ""),
        ("Gross P/L (price moves)", f'=SUM({T("P")})', USD, "Units × price move"),
        ("Costs (net − gross)", f'=SUM({T("S")})', USD, "Spread, fees and overnight charges eToro deducted"),
        ("Net P/L", f'=SUM({T("Q")})', USD, "What actually reached your account"),
        ("Winning trades", f'=COUNTIF({T("T")},"Win")', "0", ""),
        ("Losing trades", f'=COUNTIF({T("T")},"Loss")', "0", ""),
        ("Win rate", "=IFERROR(B8/B4,0)", "0.0%", ""),
        ("Average win", f'=IFERROR(AVERAGEIF({T("Q")},">0"),0)', USD, ""),
        ("Average loss", f'=IFERROR(AVERAGEIF({T("Q")},"<0"),0)', USD, ""),
        ("Profit factor", f'=IFERROR(SUMIF({T("Q")},">0")/-SUMIF({T("Q")},"<0"),0)', "0.00", "Above 1.0 = profitable"),
        ("Largest win", f'=MAX({T("Q")})', USD, ""),
        ("Largest loss", f'=MIN({T("Q")})', USD, ""),
        ("Average cost per trade", "=IFERROR(B6/B4,0)", USD, ""),
        ("Costs as % of gross P/L", "=IFERROR(-B6/B5,0)", "0%", "How much of the profit the costs took"),
        ("Average minutes held", f'=IFERROR(AVERAGE({T("J")}),0)', "0.0", ""),
    ]
    hdr(3, ["Measure", "Value", "", "", "Note"])
    for i, (lab, f, fmt, note) in enumerate(items):
        r = 4 + i
        sm.cell(row=r, column=1, value=lab).font = BOLD if lab == "Net P/L" else BLACK
        c = sm.cell(row=r, column=2, value=f)
        c.font, c.number_format = (BOLD if lab == "Net P/L" else BLACK), fmt
        sm.cell(row=r, column=5, value=note).font = SMALL
        for col in range(1, 6):
            sm.cell(row=r, column=col).border = THIN
    sm.conditional_formatting.add("B7", CellIsRule(operator="greaterThan", formula=["0"], fill=GF))
    sm.conditional_formatting.add("B7", CellIsRule(operator="lessThan", formula=["0"], fill=RF))
    r0 = 4 + len(items) + 1
    hdr(r0, ["By group", "Trades", "Net P/L ($)", "Win rate", ""])
    groups = [("Long trades", "B", "Long"), ("Short trades", "B", "Short"),
              ("Engine signals (Yes)", "U", "Yes"), ("Own trades (No)", "U", "No")]
    for i, (lab, col, val) in enumerate(groups):
        r = r0 + 1 + i
        sm.cell(row=r, column=1, value=lab).font = BLACK
        sm.cell(row=r, column=2, value=f'=COUNTIFS({T(col)},"{val}")').number_format = "0"
        sm.cell(row=r, column=3, value=f'=SUMIFS({T("Q")},{T(col)},"{val}")').number_format = USD
        sm.cell(row=r, column=4, value=f'=IFERROR(COUNTIFS({T(col)},"{val}",{T("T")},"Win")/B{r},0)').number_format = "0%"
        for c in range(1, 6):
            sm.cell(row=r, column=c).border = THIN
    sm.conditional_formatting.add(f"C{r0 + 1}:C{r0 + 4}", CellIsRule(operator="greaterThan", formula=["0"], fill=GF))
    sm.conditional_formatting.add(f"C{r0 + 1}:C{r0 + 4}", CellIsRule(operator="lessThan", formula=["0"], fill=RF))
    sm.freeze_panes = "A4"

    # Daily
    for col, w in zip("ABCDEF", (16, 8, 13, 13, 13, 13)):
        dy.column_dimensions[col].width = w
    for i, h in enumerate(["Date (Dubai)", "Trades", "Gross P/L ($)", "Costs ($)", "Net P/L ($)", "Running net ($)"], 1):
        c = dy.cell(row=1, column=i, value=h)
        c.font, c.fill = HF, HFILL
    if trades:
        first = (trades[0]["open_t"] + pd.Timedelta(hours=4)).tz_localize(None).normalize()
        last = (pd.Timestamp.now(tz="UTC") + pd.Timedelta(hours=4)).tz_localize(None).normalize()
        for i, d in enumerate(pd.date_range(first, last, freq="D")):
            x = i + 2
            dy.cell(row=x, column=1, value=d.to_pydatetime()).number_format = "ddd dd mmm yyyy"
            dy.cell(row=x, column=2, value=f'=COUNTIFS({T("I")},A{x})')
            for col, s in ((3, "P"), (4, "S"), (5, "Q")):
                dy.cell(row=x, column=col, value=f'=SUMIFS({T(s)},{T("I")},A{x})').number_format = USD
            dy.cell(row=x, column=6, value=f"=E{x}" if i == 0 else f"=F{x - 1}+E{x}").number_format = USD
            for col in range(1, 7):
                dy.cell(row=x, column=col).border = THIN
        end = len(pd.date_range(first, last, freq="D")) + 1
        dy.conditional_formatting.add(f"E2:E{end}", CellIsRule(operator="greaterThan", formula=["0"], fill=GF))
        dy.conditional_formatting.add(f"E2:E{end}", CellIsRule(operator="lessThan", formula=["0"], fill=RF))
    dy.freeze_panes = "A2"

    rm.column_dimensions["A"].width = 26
    rm.column_dimensions["B"].width = 100
    info = [("What this is", "Every closed GOLD trade on your eToro account, read automatically, with the true result after costs."),
            ("Updates", "Rebuilt by your gold agent whenever a trade closes. Download it again from the website to see new trades."),
            ("Net P/L from eToro", "eToro's own net profit for the trade. Gross P/L is units × price move; the difference is costs."),
            ("Engine signal?", "Yes = opened in the same direction within 20 minutes after one of your 🟢/🔴 alerts."),
            ("Times", "eToro uses UTC. 'Open time (Dubai)' adds 4 hours; daily totals use the Dubai date."),
            ("Privacy", "This file is password-protected because the website is public.")]
    for i, (k, v) in enumerate(info, 1):
        rm.cell(row=i, column=1, value=k).font = BOLD
        c = rm.cell(row=i, column=2, value=v)
        c.font, c.alignment = BLACK, Alignment(wrap_text=True, vertical="top")
    wb.calculation.fullCalcOnLoad = True
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def encrypt(data, password):
    import msoffcrypto
    src, dst = io.BytesIO(data), io.BytesIO()
    f = msoffcrypto.OfficeFile(src)
    f.encrypt(password, dst)
    return dst.getvalue()


def main():
    if not agent.etoro_on():
        print("P/L tracker skipped: eToro keys not set")
        return
    pw = os.getenv("XLSX_PASSWORD", "").strip()
    if not pw:
        print("P/L tracker skipped: XLSX_PASSWORD secret not set (needed because the repo is public)")
        return
    try:
        trades = closed_gold_trades()
    except Exception as e:  # noqa: BLE001
        print(f"P/L tracker: could not read eToro trades: {e}")
        return
    engine_match(trades)
    key = hashlib.sha256(json.dumps([[t["id"], t["net"], t["engine"]] for t in trades]).encode()).hexdigest()
    if HASH.exists() and HASH.read_text().strip() == key and OUT.exists():
        print(f"P/L tracker: {len(trades)} gold trades, no change")
        return
    stamp = (datetime.now(timezone.utc) + timedelta(hours=4)).strftime("%a %d %b %Y, %H:%M")
    OUT.write_bytes(encrypt(build(trades, stamp), pw))
    HASH.write_text(key)
    # Public logs: no dollar amounts. Cost per oz tells us whether eToro's net profit includes the spread.
    oz = sum(t["units"] for t in trades) or 1
    gross = sum(t["units"] * (t["close"] - t["open"]) * (1 if t["dir"] == "Long" else -1) for t in trades)
    cost_oz = (sum(t["net"] for t in trades) - gross) / oz
    print(f"P/L tracker updated: {len(trades)} gold trades; costs per oz {cost_oz:+.2f}")


if __name__ == "__main__":
    main()
