"""Does the funding rate (crowding) improve the ETH 4h strategy? Uses free Binance funding history."""
import bisect
import json
import os
import time
import urllib.request

from backtest import load_candles, run, stats
from risk import RiskConfig
from strategy import Params

HERE = os.path.dirname(os.path.abspath(__file__))


def binance_funding(symbol="ETHUSDT", start_ms=1640995200000):
    path = os.path.join(HERE, "data", f"funding_{symbol}.json")
    if os.path.exists(path) and time.time() - os.path.getmtime(path) < 6 * 3600:
        return json.load(open(path))
    out, cur = [], start_ms
    while True:
        url = f"https://fapi.binance.com/fapi/v1/fundingRate?symbol={symbol}&startTime={cur}&limit=1000"
        req = urllib.request.Request(url, headers={"User-Agent": "python-delta-bot"})
        rows = json.load(urllib.request.urlopen(req, timeout=20))
        if not rows:
            break
        out += [(r["fundingTime"] // 1000, float(r["fundingRate"])) for r in rows]
        if len(rows) < 1000:
            break
        cur = rows[-1]["fundingTime"] + 1
        time.sleep(0.3)
    json.dump(out, open(path, "w"))
    return out


def main():
    p = Params(**json.load(open(os.path.join(HERE, "params.json"))))
    candles = load_candles("ETHUSD", "4h", 1460)
    fund = binance_funding()
    ft, fr = [f[0] for f in fund], [f[1] for f in fund]

    def avg_funding(t, n=3):
        # mean of the last n settled funding prints at or before bar close time t (no look-ahead)
        j = bisect.bisect_right(ft, t)
        xs = fr[max(0, j - n):j]
        return sum(xs) / len(xs) if xs else 0.0

    close_t = [c["time"] + 4 * 3600 for c in candles]
    rc = RiskConfig()
    cut = int(len(candles) * 0.6)

    def report(label, flt):
        rows = []
        for name, (a, b) in (("in", (0, cut)), ("out", (cut, len(candles))), ("full", (0, len(candles)))):
            eq, tr, cu = run(candles, "ETHUSD", p, rc, 55.0, a, b, flt)
            s = stats(55.0, eq, tr, cu)
            rows.append(f"{name}: {s['return_pct']:+6.1f}% pf {s['profit_factor']:<5} dd {s['max_dd_pct']:4.1f}% n={s['trades']}")
        print(f"{label:<42} " + " | ".join(rows))

    # funding distribution for context
    sf = sorted(fr[-3 * 365 * 3:])
    q = lambda x: sf[int(x * (len(sf) - 1))]
    print(f"ETH funding per 8h, last ~3y: p10 {q(.1):.5f} p50 {q(.5):.5f} p90 {q(.9):.5f} p97 {q(.97):.5f}\n")

    report("baseline (no funding filter)", None)
    for hi in (0.0002, 0.0003, 0.0005):
        report(f"skip LONG if funding > {hi:.4f}", lambda i, s, hi=hi: not (s > 0 and avg_funding(close_t[i]) > hi))
    for lo in (0.0, -0.0001):
        report(f"skip SHORT if funding < {lo:.4f}", lambda i, s, lo=lo: not (s < 0 and avg_funding(close_t[i]) < lo))
    for hi, lo in ((0.0003, 0.0), (0.0003, -0.0001), (0.0005, -0.0001)):
        report(f"both: long>{hi:.4f} short<{lo:.4f}",
               lambda i, s, hi=hi, lo=lo: not ((s > 0 and avg_funding(close_t[i]) > hi) or
                                               (s < 0 and avg_funding(close_t[i]) < lo)))
    # confirmation variant: only trade with funding agreeing (longs need positive, shorts need negative-ish)
    report("confirm: long needs f>0, short needs f<0.0001",
           lambda i, s: (s > 0 and avg_funding(close_t[i]) > 0) or (s < 0 and avg_funding(close_t[i]) < 0.0001))


if __name__ == "__main__":
    main()
