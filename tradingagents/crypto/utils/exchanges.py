from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Protocol
from uuid import uuid4

from tradingagents.crypto.config import exchange_credentials, get_crypto_settings, missing_exchange_credentials
from tradingagents.crypto.models import Candle, ExchangeName, OrderRequest


class ExchangeClient(Protocol):
    exchange: ExchangeName

    def fetch_ohlcv(self, symbol: str, timeframe: str = "15m", limit: int = 300) -> List[Candle]:
        ...

    def fetch_ticker(self, symbol: str) -> Dict[str, Any]:
        ...

    def fetch_balance(self) -> Dict[str, Any]:
        ...

    def create_order(self, request: OrderRequest) -> Dict[str, Any]:
        ...


class PaperExchangeClient:
    def __init__(self, exchange: ExchangeName):
        self.exchange = exchange

    def fetch_ohlcv(self, symbol: str, timeframe: str = "15m", limit: int = 300) -> List[Candle]:
        return _ccxt_client(self.exchange).fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)

    def fetch_ticker(self, symbol: str) -> Dict[str, Any]:
        return _ccxt_client(self.exchange).fetch_ticker(symbol)

    def fetch_balance(self) -> Dict[str, Any]:
        return {
            "free": {"USDT": 100000.0, "BTC": 0.0, "ETH": 0.0},
            "used": {"USDT": 0.0, "BTC": 0.0, "ETH": 0.0},
            "total": {"USDT": 100000.0, "BTC": 0.0, "ETH": 0.0},
            "info": {"message": "Paper account balance. No live exchange credentials were used."},
        }

    def create_order(self, request: OrderRequest) -> Dict[str, Any]:
        now = datetime.now(timezone.utc).isoformat()
        ticker = self.fetch_ticker(request.symbol)
        entry_price = request.price or ticker.get("last") or ticker.get("close")
        contract_amount = _usdt_contract_amount(request.amount, request.leverage, float(entry_price))
        return {
            "id": f"paper-{uuid4()}",
            "timestamp": now,
            "datetime": now,
            "symbol": request.symbol,
            "type": request.type.value,
            "side": request.side.value,
            "amount": contract_amount,
            "price": entry_price,
            "average": entry_price,
            "status": "paper_accepted",
            "info": {
                "message": "Paper trading mode is enabled. No live order was sent.",
                "exchange": self.exchange.value,
                "leverage": request.leverage,
                "amount_unit": "USDT",
                "margin_usdt": request.amount,
                "notional_usdt": request.amount * request.leverage,
                "contract_amount": contract_amount,
                "params": request.params,
            },
        }


class CcxtExchangeClient:
    def __init__(self, exchange: ExchangeName):
        self.exchange = exchange
        self._client = _ccxt_client(exchange, authenticated=True)

    def fetch_ohlcv(self, symbol: str, timeframe: str = "15m", limit: int = 300) -> List[Candle]:
        return self._client.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)

    def fetch_ticker(self, symbol: str) -> Dict[str, Any]:
        return self._client.fetch_ticker(symbol)

    def fetch_balance(self) -> Dict[str, Any]:
        return self._client.fetch_balance()

    def create_order(self, request: OrderRequest) -> Dict[str, Any]:
        if request.leverage > 1:
            self._client.set_leverage(request.leverage, request.symbol)
        ticker = self.fetch_ticker(request.symbol)
        entry_price = request.price or ticker.get("last") or ticker.get("close")
        contract_amount = _usdt_contract_amount(request.amount, request.leverage, float(entry_price))
        params = dict(request.params)
        params.setdefault("leverage", request.leverage)
        params.setdefault("amount_unit", "USDT")
        params.setdefault("margin_usdt", request.amount)
        params.setdefault("notional_usdt", request.amount * request.leverage)
        order = self._client.create_order(
            request.symbol,
            request.type.value,
            request.side.value,
            contract_amount,
            request.price,
            params,
        )
        info = dict(order.get("info") or {})
        info.update({
            "amount_unit": "USDT",
            "margin_usdt": request.amount,
            "notional_usdt": request.amount * request.leverage,
            "contract_amount": contract_amount,
        })
        order["info"] = info
        order.setdefault("amount", contract_amount)
        return order


class _CcxtAdapter:
    def __init__(self, raw: Any):
        self.raw = raw

    def fetch_ohlcv(self, symbol: str, timeframe: str = "15m", limit: int = 300) -> List[Candle]:
        rows = self.raw.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
        return [Candle.from_ccxt(row) for row in rows]

    def fetch_ticker(self, symbol: str) -> Dict[str, Any]:
        return self.raw.fetch_ticker(symbol)

    def fetch_balance(self) -> Dict[str, Any]:
        return self.raw.fetch_balance()

    def create_order(
        self,
        symbol: str,
        order_type: str,
        side: str,
        amount: float,
        price: float | None,
        params: Dict[str, Any],
    ) -> Dict[str, Any]:
        return self.raw.create_order(symbol, order_type, side, amount, price, params)

    def set_leverage(self, leverage: int, symbol: str) -> None:
        if hasattr(self.raw, "set_leverage"):
            self.raw.set_leverage(leverage, symbol)


def get_exchange_client(exchange: ExchangeName | str) -> ExchangeClient:
    exchange_name = normalize_exchange(exchange)
    settings = get_crypto_settings()
    if settings.paper_trading:
        return PaperExchangeClient(exchange_name)
    return CcxtExchangeClient(exchange_name)


def normalize_exchange(exchange: ExchangeName | str) -> ExchangeName:
    if isinstance(exchange, ExchangeName):
        return exchange
    aliases = {
        "okey": "okx",
        "okex": "okx",
        "bybite": "bybit",
    }
    normalized = aliases.get(exchange.strip().lower(), exchange.strip().lower())
    return ExchangeName(normalized)


def _usdt_contract_amount(margin_usdt: float, leverage: int, price: float) -> float:
    if price <= 0:
        raise ValueError("Cannot convert USDT contract amount without a positive mark price.")
    return round((float(margin_usdt) * max(int(leverage), 1)) / price, 8)


def _ccxt_client(exchange: ExchangeName, authenticated: bool = False) -> _CcxtAdapter:
    try:
        import ccxt
    except ImportError as exc:
        raise RuntimeError(
            "ccxt is required for crypto exchange access. Install project dependencies with `pip install .`."
        ) from exc

    ccxt_id = {
        ExchangeName.BINANCE: "binance",
        ExchangeName.OKX: "okx",
        ExchangeName.BYBIT: "bybit",
    }[exchange]
    exchange_cls = getattr(ccxt, ccxt_id)
    options: Dict[str, Any] = {
        "enableRateLimit": True,
        "options": _ccxt_contract_options(exchange),
    }
    if authenticated:
        missing = missing_exchange_credentials(exchange)
        if missing:
            missing_text = ", ".join(missing)
            raise RuntimeError(f"{exchange.value} API credentials are incomplete: missing {missing_text}.")
        creds = exchange_credentials(exchange)
        options.update({k: v for k, v in creds.items() if v})
    raw = exchange_cls(options)
    return _CcxtAdapter(raw)


def _ccxt_contract_options(exchange: ExchangeName) -> Dict[str, Any]:
    if exchange == ExchangeName.BINANCE:
        return {
            "defaultType": "future",
            "defaultSubType": "linear",
        }
    if exchange == ExchangeName.OKX:
        return {
            "defaultType": "swap",
        }
    if exchange == ExchangeName.BYBIT:
        return {
            "defaultType": "swap",
            "defaultSubType": "linear",
        }
    return {}
