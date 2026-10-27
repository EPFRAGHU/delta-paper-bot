"""Multi-timeframe, multi-strategy backtest on ETHUSD (Delta India prices, Binance order flow).

Every timeframe is built from the same 1m data. Every strategy uses identical execution:
signal on bar close -> fill next bar open, taker fee + 18% GST + slippage per side, ATR stop checked
intrabar, 1.5% risk per trade, notional capped at 10x equity, $55 start. No parameter tuning per
timeframe (standard textbook settings), so results compare timeframes fairly instead of curve-fitting.
First 60% of the period = "early", last 40% = "late" (each starts again from $55).

  python research/multi_tf.py            -> research/results_multi_tf.csv + printed summary
"""
import math
import os
import sys
from multiprocessing import Pool

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from risk import RiskConfig, size_contracts, TAKER_FEE, SLIPPAGE  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "..", "data")
CV = 0.01
START_EQ = 55.0
TIMEFRAMES = [1, 2, 3, 5, 8, 10, 13, 15, 18, 20, 30, 45, 60, 90, 120, 180, 240]


# ---------------------------------------------------------------- data
def load_1m():
    d = np.load(os.path.join(DATA, "delta_ETHUSD_1m.npz"))
    df = pd.DataFrame({k: d[k] for k in ("o", "h", "l", "c", "v")}, index=d["t"])
    b = np.load(os.path.join(DATA, "binance_ETHUSDT_1m.npz"))
    flow = pd.DataFrame({"bvol": b["vol"], "buy": b["buy"]}, index=b["t"])
    df = df.join(flow, how="left")
    df[["bvol", "buy"]] = df[["bvol", "buy"]].fillna(0.0)
    return df


