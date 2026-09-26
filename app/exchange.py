from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
import uuid
import base64
from urllib.parse import urlencode
from decimal import Decimal, ROUND_DOWN
from datetime import UTC, datetime

import aiohttp

from .models import OrderIntent, OrderResult

log = logging.getLogger(__name__)


class ExchangeError(RuntimeError):
    pass


class OrderRejected(ExchangeError):
    """OKX explicitly rejected the order with a structured error response."""
    pass


class OKXDemoClient:
    """OKX Demo futures adapter, including authenticated private updates."""

    base_url = "https://www.okx.com"
    demo_private_ws_url = "wss://wspap.okx.com:8443/ws/v5/private"
    demo_public_ws_url = "wss://wspap.okx.com:8443/ws/v5/public"

    def __init__(self, api_key: str, api_secret: str, passphrase: str):
        self.api_key = api_key
        self.api_secret = api_secret
        self.passphrase = passphrase
        self.session: aiohttp.ClientSession | None = None

    async def _session(self) -> aiohttp.ClientSession:
        if self.session is None:
            connector = aiohttp.TCPConnector(resolver=aiohttp.ThreadedResolver())
            self.session = aiohttp.ClientSession(connector=connector, timeout=aiohttp.ClientTimeout(total=15))
        return self.session

    async def close(self) -> None:
        if self.session:
            await self.session.close()
            self.session = None

    async def private_events(self):
        timestamp = str(int(time.time()))
        prehash = f"{timestamp}GET/users/self/verify"
        signature = base64.b64encode(
            hmac.new(self.api_secret.encode(), prehash.encode(), hashlib.sha256).digest()
        ).decode()
        session = await self._session()
        async with session.ws_connect(
            self.demo_private_ws_url, heartbeat=20, receive_timeout=45
        ) as socket:
            await socket.send_json({"op": "login", "args": [{
                "apiKey": self.api_key,
                "passphrase": self.passphrase,
                "timestamp": timestamp,
                "sign": signature,
            }]})
            authenticated = False
            async for message in socket:
                if message.type == aiohttp.WSMsgType.TEXT:
                    if message.data == "ping":
                        await socket.send_str("pong")
                        continue
                    try:
                        payload = json.loads(message.data)
                    except json.JSONDecodeError:
                        continue
                    if payload.get("event") == "login":
                        if payload.get("code") != "0":
                            raise ExchangeError(payload.get("msg", "OKX Demo WebSocket login failed"))
                        authenticated = True
                        await socket.send_json({"id": uuid.uuid4().hex[:24], "op": "subscribe", "args": [
                            {"channel": "positions", "instType": "ANY", "extraParams": '{"updateInterval":"0"}'},
                            {"channel": "orders", "instType": "SWAP"},
                        ]})
                        continue
                    if payload.get("event") == "error":
                        raise ExchangeError(payload.get("msg", "OKX Demo WebSocket subscription failed"))
                    if authenticated and payload.get("arg"):
                        yield payload
                elif message.type in {aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.ERROR}:
                    raise ExchangeError("OKX Demo WebSocket disconnected")

    async def public_ticker_events(self, instruments: set[str]):
        session = await self._session()
        async with session.ws_connect(
            self.demo_public_ws_url, heartbeat=15, receive_timeout=30,
        ) as socket:
            subscribed_channel = "bbo-tbt"
            await socket.send_json({"op": "subscribe", "args": [
                {"channel": "bbo-tbt", "instId": instrument}
                for instrument in sorted(instruments)
            ]})
            last_log = time.monotonic()
            tick_count = 0

            async for message in socket:
                if message.type == aiohttp.WSMsgType.TEXT:
                    if message.data == "ping":
                        await socket.send_str("pong")
                        continue
                    try:
                        payload = json.loads(message.data)
                    except json.JSONDecodeError:
                        continue

                    event = payload.get("event")

                    if event == "error":
                        if subscribed_channel == "bbo-tbt":
                            msg = payload.get("msg", "")
                            log.warning("OKX Demo rejected bbo-tbt, falling back to tickers: %s", msg)
                            await socket.send_json({"op": "subscribe", "args": [
                                {"channel": "tickers", "instId": instrument}
                                for instrument in sorted(instruments)
                            ]})
                            subscribed_channel = "tickers"
                            continue
                        raise ExchangeError(payload.get("msg", "OKX Demo ticker subscription failed"))

                    if event == "subscribe":
                        log.info(
                            "OKX Demo public feed subscribed: channel=%s inst=%s",
                            payload.get("arg", {}).get("channel"),
                            payload.get("arg", {}).get("instId"),
                        )
                        continue

                    channel = payload.get("arg", {}).get("channel")

                    if channel == "bbo-tbt":
                        for row in payload.get("data") or []:
                            inst = row.get("instId")
                            if inst not in instruments:
                                continue
                            bids = row.get("bids") or []
                            asks = row.get("asks") or []
                            if not bids or not asks:
                                continue
                            try:
                                bid = Decimal(bids[0][0])
                                ask = Decimal(asks[0][0])
                            except (ValueError, IndexError, TypeError):
                                continue
                            mid = (bid + ask) / Decimal("2")
                            tick_count += 1
                            yield {"instId": inst, "last": str(mid), "ts": row.get("ts")}

                    elif channel == "tickers":
                        for row in payload.get("data") or []:
                            if row.get("instId") in instruments and row.get("last"):
                                tick_count += 1
                                yield row

                    now = time.monotonic()
                    if now - last_log >= 30:
                        log.info(
                            "OKX Demo public feed stats: channel=%s ticks_last_30s=%d",
                            subscribed_channel, tick_count,
                        )
                        last_log = now
                        tick_count = 0

                elif message.type in {aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.ERROR}:
                    raise ExchangeError("OKX Demo public ticker feed disconnected")

    def _headers(self, method: str, request_path: str, body: str = "") -> dict[str, str]:
        timestamp = datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        prehash = f"{timestamp}{method.upper()}{request_path}{body}"
        signature = base64.b64encode(hmac.new(self.api_secret.encode(), prehash.encode(), hashlib.sha256).digest()).decode()
        return {
            "OK-ACCESS-KEY": self.api_key,
            "OK-ACCESS-SIGN": signature,
            "OK-ACCESS-TIMESTAMP": timestamp,
            "OK-ACCESS-PASSPHRASE": self.passphrase,
            "x-simulated-trading": "1",
            "Content-Type": "application/json",
        }

    async def account_config(self) -> dict:
        request_path = "/api/v5/account/config"
        session = await self._session()
        async with session.get(f"{self.base_url}{request_path}", headers=self._headers("GET", request_path)) as response:
            try:
                data = await response.json(content_type=None)
            except (ValueError, UnicodeDecodeError):
                raise ExchangeError(f"OKX returned a non-API response (HTTP {response.status})") from None
        if not isinstance(data, dict) or data.get("code") != "0" or not data.get("data"):
            message = data.get("msg", "OKX Demo API validation failed") if isinstance(data, dict) else "OKX Demo API validation failed"
            raise ExchangeError(message)
        return data["data"][0]

    async def set_position_mode(self, position_mode: str) -> dict:
        if position_mode not in {"net_mode", "long_short_mode"}:
            raise ValueError("Unsupported position mode")
        request_path = "/api/v5/account/set-position-mode"
        body = json.dumps({"posMode": position_mode}, separators=(",", ":"))
        session = await self._session()
        async with session.post(f"{self.base_url}{request_path}", data=body, headers=self._headers("POST", request_path, body)) as response:
            data = await self._response_json(response)
        if data.get("code") != "0":
            raise ExchangeError(data.get("msg", "OKX rejected the position mode change"))
        return data["data"][0] if data.get("data") else {"posMode": position_mode}

    async def set_leverage(self, inst_id: str, leverage: Decimal, position_side: str) -> None:
        request_path = "/api/v5/account/set-leverage"
        body = json.dumps({"instId": inst_id, "lever": str(leverage), "mgnMode": "isolated", "posSide": position_side}, separators=(",", ":"))
        session = await self._session()
        async with session.post(f"{self.base_url}{request_path}", data=body, headers=self._headers("POST", request_path, body)) as response:
            data = await self._response_json(response)
        if data.get("code") != "0":
            raise ExchangeError(data.get("msg", "OKX rejected the leverage setting"))

    async def place_market_swap(self, *, inst_id: str, side: str, position_side: str, contracts: Decimal, client_order_id: str, take_profit: Decimal, stop_loss: Decimal) -> dict:
        request_path = "/api/v5/trade/order"
        payload = {"instId": inst_id, "tdMode": "isolated", "side": side, "posSide": position_side, "ordType": "market", "sz": str(contracts), "clOrdId": client_order_id, "attachAlgoOrds": [{"attachAlgoClOrdId": f"tp{client_order_id}"[:32], "tpTriggerPx": str(take_profit), "tpTriggerPxType": "mark", "tpOrdPx": "-1", "slTriggerPx": str(stop_loss), "slTriggerPxType": "mark", "slOrdPx": "-1"}]}
        body = json.dumps(payload, separators=(",", ":"))
        session = await self._session()
        async with session.post(f"{self.base_url}{request_path}", data=body, headers=self._headers("POST", request_path, body)) as response:
            data = await self._response_json(response)
        item = data.get("data", [{}])[0]
        if data.get("code") != "0" or item.get("sCode") not in {None, "0"}:
            raise OrderRejected(item.get("sMsg") or data.get("msg") or "OKX rejected the Demo order")
        return item

    async def close_market_swap(self, *, inst_id: str, side: str, position_side: str, contracts: Decimal, client_order_id: str) -> dict:
        if side not in {"buy", "sell"}:
            raise ValueError("A close order must use buy or sell")
        if position_side not in {"net", "long", "short"}:
            raise ValueError("Unsupported OKX position side")
        if contracts <= 0:
            raise ValueError("Close size must be greater than zero")
        request_path = "/api/v5/trade/order"
        body = json.dumps({"instId": inst_id, "tdMode": "isolated", "side": side, "posSide": position_side, "ordType": "market", "sz": str(contracts), "reduceOnly": True, "clOrdId": client_order_id}, separators=(",", ":"))
        session = await self._session()
        async with session.post(f"{self.base_url}{request_path}", data=body, headers=self._headers("POST", request_path, body)) as response:
            data = await self._response_json(response)
        item = data.get("data", [{}])[0]
        if data.get("code") != "0" or item.get("sCode") not in {None, "0"}:
            raise OrderRejected(item.get("sMsg") or data.get("msg") or "OKX rejected the Demo close order")
        return item

    async def amend_algo_sl(self, *, inst_id: str, algo_clordid: str,
                            new_sl_trigger: Decimal) -> dict:
        """Move the stop-loss leg of an attached algo order to a new trigger.

        `algo_clordid` is the attachAlgoClOrdId we set at entry
        (format: "tp{original_clordid}").
        """
        request_path = "/api/v5/trade/amend-algos"
        body = json.dumps({
            "instId": inst_id,
            "algoClOrdId": algo_clordid,
            "newSlTriggerPx": str(new_sl_trigger),
            "newSlOrdPx": "-1",
        }, separators=(",", ":"))
        session = await self._session()
        async with session.post(
            f"{self.base_url}{request_path}",
            data=body,
            headers=self._headers("POST", request_path, body),
        ) as response:
            data = await self._response_json(response)
        item = data.get("data", [{}])[0]
        if data.get("code") != "0" or item.get("sCode") not in {None, "0"}:
            raise OrderRejected(
                item.get("sMsg") or data.get("msg") or
                "OKX rejected the SL amend"
            )
        return item

    async def order_detail(self, inst_id: str, order_id: str) -> dict | None:
        request_path = f"/api/v5/trade/order?instId={inst_id}&ordId={order_id}"
        session = await self._session()
        async with session.get(f"{self.base_url}{request_path}", headers=self._headers("GET", request_path)) as response:
            data = await self._response_json(response)
        if data.get("code") != "0":
            raise ExchangeError(data.get("msg", "Could not reconcile OKX order"))
        return data.get("data", [None])[0]

    async def order_detail_by_clordid(self, inst_id: str, client_order_id: str) -> dict | None:
        """Fetch an order by our clOrdId (fallback when ordId is unknown)."""
        request_path = f"/api/v5/trade/order?instId={inst_id}&clOrdId={client_order_id}"
        session = await self._session()
        async with session.get(f"{self.base_url}{request_path}", headers=self._headers("GET", request_path)) as response:
            data = await self._response_json(response)
        if data.get("code") != "0":
            raise ExchangeError(data.get("msg", "Could not reconcile OKX order by clOrdId"))
        return data.get("data", [None])[0]

    async def swap_positions(self) -> list[dict]:
        request_path = "/api/v5/account/positions?instType=SWAP"
        session = await self._session()
        async with session.get(f"{self.base_url}{request_path}", headers=self._headers("GET", request_path)) as response:
            data = await self._response_json(response)
        if data.get("code") != "0":
            raise ExchangeError(data.get("msg", "Could not retrieve OKX Demo positions"))
        return [row for row in data.get("data", []) if Decimal(row.get("pos") or "0") != 0]

    async def account_balance(self) -> Decimal:
        request_path = "/api/v5/account/balance?ccy=USDT"
        session = await self._session()
        async with session.get(f"{self.base_url}{request_path}", headers=self._headers("GET", request_path)) as response:
            data = await self._response_json(response)
        if data.get("code") != "0" or not data.get("data"):
            raise ExchangeError(data.get("msg", "Could not retrieve OKX Demo balance"))
        details = data["data"][0].get("details", [])
        usdt = next((item for item in details if item.get("ccy") == "USDT"), None)
        if not usdt:
            return Decimal("0")
        return Decimal(usdt.get("availEq") or usdt.get("availBal") or "0")

    async def swap_instrument(self, inst_id: str) -> dict:
        session = await self._session()
        async with session.get(
            f"{self.base_url}/api/v5/public/instruments",
            params={"instType": "SWAP", "instId": inst_id},
            headers={"x-simulated-trading": "1"},
        ) as response:
            data = await self._response_json(response)
        if data.get("code") != "0" or not data.get("data"):
            raise ExchangeError(data.get("msg", f"No live OKX perpetual instrument found for {inst_id}"))
        return data["data"][0]

    async def list_swap_instruments(self) -> list[dict]:
        """Return SWAP instruments available on OKX Demo Trading."""
        session = await self._session()
        async with session.get(
            f"{self.base_url}/api/v5/public/instruments",
            params={"instType": "SWAP"},
            headers={"x-simulated-trading": "1"},
        ) as response:
            data = await self._response_json(response)
        if data.get("code") != "0" or not isinstance(data.get("data"), list):
            raise ExchangeError(data.get("msg", "OKX instruments list unavailable"))
        return [row for row in data["data"] if row.get("state") == "live"]

    async def last_price(self, inst_id: str) -> Decimal:
        session = await self._session()
        async with session.get(f"{self.base_url}/api/v5/market/ticker", params={"instId": inst_id}) as response:
            data = await self._response_json(response)
        if data.get("code") != "0" or not data.get("data"):
            raise ExchangeError(data.get("msg", f"No OKX market price available for {inst_id}"))
        return Decimal(data["data"][0]["last"])

    async def candles(self, inst_id: str, bar: str = "1H", limit: int = 300) -> list[dict]:
        """Fetch closed candles from OKX public REST. Oldest first."""
        session = await self._session()
        async with session.get(
            f"{self.base_url}/api/v5/market/candles",
            params={"instId": inst_id, "bar": bar, "limit": str(min(limit, 300))},
        ) as response:
            data = await self._response_json(response)
        if data.get("code") != "0" or not isinstance(data.get("data"), list):
            raise ExchangeError(data.get("msg", f"No candles for {inst_id}"))
        rows = list(reversed(data["data"]))
        out = []
        for r in rows:
            if len(r) < 9 or r[8] != "1":
                continue
            out.append({
                "timestamp": int(r[0]),
                "open": float(r[1]),
                "high": float(r[2]),
                "low": float(r[3]),
                "close": float(r[4]),
                "volume": float(r[5]),
                "confirm": True,
            })
        return out

    async def _response_json(self, response: aiohttp.ClientResponse) -> dict:
        try:
            data = await response.json(content_type=None)
        except (ValueError, UnicodeDecodeError):
            raise ExchangeError(f"OKX returned a non-API response (HTTP {response.status})") from None
        if not isinstance(data, dict):
            raise ExchangeError(f"OKX returned an unexpected response (HTTP {response.status})")
        return data


