"""Backtest the strategy on real Delta Exchange India candles.

Usage:
  python backtest.py                 # default symbols, walk-forward param search
  python backtest.py --no-search     # just run default Params
"""
import argparse
import itertools
import json
import os
import time
from datetime import datetime, timezone

from delta_client import DeltaClient, _RES_SECONDS
from risk import RiskConfig, Guard, size_contracts, TAKER_FEE, SLIPPAGE
from strategy import Params, Indicators

CACHE = os.path.join(os.path.dirname(__file__), "data")
CONTRACT_VALUE = {"BTCUSD": 0.001, "ETHUSD": 0.01, "SOLUSD": 1.0}


def load_candles(symbol, resolution, days):
    os.makedirs(CACHE, exist_ok=True)
    path = os.path.join(CACHE, f"{symbol}_{resolution}_{days}d.json")
    if os.path.exists(path) and time.time() - os.path.getmtime(path) < 6 * 3600:
        return json.load(open(path))
    end = int(time.time()) // _RES_SECONDS[resolution] * _RES_SECONDS[resolution]
    candles = DeltaClient().candles(symbol, resolution, end - days * 86400, end)
    json.dump(candles, open(path, "w"))
    return candles


def run(candles, symbol, p: Params, rc: RiskConfig, equity=55.0, start=0, end=None, entry_filter=None):
    """Bar-by-bar simulation. Signals on bar i close, fill at bar i+1 open.
    entry_filter(i, signal) -> bool can veto entries (used to test extra data like funding)."""
    ind = Indicators(candles, p)
    cv = CONTRACT_VALUE[symbol]
    end = end or len(candles)
    guard = Guard(rc, equity)
    pos = None  # dict(side, qty, entry, stop, best)
    trades, curve = [], []
    fee = TAKER_FEE + SLIPPAGE
    o = [float(c["open"]) for c in candles]

    for i in range(max(start, 1), end - 1):
        day = datetime.fromtimestamp(candles[i]["time"], timezone.utc).date()
        # manage open position against bar i's range (stop placed at exchange)
        if pos:
            hit = (pos["side"] > 0 and ind.l[i] <= pos["stop"]) or (pos["side"] < 0 and ind.h[i] >= pos["stop"])
            if hit:
                px = pos["stop"]
                if pos["side"] > 0 and o[i] < px:   # gapped through
                    px = o[i]
                if pos["side"] < 0 and o[i] > px:
                    px = o[i]
                pnl = pos["side"] * (px - pos["entry"]) * pos["qty"] * cv - (px + pos["entry"]) * pos["qty"] * cv * fee
                equity += pnl
                trades.append({"t_in": pos["t_in"], "t": candles[i]["time"], "side": pos["side"],
                               "entry": pos["entry"], "exit": px, "qty": pos["qty"], "pnl": pnl, "reason": "stop"})
                pos = None
            else:
                pos["best"] = max(pos["best"], ind.h[i]) if pos["side"] > 0 else min(pos["best"], ind.l[i])
                pos["stop"] = ind.trail_stop(i, pos["side"], pos["stop"], pos["best"])

        guard.update(equity, day)
        curve.append(equity)
        if guard.halted:
            break
        if pos is None and guard.can_enter(equity):
            sig = ind.entry_signal(i)
            if sig and entry_filter and not entry_filter(i, sig):
                sig = 0
            if sig:
                entry = o[i + 1]
                stop = entry - sig * p.atr_stop * ind.atr[i]
                qty = size_contracts(equity, entry, stop, cv, rc)
                if qty:
                    pos = {"side": sig, "qty": qty, "entry": entry, "stop": stop, "best": entry,
                           "t_in": candles[i + 1]["time"]}

    if pos:  # mark-to-market close at the end
        px = ind.c[end - 1]
        pnl = pos["side"] * (px - pos["entry"]) * pos["qty"] * cv - (px + pos["entry"]) * pos["qty"] * cv * fee
        equity += pnl
        trades.append({"t_in": pos["t_in"], "t": candles[end - 1]["time"], "side": pos["side"],
                       "entry": pos["entry"], "exit": px, "qty": pos["qty"], "pnl": pnl, "reason": "open (marked)"})
    return equity, trades, curve


