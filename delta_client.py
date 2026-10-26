"""Minimal Delta Exchange India REST client (signed requests per docs.delta.exchange)."""
import hashlib
import hmac
import json
import time
import urllib.error
import urllib.parse
import urllib.request

PROD_URL = "https://api.india.delta.exchange"
TESTNET_URL = "https://cdn-ind.testnet.deltaex.org"


class DeltaError(Exception):
    pass


class DeltaClient:
    def __init__(self, api_key="", api_secret="", base_url=PROD_URL, timeout=15):
        self.api_key = api_key
        self.api_secret = api_secret
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    # --- transport -------------------------------------------------------
    def _request(self, method, path, params=None, body=None, auth=False, retries=3):
        query = ""
        if params:
            query = "?" + urllib.parse.urlencode(params)
        payload = json.dumps(body, separators=(",", ":")) if body is not None else ""
        headers = {
            "User-Agent": "python-delta-bot",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        last_err = None
        for attempt in range(retries):
            if auth:
                if not (self.api_key and self.api_secret):
                    raise DeltaError("API key/secret missing")
                ts = str(int(time.time()))
                prehash = method + ts + path + query + payload
                sig = hmac.new(self.api_secret.encode(), prehash.encode(), hashlib.sha256).hexdigest()
                headers.update({"api-key": self.api_key, "timestamp": ts, "signature": sig})
            req = urllib.request.Request(
                self.base_url + path + query,
                data=payload.encode() if payload else None,
                headers=headers,
                method=method,
            )
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    data = json.load(resp)
            except urllib.error.HTTPError as e:
                text = e.read().decode(errors="replace")
                # 429 / 5xx are retryable; 4xx order errors are not
                if e.code == 429 or e.code >= 500:
                    last_err = DeltaError(f"HTTP {e.code}: {text}")
                    time.sleep(2 * (attempt + 1))
                    continue
                raise DeltaError(f"HTTP {e.code} {method} {path}: {text}") from None
            except (urllib.error.URLError, TimeoutError) as e:
                last_err = DeltaError(f"network: {e}")
                time.sleep(2 * (attempt + 1))
                continue
            if not data.get("success", True):
                raise DeltaError(f"{method} {path}: {data.get('error')}")
            return data
        raise last_err

    # --- public ----------------------------------------------------------
    def product(self, symbol):
        return self._request("GET", f"/v2/products/{symbol}")["result"]

    def ticker(self, symbol):
        return self._request("GET", f"/v2/tickers/{symbol}")["result"]

    def candles(self, symbol, resolution, start, end):
        """Return candles oldest->newest. Max 2000 per call, so page through."""
        step = _RES_SECONDS[resolution]
        out = {}
        cur = start
        while cur < end:
            chunk_end = min(end, cur + step * 1999)
            res = self._request("GET", "/v2/history/candles", params={
                "resolution": resolution, "symbol": symbol, "start": cur, "end": chunk_end,
            })["result"]
            for c in res:
                out[c["time"]] = c
            cur = chunk_end + step
            time.sleep(0.2)
        return [out[k] for k in sorted(out)]

    # --- private ---------------------------------------------------------
    def balances(self):
        return self._request("GET", "/v2/wallet/balances", auth=True)

    def position(self, product_id):
        return self._request("GET", "/v2/positions", params={"product_id": product_id}, auth=True)["result"]

    def set_leverage(self, product_id, leverage):
        return self._request("POST", f"/v2/products/{product_id}/orders/leverage",
                             body={"leverage": str(leverage)}, auth=True)["result"]

    def open_orders(self, product_id):
        return self._request("GET", "/v2/orders", params={"product_ids": product_id, "states": "open,pending"},
                             auth=True)["result"]

    def place_order(self, product_id, side, size, order_type="market_order", limit_price=None,
                    reduce_only=False, stop_loss=None, take_profit=None, client_order_id=None):
        body = {
            "product_id": product_id,
            "size": int(size),
            "side": side,
            "order_type": order_type,
            "reduce_only": reduce_only,
        }
        if limit_price is not None:
            body["limit_price"] = str(limit_price)
        if stop_loss is not None:
            body["bracket_stop_loss_price"] = str(stop_loss)
            body["bracket_stop_trigger_method"] = "mark_price"
        if take_profit is not None:
            body["bracket_take_profit_price"] = str(take_profit)
        if client_order_id:
            body["client_order_id"] = client_order_id
        return self._request("POST", "/v2/orders", body=body, auth=True)["result"]

    def place_stop(self, product_id, side, size, stop_price):
        """Standalone reduce-only stop-loss market order (used to trail the stop)."""
        body = {
            "product_id": product_id,
            "size": int(size),
            "side": side,
            "order_type": "market_order",
            "stop_order_type": "stop_loss_order",
            "stop_price": str(stop_price),
            "stop_trigger_method": "mark_price",
            "reduce_only": True,
        }
        return self._request("POST", "/v2/orders", body=body, auth=True)["result"]

    def cancel_order(self, product_id, order_id):
        return self._request("DELETE", "/v2/orders", body={"id": order_id, "product_id": product_id}, auth=True)

    def cancel_all(self, product_id):
        return self._request("DELETE", "/v2/orders/all", body={"product_id": product_id}, auth=True)


_RES_SECONDS = {"1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600,
                "2h": 7200, "4h": 14400, "6h": 21600, "1d": 86400}
