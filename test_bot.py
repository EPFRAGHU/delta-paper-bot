"""Offline tests: signature matches Delta docs, sizing respects limits, live order flow is sane."""
import hashlib
import hmac
import os
import tempfile

import bot as botmod
from risk import RiskConfig, size_contracts


def test_signature_follows_docs_format():
    # docs: signature_data = method + timestamp + path + query_string('?a=b') + payload
    import io, json, urllib.request
    import delta_client
    seen = {}

    def fake_urlopen(req, timeout=None):
        seen["h"] = {k.lower(): v for k, v in req.header_items()}
        seen["url"] = req.full_url
        return io.BytesIO(b'{"success": true, "result": []}')

    orig = urllib.request.urlopen
    delta_client.urllib.request.urlopen = fake_urlopen
    try:
        c = delta_client.DeltaClient("k", "s3cret", "https://x")
        c._request("GET", "/v2/orders", params={"product_id": 1, "state": "open"}, auth=True)
        ts = seen["h"]["timestamp"]
        exp = hmac.new(b"s3cret", f"GET{ts}/v2/orders?product_id=1&state=open".encode(), hashlib.sha256).hexdigest()
        assert seen["h"]["signature"] == exp and seen["h"]["api-key"] == "k"
        c._request("POST", "/v2/orders", body={"size": 1}, auth=True)
        ts = seen["h"]["timestamp"]
        exp = hmac.new(b"s3cret", (f"POST{ts}/v2/orders" + json.dumps({"size": 1}, separators=(",", ":"))).encode(),
                       hashlib.sha256).hexdigest()
        assert seen["h"]["signature"] == exp
    finally:
        delta_client.urllib.request.urlopen = orig


def test_sizing_never_exceeds_10x():
    rc = RiskConfig()
    for eq in (20, 55, 500, 5000):
        for stop_pct in (0.001, 0.01, 0.05):
            price = 2300.0
            q = size_contracts(eq, price, price * (1 - stop_pct), 0.01, rc)
            assert q * 0.01 * price <= eq * 10 + 1e-9


def test_sizing_skips_when_one_contract_too_risky():
    # $55 equity, 1 BTC contract ($80 notional) with a 10% stop = $8 loss > 3% of equity
    assert size_contracts(55, 80000, 72000, 0.001, RiskConfig()) == 0


class FakeExchange:
    def __init__(self):
        self.size, self.entry, self.orders, self.calls, self.n = 0, 0.0, {}, [], 0

    def product(self, s):
        return {"id": 3136, "contract_value": "0.01", "tick_size": "0.05"}

    def ticker(self, s):
        return {"mark_price": "2300"}

    def balances(self):
        return {"meta": {"net_equity": "55"}, "result": []}

    def set_leverage(self, pid, lev):
        assert int(lev) <= 10
        self.calls.append(("lev", lev))

    def position(self, pid):
        return {"size": self.size, "entry_price": str(self.entry)}

    def open_orders(self, pid):
        return list(self.orders.values())

    def place_order(self, pid, side, size, order_type, client_order_id=None, **kw):
        self.size += size if side == "buy" else -size
        self.entry = 2300.0
        self.calls.append(("order", side, size))
        return {"average_fill_price": "2300"}

    def place_stop(self, pid, side, size, stop):
        self.n += 1
        self.orders[self.n] = {"id": self.n, "stop_order_type": "stop_loss_order", "stop_price": stop}
        self.calls.append(("stop", side, size, stop))
        return {"id": self.n}

    def cancel_order(self, pid, oid):
        self.orders.pop(oid, None)
        self.calls.append(("cancel", oid))

    def cancel_all(self, pid):
        self.orders.clear()


class FakeInd:
    def __init__(self, sig):
        self.sig = sig
        self.c, self.h, self.l = [2300.0], [2310.0], [2290.0]
        self.atr, self.ema, self.adx = [40.0], [2200.0], [30.0]

    def entry_signal(self, i):
        return self.sig

    def trail_stop(self, i, side, cur, best):
        return max(cur, best - 3 * 40.0)


def test_live_flow_long_then_stopped():
    d = tempfile.mkdtemp()
    botmod.State.path = os.path.join(d, "s.json")
    botmod.HERE = d
    import json
    json.dump({"entry_len": 40, "exit_len": 20, "atr_stop": 2.5, "atr_trail": 3.0},
              open(os.path.join(d, "params.json"), "w"))
    fx = FakeExchange()
    orig = botmod.DeltaClient
    botmod.DeltaClient = lambda *a, **k: fx
    try:
        b = botmod.Bot("live", 55)
        b.indicators = lambda: FakeInd(1)
        b.on_bar_close()
        pos = b.state.d["pos"]
        assert pos and pos["side"] == 1 and fx.size == pos["qty"] > 0
        assert pos["stop"] == 2200.0 and len(fx.orders) == 1  # 2300 - 2.5*40
        assert fx.size * 0.01 * 2300 <= 55 * 10
        # price rises, trail tightens: new stop placed before old cancelled
        fi = FakeInd(0); fi.h = [2500.0]
        b.indicators = lambda: fi
        b.on_bar_close()
        assert b.state.d["pos"]["stop"] == 2380.0 and len(fx.orders) == 1
        kinds = [c[0] for c in fx.calls]
        assert kinds.index("cancel") > kinds.index("stop", kinds.index("stop") + 1)
        # exchange stop fills -> bot goes flat and logs trade
        fx.size = 0
        b.check_stop_fill()
        assert b.state.d["pos"] is None
        assert os.path.exists(os.path.join(d, "trades.csv"))
    finally:
        botmod.DeltaClient = orig


def test_paper_once_replays_stop_between_runs():
    import json
    d = tempfile.mkdtemp()
    botmod.State.path = os.path.join(d, "s.json")
    botmod.HERE = d
    json.dump({"atr_stop": 2.5}, open(os.path.join(d, "params.json"), "w"))
    fx = FakeExchange()
    # price dipped to 2190 between scheduled runs, then recovered; mark now is 2300
    fx.candles = lambda sym, res, start, end: [
        {"time": start, "open": 2300, "high": 2305, "low": 2250, "close": 2260},
        {"time": start + 300, "open": 2260, "high": 2265, "low": 2190, "close": 2240},
        {"time": start + 600, "open": 2240, "high": 2300, "low": 2235, "close": 2300},
    ]
    orig = botmod.DeltaClient
    botmod.DeltaClient = lambda *a, **k: fx
    try:
        b = botmod.Bot("paper", 55)
        b.state.d["pos"] = {"side": 1, "qty": 1, "entry": 2300.0, "stop": 2200.0, "best": 2300.0,
                            "stop_id": None, "opened": 1000}
        b.check_paper_stop_since(1000)
        assert b.state.d["pos"] is None
        assert b.state.d["paper_equity"] < 55  # loss booked at the stop, not at the recovered mark
    finally:
        botmod.DeltaClient = orig


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("PASS", name)
