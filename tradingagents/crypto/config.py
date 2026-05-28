from __future__ import annotations

import os
from dataclasses import dataclass

from .models import ExchangeName


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class CryptoSettings:
    paper_trading: bool = True
    celery_broker_url: str = "redis://localhost:6379/0"
    celery_result_backend: str = "redis://localhost:6379/1"
    database_url: str = "mysql+pymysql://tradingagents:tradingagents@localhost:3306/tradingagents_crypto"
    default_exchange: ExchangeName = ExchangeName.BINANCE
    default_symbol: str = "BTC/USDT"
    default_timeframe: str = "15m"
    task_poll_interval_seconds: float = 10.0
    task_poll_max_attempts: int = 0
    order_monitor_interval_seconds: float = 10.0
    order_monitor_max_attempts: int = 360


def get_crypto_settings() -> CryptoSettings:
    live_enabled = _env_bool("CRYPTO_TRADING_LIVE_ENABLED", False)
    exchange = os.getenv("CRYPTO_DEFAULT_EXCHANGE", ExchangeName.BINANCE.value).lower()
    exchange_aliases = {"okey": "okx", "okex": "okx", "bybite": "bybit"}
    exchange = exchange_aliases.get(exchange, exchange)
    return CryptoSettings(
        paper_trading=not live_enabled,
        celery_broker_url=os.getenv("CELERY_BROKER_URL", "redis://localhost:6379/0"),
        celery_result_backend=os.getenv("CELERY_RESULT_BACKEND", "redis://localhost:6379/1"),
        database_url=os.getenv(
            "CRYPTO_DATABASE_URL",
            "mysql+pymysql://tradingagents:tradingagents@localhost:3306/tradingagents_crypto",
        ),
        default_exchange=ExchangeName(exchange),
        default_symbol=os.getenv("CRYPTO_DEFAULT_SYMBOL", "BTC/USDT").upper(),
        default_timeframe=os.getenv("CRYPTO_DEFAULT_TIMEFRAME", "15m"),
        task_poll_interval_seconds=float(os.getenv("CRYPTO_TASK_POLL_INTERVAL_SECONDS", "10")),
        task_poll_max_attempts=int(os.getenv("CRYPTO_TASK_POLL_MAX_ATTEMPTS", "0")),
        order_monitor_interval_seconds=float(os.getenv("CRYPTO_ORDER_MONITOR_INTERVAL_SECONDS", "10")),
        order_monitor_max_attempts=int(os.getenv("CRYPTO_ORDER_MONITOR_MAX_ATTEMPTS", "360")),
    )


def exchange_credentials(exchange: ExchangeName) -> dict:
    prefix = exchange.value.upper()
    return {
        "apiKey": os.getenv(f"{prefix}_API_KEY", ""),
        "secret": os.getenv(f"{prefix}_API_SECRET", ""),
        "password": os.getenv(f"{prefix}_API_PASSWORD", ""),
    }


def missing_exchange_credentials(exchange: ExchangeName) -> list[str]:
    credentials = exchange_credentials(exchange)
    required = ["apiKey", "secret"]
    if exchange == ExchangeName.OKX:
        required.append("password")
    return [name for name in required if not credentials.get(name)]