def resample(df, tf):
    g = (df.index // (tf * 60)) * (tf * 60)
    out = df.groupby(g).agg(o=("o", "first"), h=("h", "max"), l=("l", "min"), c=("c", "last"),
                            v=("v", "sum"), bvol=("bvol", "sum"), buy=("buy", "sum"))
    return out


# ---------------------------------------------------------------- indicators
def ema(x, n):
    return pd.Series(x).ewm(span=n, adjust=False).mean().to_numpy()


def wilder(x, n):
    return pd.Series(x).ewm(alpha=1 / n, adjust=False).mean().to_numpy()


def atr(h, l, c, n=14):
    pc = np.r_[c[0], c[:-1]]
    tr = np.maximum.reduce([h - l, np.abs(h - pc), np.abs(l - pc)])
    return wilder(tr, n)


def adx(h, l, c, n=14):
    up, dn = np.r_[0, np.diff(h)], np.r_[0, -np.diff(l)]
    pdm = np.where((up > dn) & (up > 0), up, 0.0)
    ndm = np.where((dn > up) & (dn > 0), dn, 0.0)
    a = atr(h, l, c, n)
    pdi = 100 * wilder(pdm, n) / np.where(a == 0, np.nan, a)
    ndi = 100 * wilder(ndm, n) / np.where(a == 0, np.nan, a)
    dx = 100 * np.abs(pdi - ndi) / np.where(pdi + ndi == 0, np.nan, pdi + ndi)
    return wilder(np.nan_to_num(dx), n)


def rsi(c, n=14):
    d = np.r_[0, np.diff(c)]
    g, ls = wilder(np.maximum(d, 0), n), wilder(np.maximum(-d, 0), n)
    return 100 - 100 / (1 + g / np.where(ls == 0, 1e-12, ls))


def supertrend_dir(h, l, c, n=10, mult=3.0):
    a = atr(h, l, c, n)
    mid = (h + l) / 2
    ub, lb = mid + mult * a, mid - mult * a
    fu, fl = ub.copy(), lb.copy()
    d = np.ones(len(c))
    for i in range(1, len(c)):
        fu[i] = ub[i] if (ub[i] < fu[i - 1] or c[i - 1] > fu[i - 1]) else fu[i - 1]
        fl[i] = lb[i] if (lb[i] > fl[i - 1] or c[i - 1] < fl[i - 1]) else fl[i - 1]
        if c[i] > fu[i - 1]:
            d[i] = 1
        elif c[i] < fl[i - 1]:
            d[i] = -1
        else:
            d[i] = d[i - 1]
    return d


def daily_vwap(t, h, l, c, v):
    day = t // 86400
    tp = (h + l + c) / 3
    s = pd.DataFrame({"d": day, "pv": tp * v, "v": v})
    cpv = s.groupby("d")["pv"].cumsum().to_numpy()
    cv = s.groupby("d")["v"].cumsum().to_numpy()
    return np.where(cv > 0, cpv / np.where(cv == 0, 1, cv), c)


def cross_up(a, b):
    return (a > b) & (np.r_[False, a[:-1] <= b[:-1]])


def cross_dn(a, b):
    return (a < b) & (np.r_[False, a[:-1] >= b[:-1]])


# ---------------------------------------------------------------- strategies
# each returns: long_entry, short_entry, long_exit, short_exit (bool arrays), stop_atr, trail_atr (or None)
def s_donchian(b):
    h, l, c = b["h"], b["l"], b["c"]
    e = ema(c, 200)
    hh = pd.Series(h).rolling(40).max().shift(1).to_numpy()
    ll = pd.Series(l).rolling(40).min().shift(1).to_numpy()
    xl = pd.Series(l).rolling(20).min().shift(1).to_numpy()
    xh = pd.Series(h).rolling(20).max().shift(1).to_numpy()
    slope = e - np.r_[np.full(5, np.nan), e[:-5]]
    ax = adx(h, l, c)
    le = (c > hh) & (c > e) & (slope > 0) & (ax >= 20)
    se = (c < ll) & (c < e) & (slope < 0) & (ax >= 20)
    return le, se, c < xl, c > xh, 2.5, 3.0


def s_supertrend(b):
    d = supertrend_dir(b["h"], b["l"], b["c"])
    flip_up = (d == 1) & (np.r_[1, d[:-1]] == -1)
    flip_dn = (d == -1) & (np.r_[-1, d[:-1]] == 1)
    return flip_up, flip_dn, flip_dn, flip_up, 3.0, None


def s_ema_cross(b):
    c = b["c"]
    f, s, t = ema(c, 9), ema(c, 21), ema(c, 200)
    up, dn = cross_up(f, s), cross_dn(f, s)
    return up & (c > t), dn & (c < t), dn, up, 2.0, None


def s_rsi_mr(b):
    r = rsi(b["c"])
    return cross_dn(r, np.full_like(r, 30)), cross_up(r, np.full_like(r, 70)), r > 50, r < 50, 2.0, None


def s_bollinger_mr(b):
    c = b["c"]
    m = pd.Series(c).rolling(20).mean().to_numpy()
    sd = pd.Series(c).rolling(20).std().to_numpy()
    return c < m - 2 * sd, c > m + 2 * sd, c >= m, c <= m, 2.0, None


def s_vwap_trend(b):
    c = b["c"]
    vw = daily_vwap(b["t"], b["h"], b["l"], c, b["v"])
    t = ema(c, 200)
    return cross_up(c, vw) & (c > t), cross_dn(c, vw) & (c < t), cross_dn(c, vw), cross_up(c, vw), 2.0, None


def s_orderflow(b):
    # bar delta = taker buys - taker sells (Binance); z-score of its 20-bar sum = buying/selling pressure
    c = b["c"]
    delta = 2 * b["buy"] - b["bvol"]
    cvd = pd.Series(delta).rolling(20).sum()
    z = ((cvd - cvd.rolling(200).mean()) / cvd.rolling(200).std()).to_numpy()
    vw = daily_vwap(b["t"], b["h"], b["l"], c, b["v"])
    e = ema(c, 50)
    le = (z > 1.5) & (np.r_[0, z[:-1]] <= 1.5) & (c > vw) & (c > e)
    se = (z < -1.5) & (np.r_[0, z[:-1]] >= -1.5) & (c < vw) & (c < e)
    return le, se, z < 0, z > 0, 2.0, 3.0


STRATEGIES = {
    "donchian_breakout (bot)": s_donchian,
    "supertrend 10,3": s_supertrend,
    "ema 9/21 + ema200": s_ema_cross,
    "rsi 30/70 reversion": s_rsi_mr,
    "bollinger 20,2 reversion": s_bollinger_mr,
    "vwap cross + ema200": s_vwap_trend,
    "order-flow delta + vwap": s_orderflow,
}


# ---------------------------------------------------------------- simulator
def simulate(b, sig, a0, a1):
    le, se, lx, sx, stop_k, trail_k = sig
    o, h, l, c, at = (b[k].tolist() for k in ("o", "h", "l", "c", "atr"))
    le, se, lx, sx = (np.nan_to_num(x).astype(bool).tolist() for x in (le, se, lx, sx))
    rc, fee = RiskConfig(), TAKER_FEE + SLIPPAGE
    eq, peak, mdd, n, wins, gw, gl, fees = START_EQ, START_EQ, 0.0, 0, 0, 0.0, 0.0, 0.0
    pos = None  # [side, qty, entry, stop, best]
    warm = max(a0, 210)

    def close(px):
        nonlocal eq, n, wins, gw, gl, fees, pos
        side, qty, entry = pos[0], pos[1], pos[2]
        f = (px + entry) * qty * CV * fee
        pnl = side * (px - entry) * qty * CV - f
        eq += pnl
        fees += f
        n += 1
        if pnl > 0:
            wins += 1
            gw += pnl
        else:
            gl -= pnl
        pos = None

    i = warm
    while i < a1 - 1:
        if pos:
            side = pos[0]
            if (side > 0 and l[i] <= pos[3]) or (side < 0 and h[i] >= pos[3]):
                px = pos[3]
                if (side > 0 and o[i] < px) or (side < 0 and o[i] > px):
                    px = o[i]
                close(px)
            elif (side > 0 and lx[i]) or (side < 0 and sx[i]):
                close(o[i + 1])
            elif trail_k:
                pos[4] = max(pos[4], h[i]) if side > 0 else min(pos[4], l[i])
                ns = pos[4] - side * trail_k * at[i]
                pos[3] = max(pos[3], ns) if side > 0 else min(pos[3], ns)
        if eq < 5:  # account effectively blown
            break
        peak = max(peak, eq)
        mdd = max(mdd, (peak - eq) / peak)
        if pos is None and at[i] > 0:
            s = 1 if le[i] else -1 if se[i] else 0
            if s:
                entry = o[i + 1]
                stop = entry - s * stop_k * at[i]
                qty = size_contracts(eq, entry, stop, CV, rc)
                if qty:
                    pos = [s, qty, entry, stop, entry]
        i += 1
    if pos:
        close(c[a1 - 1])
    return {"ret": 100 * (eq / START_EQ - 1), "pf": (gw / gl) if gl else float("inf"), "dd": 100 * mdd,
            "trades": n, "win": 100 * wins / n if n else 0.0, "fees": fees}


def run_tf(tf):
    df = resample(load_1m(), tf)
    b = {"t": df.index.to_numpy(), **{k: df[k].to_numpy() for k in df.columns}}
    b["atr"] = atr(b["h"], b["l"], b["c"])
    cut = int(len(df) * 0.6)
    rows = []
    for name, fn in STRATEGIES.items():
        sig = fn(b)
        e, l_ = simulate(b, sig, 0, cut), simulate(b, sig, cut, len(df))
        rows.append({"tf_min": tf, "strategy": name, "bars": len(df),
                     "early_ret": e["ret"], "early_pf": e["pf"], "early_dd": e["dd"], "early_trades": e["trades"],
                     "late_ret": l_["ret"], "late_pf": l_["pf"], "late_dd": l_["dd"], "late_trades": l_["trades"],
                     "late_win": l_["win"], "late_fees": l_["fees"]})
    print(f"done {tf}m", flush=True)
    return rows


if __name__ == "__main__":
    tfs = [int(x) for x in sys.argv[1:]] or TIMEFRAMES
    with Pool(min(6, len(tfs))) as pool:
        rows = [r for rs in pool.map(run_tf, tfs) for r in rs]
    res = pd.DataFrame(rows)
    res.to_csv(os.path.join(HERE, "results_multi_tf.csv"), index=False)
    pd.set_option("display.width", 200)
    pd.set_option("display.max_rows", 200)
    show = res[["tf_min", "strategy", "early_ret", "early_dd", "late_ret", "late_pf", "late_dd", "late_trades"]].round(1)
    print(show.sort_values(["strategy", "tf_min"]).to_string(index=False))
