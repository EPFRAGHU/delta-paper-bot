"""Compare popular GitHub day-breakout strategies against the bot, same fees and risk sizing.

  Larry Williams volatility breakout: long if price > day_open + k * yesterday_range,
                                      short if price < day_open - k * yesterday_range.
  Dual Thrust: range = max(HH - LC, HC - LL) over the last N days; same triggers with k.
Both: one trade per UTC day max, stop at the day's open, exit at the day's close. Simulated on 1h bars.
"""
import os
from datetime import datetime, timezone

from backtest import load_candles, stats
from risk import RiskConfig, size_contracts, TAKER_FEE, SLIPPAGE

CV = 0.01


def daily(c1h):
    days = {}
    for c in c1h:
        d = datetime.fromtimestamp(c["time"], timezone.utc).date()
        days.setdefault(d, []).append(c)
    return [(d, bars) for d, bars in sorted(days.items()) if len(bars) == 24]


def simulate(days, rng_fn, k, start_frac=0.0, end_frac=1.0, equity=55.0):
    rc, fee = RiskConfig(), TAKER_FEE + SLIPPAGE
    a, b = int(len(days) * start_frac), int(len(days) * end_frac)
    trades, curve = [], []
    for di in range(max(a, 5), b):
        _, bars = days[di]
        r = rng_fn(days, di)
        op = float(bars[0]["open"])
        up, dn = op + k * r, op - k * r
        pos = None
        for bar in bars:
            h, l = float(bar["high"]), float(bar["low"])
            if pos is None:
                side = 1 if h >= up else -1 if l <= dn else 0
                if side:
                    entry = up if side > 0 else dn
                    qty = size_contracts(equity, entry, op, CV, rc)
                    if not qty:
                        break
                    pos = (side, entry, qty)
                    # stop is the day open; check the rest of this bar conservatively
                    if (side > 0 and l <= op) or (side < 0 and h >= op):
                        px = op
                        break
                continue
            side, entry, qty = pos
            if (side > 0 and l <= op) or (side < 0 and h >= op):
                px = op
                break
        else:
            px = float(bars[-1]["close"])
        if pos:
            side, entry, qty = pos
            pnl = side * (px - entry) * qty * CV - (px + entry) * qty * CV * fee
            equity += pnl
            trades.append({"pnl": pnl})
        curve.append(equity)
    return stats(55.0, equity, trades, curve)


def williams(days, di):
    _, bars = days[di - 1]
    return max(float(x["high"]) for x in bars) - min(float(x["low"]) for x in bars)


def dual_thrust(n):
    def f(days, di):
        prev = [bars for _, bars in days[di - n:di]]
        hh = max(max(float(x["high"]) for x in b) for b in prev)
        ll = min(min(float(x["low"]) for x in b) for b in prev)
        hc = max(float(b[-1]["close"]) for b in prev)
        lc = min(float(b[-1]["close"]) for b in prev)
        return max(hh - lc, hc - ll)
    return f


def main():
    days = daily(load_candles("ETHUSD", "1h", 730))
    print(f"ETH 1h, {len(days)} full days. Bot reference (4h trend breakout): in +46.6% | OUT +9.7%\n")
    for label, fn, ks in (("Larry Williams", williams, (0.4, 0.6, 0.8)),
                          ("Dual Thrust N=4", dual_thrust(4), (0.3, 0.5, 0.7))):
        for k in ks:
            i = simulate(days, fn, k, 0, 0.6)
            o = simulate(days, fn, k, 0.6, 1.0)
            print(f"{label:<16} k={k}: in {i['return_pct']:+6.1f}% pf {i['profit_factor']} dd {i['max_dd_pct']}% n={i['trades']}"
                  f"  | OUT {o['return_pct']:+6.1f}% pf {o['profit_factor']} dd {o['max_dd_pct']}% n={o['trades']}")


if __name__ == "__main__":
    main()
