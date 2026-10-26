"""Trend-following breakout strategy with regime filter and ATR risk.

Rules (evaluated on CLOSED candles only):
  Long  entry: close > highest high of last `entry_len` bars AND close > EMA(trend_len)
               AND EMA(trend_len) rising AND ADX >= adx_min
  Short entry: mirror image.
  Initial stop: entry -/+ atr_stop * ATR.
  Exit: trailing stop = max(initial stop, lowest low of last `exit_len` bars) for longs
        (mirror for shorts), plus a chandelier trail at atr_trail * ATR from the best price.
"""
from dataclasses import dataclass


@dataclass
class Params:
    entry_len: int = 20
    exit_len: int = 10
    trend_len: int = 200
    atr_len: int = 14
    atr_stop: float = 2.0
    atr_trail: float = 3.0
    adx_len: int = 14
    adx_min: float = 20.0


def ema(values, n):
    out, k, cur = [], 2 / (n + 1), None
    for v in values:
        cur = v if cur is None else cur + k * (v - cur)
        out.append(cur)
    return out


def atr(highs, lows, closes, n):
    trs = []
    for i in range(len(closes)):
        if i == 0:
            trs.append(highs[i] - lows[i])
        else:
            pc = closes[i - 1]
            trs.append(max(highs[i] - lows[i], abs(highs[i] - pc), abs(lows[i] - pc)))
    out, cur = [], None
    for i, tr in enumerate(trs):  # Wilder smoothing
        cur = tr if cur is None else (cur * (n - 1) + tr) / n
        out.append(cur)
    return out


def adx(highs, lows, closes, n):
    pdm, ndm, trs = [0.0], [0.0], [highs[0] - lows[0]]
    for i in range(1, len(closes)):
        up, dn = highs[i] - highs[i - 1], lows[i - 1] - lows[i]
        pdm.append(up if up > dn and up > 0 else 0.0)
        ndm.append(dn if dn > up and dn > 0 else 0.0)
        pc = closes[i - 1]
        trs.append(max(highs[i] - lows[i], abs(highs[i] - pc), abs(lows[i] - pc)))

    def wilder(xs):
        out, cur = [], None
        for x in xs:
            cur = x if cur is None else (cur * (n - 1) + x) / n
            out.append(cur)
        return out

    s_tr, s_p, s_n = wilder(trs), wilder(pdm), wilder(ndm)
    dx = []
    for t, p, m in zip(s_tr, s_p, s_n):
        if t == 0:
            dx.append(0.0)
            continue
        pdi, ndi = 100 * p / t, 100 * m / t
        dx.append(0.0 if pdi + ndi == 0 else 100 * abs(pdi - ndi) / (pdi + ndi))
    return wilder(dx)


class Indicators:
    def __init__(self, candles, p: Params):
        self.h = [float(c["high"]) for c in candles]
        self.l = [float(c["low"]) for c in candles]
        self.c = [float(c["close"]) for c in candles]
        self.ema = ema(self.c, p.trend_len)
        self.atr = atr(self.h, self.l, self.c, p.atr_len)
        self.adx = adx(self.h, self.l, self.c, p.adx_len)
        self.p = p

    def warm(self, i):
        return i >= max(self.p.trend_len, self.p.entry_len, self.p.adx_len * 3) + 5

    def entry_signal(self, i):
        """+1 long, -1 short, 0 none, using bar i as the last closed bar."""
        p = self.p
        if not self.warm(i):
            return 0
        hh = max(self.h[i - p.entry_len:i])
        ll = min(self.l[i - p.entry_len:i])
        slope = self.ema[i] - self.ema[i - 5]
        if self.adx[i] < p.adx_min:
            return 0
        if self.c[i] > hh and self.c[i] > self.ema[i] and slope > 0:
            return 1
        if self.c[i] < ll and self.c[i] < self.ema[i] and slope < 0:
            return -1
        return 0

    def trail_stop(self, i, side, cur_stop, best_price):
        p = self.p
        if side > 0:
            s = max(cur_stop, min(self.l[i - p.exit_len + 1:i + 1]), best_price - p.atr_trail * self.atr[i])
        else:
            s = min(cur_stop, max(self.h[i - p.exit_len + 1:i + 1]), best_price + p.atr_trail * self.atr[i])
        return s
