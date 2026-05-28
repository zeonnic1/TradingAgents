from enum import Enum


class TaskFinalStatus(str, Enum):
    COMPLETED = "COMPLETED"
    CANCELED = "CANCELED"
    FAILED = "FAILED"
    DELETED = "DELETED"


class ExchangeName(str, Enum):
    BINANCE = "binance"
    OKX = "okx"
    BYBIT = "bybit"


class OrderSide(str, Enum):
    BUY = "buy"
    SELL = "sell"


class OrderType(str, Enum):
    MARKET = "market"
    LIMIT = "limit"


class SignalAction(str, Enum):
    BUY = "buy"
    SELL = "sell"
    HOLD = "hold"


class LLMRiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