class BybitClient:
    """Minimal V5 client: credentials stay in process memory and auth uses headers."""
    def __init__(self, api_key: str, api_secret: str, testnet: bool = True, environment: str | None = None):
        self.api_key, self.api_secret = api_key, api_secret
        environment = environment or ("testnet" if testnet else "mainnet")
        urls = {"testnet": "https://api-testnet.bybit.com", "demo": "https://api-demo.bybit.com", "mainnet": "https://api.bybit.com"}
        if environment not in urls:
            raise ValueError("Unsupported Bybit environment")
        self.environment = environment
        self.base_url = urls[environment]
        self.session: aiohttp.ClientSession | None = None

    async def _session(self) -> aiohttp.ClientSession:
        if self.session is None:
            connector = aiohttp.TCPConnector(resolver=aiohttp.ThreadedResolver())
            self.session = aiohttp.ClientSession(connector=connector, timeout=aiohttp.ClientTimeout(total=15))
        return self.session

    async def close(self) -> None:
        if self.session:
            await self.session.close()
            self.session = None

    def _headers(self, body: str) -> dict[str, str]:
        timestamp, window = str(int(time.time() * 1000)), "5000"
        payload = f"{timestamp}{self.api_key}{window}{body}"
        signature = hmac.new(self.api_secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
        return {"X-BAPI-API-KEY": self.api_key, "X-BAPI-TIMESTAMP": timestamp, "X-BAPI-RECV-WINDOW": window, "X-BAPI-SIGN": signature, "Content-Type": "application/json"}

    async def _json(self, response: aiohttp.ClientResponse) -> dict:
        try:
            data = await response.json(content_type=None)
        except (ValueError, UnicodeDecodeError):
            raise ExchangeError(
                f"Bybit returned a non-API response (HTTP {response.status}). "
                "Check that the hosting region is permitted by Bybit and retry."
            ) from None
        if not isinstance(data, dict):
            raise ExchangeError(f"Bybit returned an unexpected response (HTTP {response.status})")
        return data

    async def ticker(self, symbol: str) -> Decimal:
        session = await self._session()
        async with session.get(f"{self.base_url}/v5/market/tickers", params={"category": "spot", "symbol": symbol}) as response:
            data = await self._json(response)
        items = data.get("result", {}).get("list", [])
        if data.get("retCode") != 0 or not items:
            raise ExchangeError(f"Ticker unavailable for {symbol}")
        return Decimal(items[0]["lastPrice"])

    async def instrument(self, symbol: str) -> dict:
        session = await self._session()
        async with session.get(f"{self.base_url}/v5/market/instruments-info", params={"category": "spot", "symbol": symbol}) as response:
            data = await self._json(response)
        items = data.get("result", {}).get("list", [])
        if data.get("retCode") != 0 or not items:
            raise ExchangeError(f"Instrument unavailable for {symbol}")
        return items[0]

    async def linear_instrument(self, symbol: str) -> dict:
        session = await self._session()
        async with session.get(f"{self.base_url}/v5/market/instruments-info", params={"category": "linear", "symbol": symbol}) as response:
            data = await self._json(response)
        items = data.get("result", {}).get("list", [])
        if data.get("retCode") != 0 or not items:
            raise ExchangeError(f"No linear futures instrument found for {symbol}")
        return items[0]

    async def place_market_order(self, intent: OrderIntent, stop_loss: Decimal | None = None, take_profit: Decimal | None = None) -> OrderResult:
        info = await self.instrument(intent.symbol)
        price = await self.ticker(intent.symbol)
        lot = info["lotSizeFilter"]
        qty = (intent.notional_usdt / price).quantize(Decimal(lot["basePrecision"]), rounding=ROUND_DOWN)
        if qty <= 0 or qty < Decimal(lot["minOrderQty"]):
            raise ExchangeError("Order is below Bybit minimum quantity")
        payload = {"category": "spot", "symbol": intent.symbol, "side": intent.side.value, "orderType": "Market", "qty": str(qty), "marketUnit": "baseCoin", "orderLinkId": intent.idempotency_key}
        if stop_loss is not None and take_profit is not None:
            payload.update({"stopLoss": str(stop_loss), "takeProfit": str(take_profit), "tpOrderType": "Market", "slOrderType": "Market"})
        body = json.dumps(payload, separators=(",", ":"), sort_keys=True)
        session = await self._session()
        async with session.post(f"{self.base_url}/v5/order/create", data=body, headers=self._headers(body)) as response:
            data = await self._json(response)
        if data.get("retCode") != 0:
            raise ExchangeError(data.get("retMsg", "Bybit rejected order"))
        return OrderResult(data["result"]["orderId"], "NEW", price, qty)

    async def order_status(self, order_id: str) -> OrderResult:
        query = urlencode(sorted({"category": "spot", "orderId": order_id}.items()))
        session = await self._session()
        async with session.get(f"{self.base_url}/v5/order/realtime?{query}", headers=self._headers(query)) as response:
            data = await self._json(response)
        orders = data.get("result", {}).get("list", [])
        if data.get("retCode") != 0 or not orders:
            raise ExchangeError(f"Could not reconcile order {order_id}")
        order = orders[0]
        return OrderResult(order_id, order["orderStatus"].upper(), Decimal(order.get("avgPrice") or "0"), Decimal(order.get("cumExecQty") or "0"))

    async def api_key_info(self) -> dict:
        session = await self._session()
        query = ""
        async with session.get(f"{self.base_url}/v5/user/query-api", headers=self._headers(query)) as response:
            data = await self._json(response)
        if data.get("retCode") != 0:
            raise ExchangeError(data.get("retMsg", "Bybit API key validation failed"))
        return data["result"]

    async def rsi_14(self, symbol: str, interval: str = "15") -> Decimal:
        session = await self._session()
        async with session.get(f"{self.base_url}/v5/market/kline", params={"category": "spot", "symbol": symbol, "interval": interval, "limit": "30"}) as response:
            data = await self._json(response)
        rows = data.get("result", {}).get("list", [])
        if data.get("retCode") != 0 or len(rows) < 15:
            raise ExchangeError(f"Insufficient market data for {symbol}")
        closes = [Decimal(row[4]) for row in sorted(rows, key=lambda item: int(item[0]))]
        changes = [closes[i] - closes[i - 1] for i in range(1, 15)]
        gain = sum((max(change, Decimal("0")) for change in changes), Decimal("0")) / Decimal("14")
        loss = sum((max(-change, Decimal("0")) for change in changes), Decimal("0")) / Decimal("14")
        if loss == 0:
            return Decimal("100")
        return Decimal("100") - Decimal("100") / (Decimal("1") + gain / loss)


class PaperBroker:
    """Deterministic simulated execution using the same public ticker feed."""
    def __init__(self, market: BybitClient):
        self.market = market

    async def place_market_order(self, intent: OrderIntent) -> OrderResult:
        price = await self.market.ticker(intent.symbol)
        qty = (intent.notional_usdt / price).quantize(Decimal("0.00000001"), rounding=ROUND_DOWN)
        if qty <= 0:
            raise ExchangeError("Notional too small")
        return OrderResult(f"paper-{uuid.uuid4()}", "FILLED", price, qty)
