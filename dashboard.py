"""Local web dashboard for the bot. Read-only: it never places or cancels orders.

  python dashboard.py            then open http://127.0.0.1:8765
"""
import csv
import json
import os
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from backtest import load_candles, run
from delta_client import DeltaClient, DeltaError
from profiles import PROFILES, fetch_candles
from risk import RiskConfig, TAKER_FEE
from strategy import ema

HERE = os.path.dirname(os.path.abspath(__file__))
SYMBOL, CV = "ETHUSD", 0.01
# dashboard tab -> (bot mode, strategy profile, state file prefix)
VIEWS = {
    "paper": ("paper", "donchian4h", "state_paper"),
    "paper_ema90": ("paper", "ema90", "state_paper"),
    "testnet": ("testnet", "donchian4h", "state_testnet"),
    "live": ("live", "donchian4h", "state"),
}
PORT = 8765

_cache = {}


def cached(key, ttl, fn):
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    val = fn()
    _cache[key] = (time.time(), val)
    return val


def read_csv(name, mode):
    path = os.path.join(HERE, name)
    if not os.path.exists(path):
        return []
    with open(path, newline="") as f:
        return [r for r in csv.DictReader(f) if r.get("mode") == mode]


def iso_to_ts(s):
    return int(datetime.fromisoformat(s).timestamp())


def params(prof):
    return prof.params_cls(**json.load(open(os.path.join(HERE, prof.params_file))))


def chart_data(candles, prof, p):
    """Candles plus the indicator lines this strategy trades on (drawn by the dashboard)."""
    closes = [float(c["close"]) for c in candles]

    def line(name, color, values, skip, dash=False):
        return {"name": name, "color": color, "dash": dash,
                "data": [{"time": c["time"], "value": v} for c, v in zip(candles, values)][skip:]}

    lines = [line("EMA 200 (trend filter)", "#f0b90b", ema(closes, p.trend_len), p.trend_len)]
    if prof.name == "ema90":
        lines += [line(f"EMA {p.fast}", "#22d3ee", ema(closes, p.fast), p.fast),
                  line(f"EMA {p.slow}", "#a78bfa", ema(closes, p.slow), p.slow)]
    else:
        n = p.entry_len
        hi = [None] * n + [max(float(c["high"]) for c in candles[i - n:i]) for i in range(n, len(candles))]
        lo = [None] * n + [min(float(c["low"]) for c in candles[i - n:i]) for i in range(n, len(candles))]
        lines += [line("Breakout high", "rgba(59,130,246,.7)", hi, n, True),
                  line("Breakout low", "rgba(59,130,246,.7)", lo, n, True)]
    return {
        "candles": [{"time": c["time"], "open": float(c["open"]), "high": float(c["high"]),
                     "low": float(c["low"]), "close": float(c["close"])} for c in candles],
        "lines": lines,
    }


def summarize(trades, start_eq):
    """trades: list of dict(pnl, fee). Exchange-style P&L summary."""
    wins = [t["pnl"] for t in trades if t["pnl"] > 0]
    losses = [t["pnl"] for t in trades if t["pnl"] <= 0]
    eq, peak, mdd = start_eq, start_eq, 0.0
    for t in trades:
        eq += t["pnl"]
        peak = max(peak, eq)
        mdd = max(mdd, (peak - eq) / peak if peak else 0)
    total = sum(wins) + sum(losses)
    return {
        "trades": len(trades), "wins": len(wins), "losses": len(losses),
        "win_rate": 100 * len(wins) / len(trades) if trades else 0,
        "gross_profit": sum(wins), "gross_loss": sum(losses), "net_pnl": total,
        "return_pct": 100 * total / start_eq if start_eq else 0,
        "avg_win": sum(wins) / len(wins) if wins else 0, "avg_loss": sum(losses) / len(losses) if losses else 0,
        "best": max(wins) if wins else 0, "worst": min(losses) if losses else 0,
        "profit_factor": (sum(wins) / -sum(losses)) if losses and sum(losses) else None,
        "fees": sum(t.get("fee", 0) for t in trades), "max_dd_pct": 100 * mdd,
    }