def stats(start_eq, end_eq, trades, curve):
    wins = [t for t in trades if t["pnl"] > 0]
    peak, mdd = start_eq, 0.0
    for e in curve:
        peak = max(peak, e)
        mdd = max(mdd, (peak - e) / peak)
    gross_win = sum(t["pnl"] for t in wins)
    gross_loss = -sum(t["pnl"] for t in trades if t["pnl"] <= 0)
    return {
        "return_pct": round(100 * (end_eq / start_eq - 1), 1),
        "trades": len(trades),
        "win_rate": round(100 * len(wins) / len(trades), 1) if trades else 0,
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss else float("inf"),
        "max_dd_pct": round(100 * mdd, 1),
    }


GRID = {
    "entry_len": [20, 40, 55],
    "exit_len": [10, 20],
    "atr_stop": [1.5, 2.5],
    "atr_trail": [3.0, 4.5],
    "adx_min": [0.0, 20.0],
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default="BTCUSD,ETHUSD,SOLUSD")
    ap.add_argument("--resolution", default="1h")
    ap.add_argument("--days", type=int, default=730)
    ap.add_argument("--equity", type=float, default=55.0)
    ap.add_argument("--no-search", action="store_true")
    ap.add_argument("--risk", type=float, default=RiskConfig.risk_per_trade)
    a = ap.parse_args()
    rc = RiskConfig(risk_per_trade=a.risk, max_single_risk=max(RiskConfig.max_single_risk, a.risk * 1.5))
    data = {s: load_candles(s, a.resolution, a.days) for s in a.symbols.split(",")}
    for s, c in data.items():
        print(f"{s}: {len(c)} candles {datetime.fromtimestamp(c[0]['time'], timezone.utc):%Y-%m-%d}"
              f" -> {datetime.fromtimestamp(c[-1]['time'], timezone.utc):%Y-%m-%d}")

    params_path = os.path.join(os.path.dirname(__file__), "params.json")
    best = Params(**json.load(open(params_path))) if a.no_search and os.path.exists(params_path) else Params()
    if not a.no_search:
        # in-sample = first 60% of each series; score = mean profit factor across symbols, penalise drawdown
        results = []
        keys = list(GRID)
        for combo in itertools.product(*GRID.values()):
            p = Params(**dict(zip(keys, combo)))
            score, rows = 0.0, []
            for s, c in data.items():
                cut = int(len(c) * 0.6)
                eq, tr, cu = run(c, s, p, rc, a.equity, 0, cut)
                st = stats(a.equity, eq, tr, cu)
                rows.append(st)
                pf = min(st["profit_factor"], 3.0)
                score += pf - st["max_dd_pct"] / 100
            results.append((score / len(data), p, rows))
        results.sort(key=lambda r: r[0], reverse=True)
        print("\nTop in-sample parameter sets:")
        for sc, p, rows in results[:5]:
            print(f"  score={sc:.2f} {p}  " + " | ".join(f"ret {r['return_pct']}% pf {r['profit_factor']} dd {r['max_dd_pct']}%" for r in rows))
        best = results[0][1]

    print(f"\nChosen params: {best}")
    for label, frac in (("IN-SAMPLE (first 60%)", (0, 0.6)), ("OUT-OF-SAMPLE (last 40%)", (0.6, 1.0)), ("FULL", (0, 1.0))):
        print(f"\n{label}")
        for s, c in data.items():
            st_i, en_i = int(len(c) * frac[0]), int(len(c) * frac[1])
            eq, tr, cu = run(c, s, best, rc, a.equity, st_i, en_i)
            st = stats(a.equity, eq, tr, cu)
            bh = 100 * (float(c[en_i - 1]["close"]) / float(c[st_i]["open"]) - 1)
            print(f"  {s}: ${a.equity:.0f} -> ${eq:.2f}  {st}  buy&hold {bh:.1f}%")
    json.dump(best.__dict__, open(os.path.join(os.path.dirname(__file__), "params.json"), "w"), indent=2)
    print("\nSaved chosen params to params.json")


if __name__ == "__main__":
    main()
