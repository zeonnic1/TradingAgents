from __future__ import annotations

import json
from typing import List, Protocol

from tradingagents.crypto.managers.redis_manager import create_redis_client
from tradingagents.crypto.models import AnalyzeRequest, Candle, ExchangeName, MarketSnapshot
from tradingagents.crypto.utils.exchanges import get_exchange_client


class CandleStore(Protocol):
    def reset(self, exchange: ExchangeName, symbol: str, timeframe: str) -> None:
        ...

    def count(self, exchange: ExchangeName, symbol: str, timeframe: str) -> int:
        ...

    def save_many(
        self,
        exchange: ExchangeName,
        symbol: str,
        timeframe: str,
        candles: List[Candle],
        update_existing: bool = False,
    ) -> int:
        ...

    def list_recent(self, exchange: ExchangeName, symbol: str, timeframe: str, limit: int) -> List[Candle]:
        ...


class RedisCandleStore:
    """Redis K 线仓储。

    每个 exchange/symbol/timeframe 一个 sorted set，score 使用毫秒时间戳。
    value 是 Candle JSON。写入前按 timestamp 查询，已存在则跳过。
    """

    def __init__(self, redis_client=None):
        self.redis_client = redis_client
        self._owned_client = None

    def reset(self, exchange: ExchangeName, symbol: str, timeframe: str) -> None:
        client = self._client()
        client.delete(candle_cache_key(exchange, symbol, timeframe))

    def count(self, exchange: ExchangeName, symbol: str, timeframe: str) -> int:
        return int(self._client().zcard(candle_cache_key(exchange, symbol, timeframe)) or 0)

    def save_many(
        self,
        exchange: ExchangeName,
        symbol: str,
        timeframe: str,
        candles: List[Candle],
        update_existing: bool = False,
    ) -> int:
        if not candles:
            return 0
        client = self._client()
        key = candle_cache_key(exchange, symbol, timeframe)
        saved = 0
        for candle in candles:
            if client.zcount(key, candle.timestamp, candle.timestamp):
                if update_existing:
                    client.zremrangebyscore(key, candle.timestamp, candle.timestamp)
                else:
                    continue
            client.zadd(key, {json.dumps(candle.model_dump(mode="json"), ensure_ascii=False): candle.timestamp})
            saved += 1
        return saved

    def list_recent(self, exchange: ExchangeName, symbol: str, timeframe: str, limit: int) -> List[Candle]:
        rows = self._client().zrevrange(candle_cache_key(exchange, symbol, timeframe), 0, max(limit - 1, 0))
        candles = []
        for row in reversed(rows):
            if isinstance(row, bytes):
                row = row.decode("utf-8")
            candles.append(Candle(**json.loads(row)))
        return candles

    def _client(self):
        if self.redis_client is not None:
            return self.redis_client
        if self._owned_client is None:
            self._owned_client = create_redis_client()
        return self._owned_client


class CandleCache:
    """K 线缓存服务。

    初始化某个 exchange/symbol/timeframe 时先清理 Redis 中对应 K 线数据，再按
    传入 limit 拉取全量 K 线写入 Redis。后续策略轮询只拉最新一根 K 线，写入
    Redis；timestamp 已存在则跳过。
    """

    def __init__(self, store: CandleStore | None = None):
        self.store = store or RedisCandleStore()
        self._initialized_keys: set[tuple[str, str, str]] = set()

    def get_snapshot(self, request: AnalyzeRequest, *, refresh_full: bool = False) -> MarketSnapshot:
        client = get_exchange_client(request.exchange)
        key = _identity(request.exchange, request.symbol, request.timeframe)
        if key not in self._initialized_keys:
            self.store.reset(request.exchange, request.symbol, request.timeframe)
            initial_candles = client.fetch_ohlcv(request.symbol, request.timeframe, request.limit)
            self.store.save_many(request.exchange, request.symbol, request.timeframe, initial_candles)
            self._initialized_keys.add(key)
        elif refresh_full and self.store.count(request.exchange, request.symbol, request.timeframe) < request.limit:
            expanded_candles = client.fetch_ohlcv(request.symbol, request.timeframe, request.limit)
            self.store.save_many(request.exchange, request.symbol, request.timeframe, expanded_candles)

        latest_candles = client.fetch_ohlcv(request.symbol, request.timeframe, 1)
        self.store.save_many(request.exchange, request.symbol, request.timeframe, latest_candles, update_existing=True)

        return MarketSnapshot(
            exchange=request.exchange,
            symbol=request.symbol,
            timeframe=request.timeframe,
            candles=self.store.list_recent(request.exchange, request.symbol, request.timeframe, request.limit),
        )


def candle_cache_key(exchange: ExchangeName | str, symbol: str, timeframe: str) -> str:
    exchange_value = exchange.value if isinstance(exchange, ExchangeName) else str(exchange)
    normalized_symbol = symbol.upper().replace("/", "_")
    return f"crypto:candles:{exchange_value}:{normalized_symbol}:{timeframe}"


def _identity(exchange: ExchangeName, symbol: str, timeframe: str) -> tuple[str, str, str]:
    return (exchange.value, symbol.upper(), timeframe)
