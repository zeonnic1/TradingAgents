from __future__ import annotations

from .enum import *
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator


class Candle(BaseModel):
    timestamp: int
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0

    @classmethod
    def from_ccxt(cls, row: List[float]) -> "Candle":
        return cls(
            timestamp=int(row[0]),
            open=float(row[1]),
            high=float(row[2]),
            low=float(row[3]),
            close=float(row[4]),
            volume=float(row[5]) if len(row) > 5 else 0.0,
        )


class MarketSnapshot(BaseModel):
    exchange: ExchangeName
    symbol: str
    timeframe: str
    candles: List[Candle]


class TradingHubAnalysis(BaseModel):
    action: SignalAction
    confidence: float = Field(ge=0.0, le=1.0)
    bias: str
    setup: str
    entry: Optional[float] = None
    stop_loss: Optional[float] = None
    take_profit_1: Optional[float] = None
    take_profit_2: Optional[float] = None
    reasons: List[str] = Field(default_factory=list)
    invalidation: List[str] = Field(default_factory=list)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class AnalyzeRequest(BaseModel):
    exchange: ExchangeName = ExchangeName.BINANCE
    symbol: str = "BTC/USDT"
    timeframe: str = "15m"
    limit: int = Field(default=300, ge=60, le=1000)

    @field_validator("symbol")
    @classmethod
    def normalize_symbol(cls, value: str) -> str:
        return value.strip().upper()


class OrderRequest(BaseModel):
    exchange: ExchangeName
    symbol: str
    side: OrderSide
    type: OrderType = OrderType.MARKET
    amount: float = Field(gt=0, description="USDT margin amount for the contract order.")
    price: Optional[float] = Field(default=None, gt=0)
    leverage: int = Field(default=1, ge=1, le=125)
    params: Dict[str, Any] = Field(default_factory=dict)

    @field_validator("symbol")
    @classmethod
    def normalize_symbol(cls, value: str) -> str:
        return value.strip().upper()


class OrderResponse(BaseModel):
    exchange: ExchangeName
    paper: bool
    order: Dict[str, Any]
    account: Optional["ExchangeBalance"] = None


class BalanceAsset(BaseModel):
    asset: str
    free: float = 0.0
    used: float = 0.0
    total: float = 0.0


class ExchangeBalance(BaseModel):
    exchange: ExchangeName
    paper: bool
    validated: bool
    assets: List[BalanceAsset] = Field(default_factory=list)
    message: str = ""


class OrderRecord(BaseModel):
    id: str
    exchange: ExchangeName
    symbol: str
    side: OrderSide
    type: OrderType
    amount: float
    entry_price: Optional[float] = None
    leverage: int = 1
    paper: bool = True
    status: str = "created"
    created_at: str
    raw_order: Dict[str, Any] = Field(default_factory=dict)


class PerformanceSummary(BaseModel):
    total_orders: int
    open_orders: int
    closed_orders: int
    notional: float
    margin_used: float
    unrealized_pnl: float
    unrealized_roe: float
    records: List[Dict[str, Any]] = Field(default_factory=list)


class TaskRecord(BaseModel):
    id: str
    status: str
    progress: int = Field(default=0, ge=0, le=100)
    exchange: ExchangeName
    symbol: str
    timeframe: str
    execute: bool = False
    use_llm_decision: bool = True
    amount: float = 0.0
    leverage: int = 1
    created_at: str
    updated_at: str
    result: Optional[Dict[str, Any]] = None
    result_data: Optional[Dict[str, Any]] = None
    llm_logs: List[Dict[str, Any]] = Field(default_factory=list)
    error: Optional[str] = None


class TaskLogRecord(BaseModel):
    id: int
    task_id: str
    level: str
    message: str
    created_at: str
    context: Dict[str, Any] = Field(default_factory=dict)


class TaskCenterResponse(BaseModel):
    tasks: List[TaskRecord] = Field(default_factory=list)
    logs: List[TaskLogRecord] = Field(default_factory=list)
    watched_orders: List[Dict[str, Any]] = Field(default_factory=list)


class TaskResponse(BaseModel):
    task_id: str
    status: str


class BotRunRequest(AnalyzeRequest):
    task_type: str = "crypto_bot_run"
    execute: bool = False
    transaction: bool = False
    use_llm_decision: bool = True
    amount: float = Field(default=0.0, ge=0.0, description="USDT margin amount for contract order placement.")
    leverage: int = Field(default=1, ge=1, le=125)


class TaskSubmitRequest(BotRunRequest):
    task_type: str = "crypto_bot_run"


class CryptoLLMDecision(BaseModel):
    approved: bool = Field(description="Whether the LLM agent approves taking the trade.")
    action: SignalAction = Field(description="Final action after LLM review: buy, sell, or hold.")
    confidence: float = Field(ge=0.0, le=1.0, description="LLM confidence in the final decision.")
    risk_level: LLMRiskLevel = Field(description="Risk level for the proposed trade.")
    recommended_leverage: int = Field(default=1, ge=1, le=125)
    entry: Optional[float] = None
    stop_loss: Optional[float] = None
    take_profit_1: Optional[float] = None
    take_profit_2: Optional[float] = None
    position_sizing: str = ""
    trader_rationale: str = ""
    portfolio_rationale: str = ""
    warnings: List[str] = Field(default_factory=list)


class LLMDecisionResponse(BaseModel):
    provider: str
    model: str
    eligible: bool
    decision: CryptoLLMDecision
    rule_payload: Dict[str, Any]
    task_conclusion: Dict[str, Any] = Field(default_factory=dict)
    prompt_messages: List[Dict[str, str]] = Field(default_factory=list)
