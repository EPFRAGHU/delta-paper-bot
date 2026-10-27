"""How much do late cloud runs hurt the 1h strategy?

GitHub's scheduler starts the bot some minutes after each candle closes, so entries fill late.
Model: signal on 1h bar i close, fill at the price `delay` minutes into bar i+1 (taken from 5m candles).
Stops are unaffected: each cloud run replays 5m candles, so a touched stop is still filled at the stop.
"""
import json
import os

from backtest import load_candles, run, stats
from risk import RiskConfig
from strategy import Params

HERE = os.path.dirname(os.path.abspath(__file__))


def main():
    import sys
    res = sys.argv[1] if len(sys.argv) > 1 else "1h"
    pfile = {"1h": "params_1h_eth.json", "4h": "params_4h_eth.json"}[res]
    p = Params(**json.load(open(os.path.join(HERE, pfile))))
    c1h = load_candles("ETHUSD", res, 730)
    c5 = {c["time"]: float(c["open"]) for c in load_candles("ETHUSD", "5m", 730)}
    cut = int(len(c1h) * 0.6)
    print(f"{res} ETH, {len(c1h)} bars, params {p}\n")
    for delay in (0, 5, 15, 30, 45):
        def fill(i, delay=delay):
            t = c1h[i + 1]["time"] + delay * 60
            return c5.get(t, float(c1h[i + 1]["open"]))
        row = []
        for name, (a, b) in (("in", (0, cut)), ("OUT", (cut, len(c1h))), ("full", (0, len(c1h)))):
            eq, tr, cu = run(c1h, "ETHUSD", p, RiskConfig(), 55.0, a, b, entry_price=fill)
            s = stats(55.0, eq, tr, cu)
            row.append(f"{name} {s['return_pct']:+6.1f}% pf {s['profit_factor']} dd {s['max_dd_pct']}%")
        print(f"entry {delay:>2} min late: " + " | ".join(row))


if __name__ == "__main__":
    main()
