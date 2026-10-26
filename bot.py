"""Delta Exchange India trading bot: ETHUSD perpetual, 4h trend breakout.

Modes:
  python bot.py                     paper trading on live market data (default, no keys needed)
  python bot.py --mode testnet      real orders on Delta India testnet (testnet keys)
  python bot.py --mode live         real money. Requires LIVE_CONFIRM=YES in .env too.

Create a file named STOP in this folder to block new entries (open position stays protected by its stop).
"""
import argparse
import csv
import json
import logging
import math
import os
import sys
import time
from datetime import datetime, timezone

from delta_client import DeltaClient, DeltaError, PROD_URL, TESTNET_URL, _RES_SECONDS
from risk import RiskConfig, Guard, size_contracts, TAKER_FEE, SLIPPAGE
from strategy import Params, Indicators

HERE = os.path.dirname(os.path.abspath(__file__))
SYMBOL = "ETHUSD"
RESOLUTION = "4h"
LEVERAGE = 10           # exchange leverage setting; never above 10 per user limit
HISTORY_BARS = 400

log = logging.getLogger("bot")


def load_env(path=os.path.join(HERE, ".env")):
    if not os.path.exists(path):
        return
    for line in open(path):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


def round_tick(price, tick, side_up):
    n = price / tick
    return round((math.ceil(n) if side_up else math.floor(n)) * tick, 8)


class State:
    path = os.path.join(HERE, "state.json")

    def __init__(self):
        self.d = {"pos": None, "last_bar": 0, "paper_equity": None, "guard": None}
        if os.path.exists(self.path):
            self.d.update(json.load(open(self.path)))

    def save(self):
        tmp = self.path + ".tmp"
        json.dump(self.d, open(tmp, "w"), indent=2)
        os.replace(tmp, self.path)


def record_trade(row, name="trades.csv"):
    path = os.path.join(HERE, name)
    new = not os.path.exists(path)
    with open(path, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(row))
        if new:
            w.writeheader()
        w.writerow(row)


