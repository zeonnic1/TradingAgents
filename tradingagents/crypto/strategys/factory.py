from __future__ import annotations

from tradingagents.crypto.strategys.base import BaseStrategy
from tradingagents.crypto.strategys.trading_hub import TradingHubStrategy


class StrategyFactory:
    """策略工厂。

    Factory Pattern 把“策略名称 -> 策略实现”的选择集中管理。
    Service 层不直接 import 具体策略类，减少策略扩展时对业务流程的影响。
    """

    _strategies: dict[str, type[BaseStrategy]] = {
        TradingHubStrategy.name: TradingHubStrategy,
    }

    @classmethod
    def create(cls, strategy_name: str = TradingHubStrategy.name) -> BaseStrategy:
        strategy_cls = cls._strategies.get(strategy_name)
        if strategy_cls is None:
            raise ValueError(f"Unsupported strategy: {strategy_name}")
        return strategy_cls()

    @classmethod
    def register(cls, strategy_cls: type[BaseStrategy]) -> None:
        cls._strategies[strategy_cls.name] = strategy_cls
