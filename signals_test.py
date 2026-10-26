"""Test extra free data as entry filters on the ETH 4h strategy.

Sources (no API keys):
  - Binance ETHUSDT / BTCUSDT 4h klines: volume + taker-buy volume (order flow), BTC trend
  - alternative.me Fear & Greed index (same idea as CoinMarketCap's, free full history)
A filter is only worth adding if it helps OUT-OF-SAMPLE, not just in-sample.
"""
import bisect
import json
import os
import time
import urllib.request

from backtest import load_candles, run, stats
from risk import RiskConfig
from strategy import Params, ema

HERE = os.path.dirname(os.path.abspath(__file__))


def fetch_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "python-delta-bot"})
    return json.load(urllib.request.urlopen(req, timeout=20))


def cache(name, fn):
    path = os.path.join(HERE, "data", name)
    if os.path.exists(path) and time.time() - os.path.getmtime(path) < 6 * 3600:
        return json.load(open(path))
    data = fn()
    json.dump(data, open(path, "w"))
    return data


def binance_klines(symbol, start_s):
    def get():
        out, cur = [], start_s * 1000
        while True:
            rows = fetch_json(f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval=4h&startTime={cur}&limit=1000")
            if not rows:
                break
            # open time, volume, taker buy base volume, close
            out += [[r[0] // 1000, float(r[5]), float(r[9]), float(r[4])] for r in rows]
            if len(rows) < 1000:
                break
            cur = rows[-1][0] + 1
            time.sleep(0.2)
        return out
    return cache(f"binance_{symbol}_4h.json", get)


def fear_greed():
    def get():
        d = fetch_json("https://api.alternative.me/fng/?limit=0&format=json")["data"]
        return sorted([int(x["timestamp"]), int(x["value"])] for x in d)
    return cache("fear_greed.json", get)


def main():
    p = Params(**json.load(open(os.path.join(HERE, "params.json"))))
    candles = load_candles("ETHUSD", "4h", 1460)
    t0 = candles[0]["time"] - 300 * 4 * 3600
    eth = {k[0]: k for k in binance_klines("ETHUSDT", t0)}
    btc_rows = binance_klines("BTCUSDT", t0)
    btc_t = [r[0] for r in btc_rows]
    btc_ema = ema([r[3] for r in btc_rows], 200)
    fg = fear_greed()
    fg_t = [x[0] for x in fg]

    times = [c["time"] for c in candles]
    # rolling 20-bar average Binance ETH volume, aligned to Delta candles
    vol = [eth[t][1] if t in eth else None for t in times]
    taker = [eth[t][2] / eth[t][1] if t in eth and eth[t][1] else 0.5 for t in times]

    def vol_ratio(i):
        hist = [v for v in vol[max(0, i - 20):i] if v]
        return (vol[i] / (sum(hist) / len(hist))) if vol[i] and hist else 1.0

    def btc_trend(i):
        j = bisect.bisect_right(btc_t, times[i]) - 1
        if j < 205:
            return 0
        up = btc_rows[j][3] > btc_ema[j] and btc_ema[j] > btc_ema[j - 5]
        dn = btc_rows[j][3] < btc_ema[j] and btc_ema[j] < btc_ema[j - 5]
        return 1 if up else -1 if dn else 0

    def fng(i):
        j = bisect.bisect_right(fg_t, times[i] + 4 * 3600) - 1
        return fg[j][1] if j >= 0 else 50

    cut = int(len(candles) * 0.6)
    rc = RiskConfig()

    def report(label, flt):
        out = []
        for name, (a, b) in (("in", (0, cut)), ("OUT", (cut, len(candles))), ("full", (0, len(candles)))):
            eq, tr, cu = run(candles, "ETHUSD", p, rc, 55.0, a, b, flt)
            s = stats(55.0, eq, tr, cu)
            out.append(f"{name}: {s['return_pct']:+6.1f}% pf {str(s['profit_factor']):<5} dd {s['max_dd_pct']:4.1f}% n={s['trades']}")
        print(f"{label:<44} " + " | ".join(out))

    report("baseline", None)
    for k in (1.0, 1.3, 1.6):
        report(f"volume confirm: breakout vol > {k}x avg", lambda i, s, k=k: vol_ratio(i) > k)
    for k in (0.52, 0.55):
        report(f"order flow: taker side agrees >{k:.0%}",
               lambda i, s, k=k: (s > 0 and taker[i] > k) or (s < 0 and taker[i] < 1 - k))
    report("BTC trend agrees (not against)", lambda i, s: btc_trend(i) != -s)
    report("BTC trend agrees (strict)", lambda i, s: btc_trend(i) == s)
    for lo, hi in ((20, 80), (25, 75)):
        report(f"fear&greed: no long >{hi}, no short <{lo}",
               lambda i, s, lo=lo, hi=hi: not ((s > 0 and fng(i) > hi) or (s < 0 and fng(i) < lo)))


if __name__ == "__main__":
    main()
