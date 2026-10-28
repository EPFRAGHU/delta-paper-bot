"""Strategy profiles: which candles, indicators, params and files each paper bot uses."""
from dataclasses import dataclass

from strategy import Params, Indicators, EmaParams, EmaCross
from delta_client import _RES_SECONDS


@dataclass(frozen=True)
class Profile:
    name: str          # --strategy value
    label: str         # shown on the dashboard
    bar_sec: int       # strategy candle length
    fetch_res: str     # Delta resolution used to build those candles
    params_file: str
    params_cls: type
    indicators: type
    suffix: str        # appended to state / trades / signals file names ("" keeps the original 4h files)


PROFILES = {
    "donchian4h": Profile("donchian4h", "Donchian breakout · 4h", 14400, "4h", "params.json",
                          Params, Indicators, ""),
    "ema90": Profile("ema90", "EMA 9/21 + EMA200 · 90m", 5400, "30m", "params_ema90.json",
                     EmaParams, EmaCross, "_ema90"),
}


def fetch_candles(client, symbol, prof: Profile, bars, now):
    """Candles of prof.bar_sec length, oldest first, including the still-forming one.
    Delta has no 90m resolution, so smaller candles are merged (bars align to UTC midnight)."""
    step = prof.bar_sec
    end = now // step * step + step
    raw = client.candles(symbol, prof.fetch_res, end - bars * step, end)
    if _RES_SECONDS[prof.fetch_res] == step:
        return raw
    out = {}
    for c in raw:
        t = c["time"] // step * step
        b = out.get(t)
        if b is None:
            out[t] = {"time": t, "open": float(c["open"]), "high": float(c["high"]), "low": float(c["low"]),
                      "close": float(c["close"]), "volume": float(c.get("volume") or 0)}
        else:
            b["high"] = max(b["high"], float(c["high"]))
            b["low"] = min(b["low"], float(c["low"]))
            b["close"] = float(c["close"])
            b["volume"] += float(c.get("volume") or 0)
    return [out[k] for k in sorted(out)]