class Bot:
    def __init__(self, mode, paper_equity):
        self.mode = mode
        base = TESTNET_URL if mode == "testnet" else PROD_URL
        key = os.environ.get("DELTA_API_KEY", "")
        secret = os.environ.get("DELTA_API_SECRET", "")
        self.cli = DeltaClient(key, secret, base)
        self.data = DeltaClient(base_url=base)  # public candles from the same venue
        self.params = Params(**json.load(open(os.path.join(HERE, "params.json"))))
        self.rc = RiskConfig()
        self.state = State()
        prod = self.data.product(SYMBOL)
        self.pid, self.cv, self.tick_size = prod["id"], float(prod["contract_value"]), float(prod["tick_size"])
        if mode == "paper" and self.state.d["paper_equity"] is None:
            self.state.d["paper_equity"] = paper_equity
        eq = self.equity()
        g = self.state.d.get("guard") or {}
        self.guard = Guard(self.rc, g.get("peak", eq))
        self.guard.halted = g.get("halted", False)
        log.info("start mode=%s %s pid=%s cv=%s equity=$%.2f params=%s", mode, SYMBOL, self.pid, self.cv, eq, self.params)
        if mode != "paper":
            self.cli.set_leverage(self.pid, LEVERAGE)
            self.reconcile()

    # --- account ---------------------------------------------------------
    def equity(self):
        if self.mode == "paper":
            return float(self.state.d["paper_equity"])
        bal = self.cli.balances()
        net = (bal.get("meta") or {}).get("net_equity")
        if net:
            return float(net)
        for a in bal["result"]:
            if a["asset_symbol"] in ("USD", "USDT"):
                return float(a["balance"])
        raise DeltaError("no USD balance found")

    def exchange_position(self):
        p = self.cli.position(self.pid)
        size = int(p.get("size") or 0)
        return size, float(p.get("entry_price") or 0)

    def reconcile(self):
        """Make local state agree with the exchange after a restart."""
        size, entry = self.exchange_position()
        pos = self.state.d["pos"]
        if size == 0 and pos:
            log.warning("exchange flat but state had a position; stop likely filled while bot was off")
            self.close_local(pos, float(self.data.ticker(SYMBOL)["mark_price"]), "reconcile")
        elif size != 0 and not pos:
            log.warning("adopting untracked exchange position size=%s entry=%s", size, entry)
            ind = self.indicators()
            side = 1 if size > 0 else -1
            stop = entry - side * self.params.atr_stop * ind.atr[-1]
            self.state.d["pos"] = {"side": side, "qty": abs(size), "entry": entry, "stop": stop,
                                   "best": entry, "stop_id": None, "opened": int(time.time())}
            self.ensure_stop()
        elif pos:
            self.ensure_stop()
        self.state.save()

    # --- market data -----------------------------------------------------
    def closed_candles(self):
        step = _RES_SECONDS[RESOLUTION]
        now = int(time.time())
        cur_bar = now // step * step
        c = self.data.candles(SYMBOL, RESOLUTION, cur_bar - HISTORY_BARS * step, cur_bar)
        return [x for x in c if x["time"] < cur_bar]  # drop the still-forming bar

    def indicators(self):
        return Indicators(self.closed_candles(), self.params)

    # --- orders ----------------------------------------------------------
    def ensure_stop(self):
        pos = self.state.d["pos"]
        if self.mode == "paper" or not pos:
            return
        orders = self.cli.open_orders(self.pid)
        stops = [o for o in orders if o.get("stop_order_type") == "stop_loss_order"]
        if not any(str(o["id"]) == str(pos.get("stop_id")) for o in stops):
            self.move_stop(pos["stop"], force=True)

    def move_stop(self, new_stop, force=False):
        pos = self.state.d["pos"]
        side_up = pos["side"] < 0  # short stop is above price: round up
        new_stop = round_tick(new_stop, self.tick_size, side_up)
        if not force and new_stop == pos.get("placed_stop"):
            return
        if self.mode != "paper":
            exit_side = "sell" if pos["side"] > 0 else "buy"
            old_id = pos.get("stop_id")
            o = self.cli.place_stop(self.pid, exit_side, pos["qty"], new_stop)  # new stop first, then cancel old
            pos["stop_id"] = o["id"]
            if old_id:
                try:
                    self.cli.cancel_order(self.pid, old_id)
                except DeltaError as e:
                    log.warning("cancel old stop %s failed: %s", old_id, e)
        pos["stop"] = pos["placed_stop"] = new_stop
        log.info("stop set at %.2f", new_stop)

    def open_position(self, side, ind):
        price = float(self.data.ticker(SYMBOL)["mark_price"])
        stop = price - side * self.params.atr_stop * ind.atr[-1]
        eq = self.equity()
        qty = size_contracts(eq, price, stop, self.cv, self.rc)
        if qty == 0:
            log.info("signal %+d skipped: size 0 (equity $%.2f too small for stop distance)", side, eq)
            return
        log.info("ENTER %s qty=%d @~%.2f stop=%.2f equity=$%.2f notional=$%.2f (%.1fx)",
                 "LONG" if side > 0 else "SHORT", qty, price, stop, eq, qty * self.cv * price,
                 qty * self.cv * price / eq)
        fill = price
        if self.mode != "paper":
            o = self.cli.place_order(self.pid, "buy" if side > 0 else "sell", qty, "market_order",
                                     client_order_id=f"bot{int(time.time())}")
            fill = float(o.get("average_fill_price") or price)
            stop = fill - side * self.params.atr_stop * ind.atr[-1]
        else:
            fill = price * (1 + side * SLIPPAGE)
        self.state.d["pos"] = {"side": side, "qty": qty, "entry": fill, "stop": stop, "best": fill,
                               "stop_id": None, "opened": int(time.time())}
        self.move_stop(stop, force=True)
        self.state.save()

    def close_local(self, pos, px, reason):
        pnl = pos["side"] * (px - pos["entry"]) * pos["qty"] * self.cv - (px + pos["entry"]) * pos["qty"] * self.cv * TAKER_FEE
        if self.mode == "paper":
            self.state.d["paper_equity"] = float(self.state.d["paper_equity"]) + pnl
        log.info("EXIT %s qty=%d entry=%.2f exit=%.2f pnl=$%.2f (%s)", "LONG" if pos["side"] > 0 else "SHORT",
                 pos["qty"], pos["entry"], px, pnl, reason)
        record_trade({"time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                      "entry_time": datetime.fromtimestamp(pos["opened"], timezone.utc).isoformat(timespec="seconds"),
                      "mode": self.mode, "side": pos["side"], "qty": pos["qty"], "entry": round(pos["entry"], 2),
                      "exit": round(px, 2), "pnl_usd_est": round(pnl, 4), "reason": reason})
        self.state.d["pos"] = None
        self.state.save()

    # --- main loop -------------------------------------------------------
    def check_stop_fill(self):
        pos = self.state.d["pos"]
        if not pos:
            return
        if self.mode == "paper":
            mark = float(self.data.ticker(SYMBOL)["mark_price"])
            if (pos["side"] > 0 and mark <= pos["stop"]) or (pos["side"] < 0 and mark >= pos["stop"]):
                self.close_local(pos, pos["stop"] * (1 - pos["side"] * SLIPPAGE), "stop")
        else:
            size, _ = self.exchange_position()
            if size == 0:
                self.close_local(pos, pos["stop"], "stop")
                self.cli.cancel_all(self.pid)  # clear any leftover stop orders

    def on_bar_close(self):
        ind = self.indicators()
        i = len(ind.c) - 1
        pos = self.state.d["pos"]
        if pos:
            pos["best"] = max(pos["best"], ind.h[i]) if pos["side"] > 0 else min(pos["best"], ind.l[i])
            self.move_stop(ind.trail_stop(i, pos["side"], pos["stop"], pos["best"]))
            self.state.save()
            return
        eq = self.equity()
        self.state.d["equity"] = eq
        self.guard.update(eq, datetime.now(timezone.utc).date())
        self.state.d["guard"] = {"peak": self.guard.peak, "halted": self.guard.halted}
        self.state.save()
        if self.guard.halted:
            log.error("max drawdown hit (peak $%.2f, now $%.2f). Bot halted; review before restarting.", self.guard.peak, eq)
            return
        if os.path.exists(os.path.join(HERE, "STOP")):
            log.info("STOP file present: no new entries")
            return
        if not self.guard.can_enter(eq):
            log.info("daily loss limit reached: no new entries today")
            return
        sig = ind.entry_signal(i)
        log.info("bar close %.2f ema200 %.2f adx %.1f atr %.2f signal %+d equity $%.2f",
                 ind.c[i], ind.ema[i], ind.adx[i], ind.atr[i], sig, eq)
        record_trade({"time": datetime.now(timezone.utc).isoformat(timespec="seconds"), "mode": self.mode,
                      "close": round(ind.c[i], 2), "ema200": round(ind.ema[i], 2), "adx": round(ind.adx[i], 1),
                      "atr": round(ind.atr[i], 2), "signal": sig, "equity": round(eq, 2)}, "signals.csv")
        if sig:
            self.open_position(sig, ind)

    def check_paper_stop_since(self, since):
        """Scheduled (--once) paper runs happen minutes apart: replay 5m candles since the last
        check so a stop touched in between is not missed."""
        pos = self.state.d["pos"]
        if not pos or self.mode != "paper":
            return
        now = int(time.time())
        for c in self.data.candles(SYMBOL, "5m", max(since, pos["opened"]) // 300 * 300, now):
            if (pos["side"] > 0 and float(c["low"]) <= pos["stop"]) or (pos["side"] < 0 and float(c["high"]) >= pos["stop"]):
                px = pos["stop"]
                o = float(c["open"])
                if (pos["side"] > 0 and o < px) or (pos["side"] < 0 and o > px):
                    px = o  # gapped through the stop
                self.close_local(pos, px * (1 - pos["side"] * SLIPPAGE), "stop")
                return

    def first_start(self):
        step = _RES_SECONDS[RESOLUTION]
        bar = int(time.time()) // step * step
        if self.state.d["last_bar"] == 0 and time.time() - bar > 900:
            # first start mid-bar: don't act on a signal that fired hours ago, wait for the next close
            ind = self.indicators()
            i = len(ind.c) - 1
            log.info("status: close %.2f ema200 %.2f adx %.1f signal-on-last-bar %+d; next decision at %s UTC",
                     ind.c[i], ind.ema[i], ind.adx[i], ind.entry_signal(i),
                     datetime.fromtimestamp(bar + step, timezone.utc).strftime("%Y-%m-%d %H:%M"))
            self.state.d["last_bar"] = bar
            self.state.save()

    def tick(self, once=False):
        step = _RES_SECONDS[RESOLUTION]
        if once:
            self.check_paper_stop_since(self.state.d.get("last_check") or int(time.time()) - 900)
        else:
            self.check_stop_fill()
        bar = int(time.time()) // step * step
        # act once per bar, 20s after it closes so the candle is final
        if bar > self.state.d["last_bar"] and time.time() - bar > 20:
            self.on_bar_close()
            self.state.d["last_bar"] = bar
        self.state.d["last_check"] = self.state.d["heartbeat"] = int(time.time())  # dashboard "alive" signal
        self.state.d["heartbeat_every"] = 900 if once else 30
        self.state.save()

    def run(self, once=False):
        self.first_start()
        if once:
            self.tick(once=True)
            return
        while True:
            try:
                self.tick()
            except DeltaError as e:
                log.error("api error: %s", e)
            except Exception:
                log.exception("unexpected error")
            time.sleep(30)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["paper", "testnet", "live"], default="paper")
    ap.add_argument("--paper-equity", type=float, default=55.0)
    ap.add_argument("--once", action="store_true", help="run a single check and exit (for scheduled cloud runs)")
    a = ap.parse_args()
    if a.once and a.mode != "paper":
        sys.exit("--once is only for paper mode; live/testnet need the continuous loop")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.FileHandler(os.path.join(HERE, f"bot_{a.mode}.log")),
                                  logging.StreamHandler(sys.stdout)])
    load_env()
    if a.mode == "live" and os.environ.get("LIVE_CONFIRM") != "YES":
        sys.exit("Live mode needs LIVE_CONFIRM=YES in .env. Run paper/testnet first.")
    if a.mode == "paper":
        State.path = os.path.join(HERE, "state_paper.json")
    elif a.mode == "testnet":
        State.path = os.path.join(HERE, "state_testnet.json")
    Bot(a.mode, a.paper_equity).run(once=a.once)


if __name__ == "__main__":
    main()
