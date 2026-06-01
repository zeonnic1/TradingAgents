from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import List, Optional

from tradingagents.crypto.models import Candle, TradingHubAnalysis


@dataclass(frozen=True)
class StrategyContext:
    """策略执行上下文。

    这里只放策略判断需要的数据。交易开关、订单金额、账户余额等执行层信息
    不进入策略上下文，避免策略承担下单职责。
    """

    candles: List[Candle]
    daily_candles: Optional[List[Candle]] = None


class BaseStrategy(ABC):
    """策略基类。

    生产代码中所有策略都实现同一个 analyze 接口，任务层只依赖抽象能力。
    后续增加 Vegas、纯流动性、趋势跟随等策略时，注册到 StrategyFactory 即可。
    """

    name: str

    @abstractmethod
    def analyze(self, context: StrategyContext) -> TradingHubAnalysis:
        raise NotImplementedError
