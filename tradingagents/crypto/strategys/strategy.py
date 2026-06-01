from __future__ import annotations

from typing import List, Optional

from tradingagents.crypto.models import Candle, TradingHubAnalysis
from tradingagents.crypto.strategys.base import StrategyContext
from tradingagents.crypto.strategys.factory import StrategyFactory
from tradingagents.crypto.strategys.trading_hub import TradingHubStrategy


def analyze_trading_hub_setup(candles: List[Candle], daily_candles: Optional[List[Candle]] = None) -> TradingHubAnalysis:
    """兼容旧入口：使用策略工厂执行 Trading Hub 策略。"""

    strategy = StrategyFactory.create(TradingHubStrategy.name)
    return strategy.analyze(StrategyContext(candles=candles, daily_candles=daily_candles))
