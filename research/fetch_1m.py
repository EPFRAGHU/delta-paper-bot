"""Download 2 years of 1-minute ETH data for the multi-timeframe study.

  Delta India ETHUSD 1m OHLCV  -> data/delta_ETHUSD_1m.npz   (prices we trade on)
  Binance ETHUSDT 1m klines    -> data/binance_ETHUSDT_1m.npz (taker buy volume = order-flow delta proxy)
"""
import json
import os
import sys
import time
import urllib.request

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "..", "data")
DAYS = 730


def get(url):
    for attempt in range(5):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "python-delta-bot"})
            return json.load(urllib.request.urlopen(req, timeout=30))
        except Exception as e:
            print("retry", attempt, e, flush=True)
            time.sleep(3 * (attempt + 1))
    raise RuntimeError(url)


def delta(end):
    start = end - DAYS * 86400
    rows, cur = {}, start
    while cur < end:
        chunk = min(end, cur + 60 * 1999)
        for c in get(f"https://api.india.delta.exchange/v2/history/candles?resolution=1m&symbol=ETHUSD&start={cur}&end={chunk}")["result"]:
            rows[c["time"]] = (c["open"], c["high"], c["low"], c["close"], c["volume"] or 0)
        cur = chunk + 60
        time.sleep(0.15)
    t = np.array(sorted(rows), dtype=np.int64)
    v = np.array([rows[k] for k in t], dtype=np.float64)
    np.savez(os.path.join(DATA, "delta_ETHUSD_1m.npz"), t=t, o=v[:, 0], h=v[:, 1], l=v[:, 2], c=v[:, 3], v=v[:, 4])
    print("delta rows", len(t), flush=True)


def binance(end):
    start = (end - DAYS * 86400) * 1000
    out, cur = [], start
    while cur < end * 1000:
        rows = get(f"https://api.binance.com/api/v3/klines?symbol=ETHUSDT&interval=1m&startTime={cur}&limit=1000")
        if not rows:
            break
        out += [(r[0] // 1000, float(r[5]), float(r[9])) for r in rows]
        cur = rows[-1][0] + 60000
        time.sleep(0.1)
    a = np.array(out)
    np.savez(os.path.join(DATA, "binance_ETHUSDT_1m.npz"), t=a[:, 0].astype(np.int64), vol=a[:, 1], buy=a[:, 2])
    print("binance rows", len(a), flush=True)


if __name__ == "__main__":
    os.makedirs(DATA, exist_ok=True)
    end = int(time.time()) // 60 * 60
    which = sys.argv[1] if len(sys.argv) > 1 else "both"
    if which in ("delta", "both"):
        delta(end)
    if which in ("binance", "both"):
        binance(end)