def waiting_for(prof, p, closed):
    """Plain-language description of what would make a flat bot enter, from closed candles."""
    if len(closed) < p.trend_len + 10:
        return "Collecting candle history."
    ind = prof.indicators(closed, p)
    i = len(ind.c) - 1
    up = ind.c[i] > ind.ema[i]
    tf = "4h" if prof.bar_sec == 14400 else f"{prof.bar_sec // 60}m"
    if prof.name == "ema90":
        if up:
            return f"Price is above the EMA 200. A long opens when EMA {p.fast} crosses above EMA {p.slow}."
        return f"Price is below the EMA 200. A short opens when EMA {p.fast} crosses below EMA {p.slow}."
    n = p.entry_len
    hi, lo = max(ind.h[i - n + 1:i + 1]), min(ind.l[i - n + 1:i + 1])
    rising = ind.ema[i] > ind.ema[i - 5]
    weak = ind.adx[i] < p.adx_min
    if rising and up:
        s = f"A long opens on a {tf} close above {hi:,.2f}."
    elif not rising and not up:
        s = f"A short opens on a {tf} close below {lo:,.2f}."
    else:
        s = "Trend is turning, so no breakout counts until price and the EMA 200 agree."
    return s + (" Trend strength is low right now." if weak else "")


def live_payload(view):
    mode, pname, state_prefix = VIEWS[view]
    prof = PROFILES[pname]
    p = params(prof)
    step = prof.bar_sec
    sp = os.path.join(HERE, f"{state_prefix}{prof.suffix}.json")
    state = json.load(open(sp)) if os.path.exists(sp) else {}
    cli = DeltaClient()
    tick = cached("ticker", 3, lambda: cli.ticker(SYMBOL))
    mark, last = float(tick["mark_price"]), float(tick.get("close") or tick["mark_price"])

    def fetch():
        return fetch_candles(cli, SYMBOL, prof, 450, int(time.time()))
    # full history changes slowly; the forming candle is patched with the live last price below
    candles = [dict(c) for c in cached("candles" + prof.name, 30, fetch)]
    cur_bar = int(time.time()) // step * step
    if candles and candles[-1]["time"] == cur_bar:
        c = candles[-1]
        c["close"] = last
        c["high"] = max(float(c["high"]), last)
        c["low"] = min(float(c["low"]), last)
    elif candles and candles[-1]["time"] < cur_bar:
        candles.append({"time": cur_bar, "open": last, "high": last, "low": last, "close": last})

    rows = read_csv(f"trades{prof.suffix}.csv", mode)
    trades = []
    for r in rows:
        entry, exit_, qty = float(r["entry"]), float(r["exit"]), int(r["qty"])
        trades.append({
            "entry_time": iso_to_ts(r["entry_time"]) if r.get("entry_time") else None,
            "exit_time": iso_to_ts(r["time"]), "side": int(r["side"]), "qty": qty,
            "entry": entry, "exit": exit_, "pnl": float(r["pnl_usd_est"]), "reason": r.get("reason", ""),
            "fee": (entry + exit_) * qty * CV * TAKER_FEE,
        })
    realized = sum(t["pnl"] for t in trades)
    if mode == "paper":
        equity = float(state.get("paper_equity") or 55.0)
        start_eq = equity - realized
    else:
        equity = state.get("equity")
        start_eq = (equity - realized) if equity is not None else None

    pos = state.get("pos")
    if pos:
        upnl = pos["side"] * (mark - pos["entry"]) * pos["qty"] * CV
        notional = pos["qty"] * CV * mark
        pos = dict(pos, mark=mark, upnl=upnl, notional=notional,
                   upnl_pct=100 * upnl / (pos["entry"] * pos["qty"] * CV),
                   risk_to_stop=pos["side"] * (pos["stop"] - mark) * pos["qty"] * CV,
                   eff_leverage=notional / equity if equity else None)

    curve = []
    if start_eq is not None:
        eq = start_eq
        curve.append({"time": iso_to_ts(rows[0]["entry_time"]) if rows and rows[0].get("entry_time") else
                      candles[0]["time"], "value": eq})
        for t in trades:
            eq += t["pnl"]
            curve.append({"time": t["exit_time"], "value": eq})

    hb = state.get("heartbeat")
    last_bar = state.get("last_bar") or 0
    return {
        "mode": view, "symbol": SYMBOL, "strategy": prof.label, "bar_sec": step, "params": p.__dict__,
        "mark": mark, "last": last, "updated": int(time.time()),
        "equity": equity, "start_equity": start_eq, "position": pos,
        "trades": trades[::-1], "signals": read_csv(f"signals{prof.suffix}.csv", mode)[::-1][:200],
        "stats": summarize(trades, start_eq) if start_eq else None, "curve": curve,
        # loop bot beats every 30s; scheduled cloud runs every ~15 min (GitHub cron can lag, so allow 3x + slack)
        "bot_alive": bool(hb and time.time() - hb < 3 * state.get("heartbeat_every", 30) + 90), "heartbeat": hb,
        "heartbeat_every": state.get("heartbeat_every", 30),
        "halted": (state.get("guard") or {}).get("halted", False),
        "stop_file": os.path.exists(os.path.join(HERE, "STOP")),
        "next_decision": (int(time.time()) // step + 1) * step,
        "chart": chart_data(candles, prof, p),
        "waiting_for": waiting_for(prof, p, [c for c in candles if c["time"] < cur_bar]),
    }


def backtest_payload():
    def compute():
        prof = PROFILES["donchian4h"]
        p = params(prof)
        candles = load_candles(SYMBOL, "4h", 1460)
        start_eq = 55.0
        eq, tr, _ = run(candles, SYMBOL, p, RiskConfig(), start_eq)
        trades = [{"entry_time": t["t_in"], "exit_time": t["t"], "side": t["side"], "qty": t["qty"],
                   "entry": t["entry"], "exit": t["exit"], "pnl": t["pnl"], "reason": t.get("reason", "stop"),
                   "fee": (t["entry"] + t["exit"]) * t["qty"] * CV * TAKER_FEE} for t in tr]
        curve, e = [{"time": candles[0]["time"], "value": start_eq}], start_eq
        for t in trades:
            e += t["pnl"]
            curve.append({"time": t["exit_time"], "value": e})
        return {
            "mode": "backtest", "symbol": SYMBOL, "strategy": prof.label, "bar_sec": prof.bar_sec,
            "params": p.__dict__,
            "equity": eq, "start_equity": start_eq, "mark": float(candles[-1]["close"]), "position": None,
            "trades": trades[::-1], "signals": [], "stats": summarize(trades, start_eq), "curve": curve,
            "chart": chart_data(candles, prof, p),
            "buy_hold_pct": 100 * (float(candles[-1]["close"]) / float(candles[0]["open"]) - 1),
        }
    return cached("backtest", 3600, compute)


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        u = urlparse(self.path)
        try:
            if u.path in ("/", "/index.html"):
                self._send(200, open(os.path.join(HERE, "dashboard.html"), "rb").read(), "text/html; charset=utf-8")
            elif u.path == "/api/data":
                mode = parse_qs(u.query).get("mode", ["paper"])[0]
                if mode == "backtest":
                    payload = backtest_payload()
                elif mode in VIEWS:
                    payload = live_payload(mode)
                else:
                    return self._send(400, '{"error":"bad mode"}', "application/json")
                self._send(200, json.dumps(payload), "application/json")
            else:
                self._send(404, "not found", "text/plain")
        except (DeltaError, OSError, ValueError) as e:
            self._send(502, json.dumps({"error": str(e)}), "application/json")

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    print(f"Dashboard: http://127.0.0.1:{PORT}  (Ctrl-C to stop)")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
