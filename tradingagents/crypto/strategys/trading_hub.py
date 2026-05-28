from __future__ import annotations

from statistics import mean
from typing import Iterable, List, Optional

from tradingagents.crypto.models import Candle, SignalAction, TradingHubAnalysis
from tradingagents.crypto.strategys.base import BaseStrategy, StrategyContext


class TradingHubStrategy(BaseStrategy):
    """Trading Hub 3.0 规则策略。

    该类只回答“当前行情是否满足策略条件”。它不检查交易开关，不读取账户，
    不判断订单金额，也不直接下单。订单执行由 TaskHandler/Service 层负责。
    """

    name = "trading_hub"

    def analyze(self, context: StrategyContext) -> TradingHubAnalysis:
        candles = context.candles
        if len(candles) < 60:
            return TradingHubAnalysis(
                action=SignalAction.HOLD,
                confidence=0.0,
                bias="unknown",
                setup="insufficient_data",
                reasons=["Need at least 60 candles for structure and liquidity analysis."],
            )

        closes = [c.close for c in candles]
        last = candles[-1]
        prev = candles[-2]
        range_high, range_low = _recent_range(candles, lookback=20)
        ema12 = _ema(closes, 12)
        ema50 = _ema(closes, 50)
        ema144 = _ema(closes, 144)
        ema169 = _ema(closes, 169)

        buy_side_sweep = last.high > range_high and last.close < range_high
        sell_side_sweep = last.low < range_low and last.close > range_low
        choch_up = last.close > prev.high
        choch_down = last.close < prev.low

        vegas_bias = "neutral"
        if ema144 is not None and ema169 is not None:
            if ema144 > ema169:
                vegas_bias = "bullish"
            elif ema144 < ema169:
                vegas_bias = "bearish"

        ema50_bias = "neutral"
        if ema50 is not None:
            ema50_bias = "bullish" if last.close >= ema50 else "bearish"

        metadata = {
            "range_high": range_high,
            "range_low": range_low,
            "ema12": ema12,
            "ema50": ema50,
            "ema144": ema144,
            "ema169": ema169,
            "buy_side_sweep": buy_side_sweep,
            "sell_side_sweep": sell_side_sweep,
            "choch_up": choch_up,
            "choch_down": choch_down,
            "vegas_bias": vegas_bias,
            "ema50_bias": ema50_bias,
        }

        if sell_side_sweep:
            return _bullish_sweep_analysis(last, range_high, range_low, ema12, vegas_bias, ema50_bias, choch_up, metadata)
        if buy_side_sweep:
            return _bearish_sweep_analysis(last, range_high, range_low, ema12, vegas_bias, ema50_bias, choch_down, metadata)

        return TradingHubAnalysis(
            action=SignalAction.HOLD,
            confidence=0.0,
            bias=vegas_bias,
            setup="no_liquidity_sweep",
            reasons=["No recent external liquidity sweep was detected; waiting for a cleaner setup."],
            invalidation=["Do not force entries without a sweep and LTF structure shift."],
            metadata=metadata,
        )


def _bullish_sweep_analysis(
    last: Candle,
    range_high: float,
    range_low: float,
    ema12: Optional[float],
    vegas_bias: str,
    ema50_bias: str,
    choch_up: bool,
    metadata: dict,
) -> TradingHubAnalysis:
    score = 2
    reasons: List[str] = ["Sell-side liquidity was swept and price closed back inside the range."]
    if choch_up:
        score += 2
        reasons.append("LTF structure shifted up by closing above the previous candle high.")
    if vegas_bias == "bullish":
        score += 1
        reasons.append("Vegas channel is bullish: EMA144 is above EMA169.")
    if ema50_bias == "bullish":
        score += 1
        reasons.append("Price is above EMA50, supporting bullish continuation.")
    if ema12 is not None and last.close > ema12:
        score += 1
        reasons.append("Price closed above EMA12 after the sweep.")

    entry = round((last.close + max(range_low, last.low)) / 2, 8)
    stop = round(last.low, 8)
    risk = max(entry - stop, 0)
    return TradingHubAnalysis(
        action=SignalAction.BUY if choch_up else SignalAction.HOLD,
        confidence=_confidence(score),
        bias="bullish",
        setup="sell_side_sweep_ltf_choch",
        entry=entry,
        stop_loss=stop,
        take_profit_1=round(entry + risk, 8) if risk else None,
        take_profit_2=round(range_high, 8),
        reasons=reasons,
        invalidation=["Bullish setup fails if price closes below the sweep low."],
        metadata=metadata,
    )


def _bearish_sweep_analysis(
    last: Candle,
    range_high: float,
    range_low: float,
    ema12: Optional[float],
    vegas_bias: str,
    ema50_bias: str,
    choch_down: bool,
    metadata: dict,
) -> TradingHubAnalysis:
    score = 2
    reasons: List[str] = ["Buy-side liquidity was swept and price closed back inside the range."]
    if choch_down:
        score += 2
        reasons.append("LTF structure shifted down by closing below the previous candle low.")
    if vegas_bias == "bearish":
        score += 1
        reasons.append("Vegas channel is bearish: EMA144 is below EMA169.")
    if ema50_bias == "bearish":
        score += 1
        reasons.append("Price is below EMA50, supporting bearish continuation.")
    if ema12 is not None and last.close < ema12:
        score += 1
        reasons.append("Price closed below EMA12 after the sweep.")

    entry = round((last.close + min(range_high, last.high)) / 2, 8)
    stop = round(last.high, 8)
    risk = max(stop - entry, 0)
    return TradingHubAnalysis(
        action=SignalAction.SELL if choch_down else SignalAction.HOLD,
        confidence=_confidence(score),
        bias="bearish",
        setup="buy_side_sweep_ltf_choch",
        entry=entry,
        stop_loss=stop,
        take_profit_1=round(entry - risk, 8) if risk else None,
        take_profit_2=round(range_low, 8),
        reasons=reasons,
        invalidation=["Bearish setup fails if price closes above the sweep high."],
        metadata=metadata,
    )


def _ema(values: Iterable[float], period: int) -> Optional[float]:
    data = list(values)
    if len(data) < period:
        return None
    alpha = 2 / (period + 1)
    ema = mean(data[:period])
    for value in data[period:]:
        ema = alpha * value + (1 - alpha) * ema
    return ema


def _recent_range(candles: List[Candle], lookback: int = 20) -> tuple[float, float]:
    window = candles[-lookback - 1:-1]
    return max(c.high for c in window), min(c.low for c in window)


def _confidence(score: int, max_score: int = 7) -> float:
    return round(min(score / max_score, 1.0), 2)
