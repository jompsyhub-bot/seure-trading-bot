from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid
import base64
from urllib.parse import urlencode
from decimal import Decimal, ROUND_DOWN
from datetime import UTC, datetime

import aiohttp

from .models import OrderIntent, OrderResult


class ExchangeError(RuntimeError):
    pass


class OKXDemoClient:
    """Read-only validation client for OKX Demo Trading.

    Order execution is intentionally not included until its futures-specific
    risk, contract-sizing, and reconciliation layer has been tested.
    """

    base_url = "https://www.okx.com"

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
        # availEq is the safest value for a new cross/isolated position.
        return Decimal(usdt.get("availEq") or usdt.get("availBal") or "0")

    async def swap_instrument(self, inst_id: str) -> dict:
        session = await self._session()
        async with session.get(f"{self.base_url}/api/v5/public/instruments", params={"instType": "SWAP", "instId": inst_id}) as response:
            data = await self._response_json(response)
        if data.get("code") != "0" or not data.get("data"):
            raise ExchangeError(data.get("msg", f"No live OKX perpetual instrument found for {inst_id}"))
        return data["data"][0]

    async def last_price(self, inst_id: str) -> Decimal:
        session = await self._session()
        async with session.get(f"{self.base_url}/api/v5/market/ticker", params={"instId": inst_id}) as response:
            data = await self._response_json(response)
        if data.get("code") != "0" or not data.get("data"):
            raise ExchangeError(data.get("msg", f"No OKX market price available for {inst_id}"))
        return Decimal(data["data"][0]["last"])

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
            # On some Windows installs aiodns cannot contact the configured DNS
            # server. ThreadedResolver uses the OS resolver instead.
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
        """Convert exchange responses without leaking HTML/error pages to users.

        A non-JSON response usually means an upstream WAF, regional restriction,
        or temporary proxy failure—not an invalid user API key.
        """
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
        """Public linear-perpetual rules; no account secret is sent."""
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
        # Creation acknowledgement is not a fill. Reconciliation must confirm it.
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
        """Validate a newly supplied key without logging it or persisting failed attempts."""
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
