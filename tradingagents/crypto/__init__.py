"""Crypto trading platform extensions for TradingAgents."""

from .models import ExchangeName, OrderSide, OrderType, SignalAction
from tradingagents.crypto.strategys.strategy import analyze_trading_hub_setup

__all__ = [
    "ExchangeName",
    "OrderSide",
    "OrderType",
    "SignalAction",
    "analyze_trading_hub_setup",
]
