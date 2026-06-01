from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, List, Optional

import numpy as np
import pandas as pd

from tradingagents.crypto.models import Candle, SignalAction, TradingHubAnalysis
from tradingagents.crypto.strategys.base import BaseStrategy, StrategyContext


BOS_LOOKBACK_CANDLES = 1000


@dataclass(frozen=True)
class SwingPoint:
    index: int
    price: float
    kind: str


@dataclass(frozen=True)
class BosStructure:
    direction: str
    break_price: float
    break_index: int
    broken_swing_index: int
    pullback_index: int
    pullback_price: float
    dynamic_structure_high: Optional[float]
    dynamic_structure_low: Optional[float]
    dynamic_structure_high_index: Optional[int]
    dynamic_structure_low_index: Optional[int]
    poi_ob_low: Optional[float]
    poi_ob_high: Optional[float]
    poi_ob_index: Optional[int]
    poi_ob_fvg_low: Optional[float]
    poi_ob_fvg_high: Optional[float]
    poi_ob_fvg_index: Optional[int]


class TradingHubIndicatorMixin:
    """Trading Hub 指标能力。"""

    def ema(self, values: Iterable[float], period: int) -> Optional[float]:
        return _ema(values, period)

    def vegas_bias(self, ema144: Optional[float], ema169: Optional[float]) -> str:
        return _vegas_bias(ema144, ema169)


class TradingHubStructureMixin:
    """Trading Hub 结构识别能力。"""

    def latest_bos_structure(self, candles: List[Candle], preferred_direction: Optional[str] = None) -> Optional[BosStructure]:
        return _latest_bos_structure(candles, preferred_direction=preferred_direction)


class TradingHubAnalysisMixin:
    """Trading Hub 信号生成能力。"""

    def metadata(
        self,
        *,
        ema12: Optional[float],
        ema50: Optional[float],
        ema144: Optional[float],
        ema169: Optional[float],
        vegas_bias: str,
        daily_bias: str,
        ema50_bias: str,
        latest_bos: Optional[BosStructure],
        buy_side_sweep: bool,
        sell_side_sweep: bool,
        choch_up: bool,
        choch_down: bool,
        live_candle: Candle,
        signal_candle: Candle,
    ) -> dict:
        return _metadata(
            ema12=ema12,
            ema50=ema50,
            ema144=ema144,
            ema169=ema169,
            vegas_bias=vegas_bias,
            daily_bias=daily_bias,
            ema50_bias=ema50_bias,
            latest_bos=latest_bos,
            buy_side_sweep=buy_side_sweep,
            sell_side_sweep=sell_side_sweep,
            choch_up=choch_up,
            choch_down=choch_down,
            live_candle=live_candle,
            signal_candle=signal_candle,
        )

    def bullish_sweep_analysis(
        self,
        last: Candle,
        structure: BosStructure,
        ema12: Optional[float],
        vegas_bias: str,
        ema50_bias: str,
        choch_up: bool,
        metadata: dict,
    ) -> TradingHubAnalysis:
        return _bullish_sweep_analysis(last, structure, ema12, vegas_bias, ema50_bias, choch_up, metadata)

    def bearish_sweep_analysis(
        self,
        last: Candle,
        structure: BosStructure,
        ema12: Optional[float],
        vegas_bias: str,
        ema50_bias: str,
        choch_down: bool,
        metadata: dict,
    ) -> TradingHubAnalysis:
        return _bearish_sweep_analysis(last, structure, ema12, vegas_bias, ema50_bias, choch_down, metadata)


class TradingHubStrategy(TradingHubAnalysisMixin, TradingHubStructureMixin, TradingHubIndicatorMixin, BaseStrategy):
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

        live_candle = candles[-1]
        signal_candle = candles[-2]
        previous_closed = candles[-3]
        closed_closes = [c.close for c in candles[:-1]]
        ema12 = self.ema(closed_closes, 12)
        ema50 = self.ema(closed_closes, 50)
        ema144 = self.ema(closed_closes, 144)
        ema169 = self.ema(closed_closes, 169)
        vegas_bias = self.vegas_bias(ema144, ema169)
        daily_bias = _daily_bias(context.daily_candles, fallback=vegas_bias)
        ema50_bias = "bullish" if ema50 is not None and signal_candle.close >= ema50 else "bearish" if ema50 is not None else "neutral"
        latest_bos = self.latest_bos_structure(candles, preferred_direction=_preferred_bos_direction(daily_bias))
        #FIXME 这里判断收盘逻辑不应该是chock,chock代表结构反转，这里只是收盘
        choch_up = signal_candle.close > previous_closed.high
        choch_down = signal_candle.close < previous_closed.low
        buy_side_sweep = bool(
            latest_bos
            and latest_bos.direction == "bearish"
            and latest_bos.dynamic_structure_high is not None
            and signal_candle.high > latest_bos.dynamic_structure_high
            and signal_candle.close < latest_bos.dynamic_structure_high
        )
        sell_side_sweep = bool(
            latest_bos
            and latest_bos.direction == "bullish"
            and latest_bos.dynamic_structure_low is not None
            and signal_candle.low < latest_bos.dynamic_structure_low
            and signal_candle.close > latest_bos.dynamic_structure_low
        )

        metadata = self.metadata(
            ema12=ema12,
            ema50=ema50,
            ema144=ema144,
            ema169=ema169,
            vegas_bias=vegas_bias,
            daily_bias=daily_bias,
            ema50_bias=ema50_bias,
            latest_bos=latest_bos,
            buy_side_sweep=buy_side_sweep,
            sell_side_sweep=sell_side_sweep,
            choch_up=choch_up,
            choch_down=choch_down,
            live_candle=live_candle,
            signal_candle=signal_candle,
        )

        if sell_side_sweep and latest_bos is not None:
            return self.bullish_sweep_analysis(signal_candle, latest_bos, ema12, vegas_bias, ema50_bias, choch_up, metadata)
        if buy_side_sweep and latest_bos is not None:
            return self.bearish_sweep_analysis(signal_candle, latest_bos, ema12, vegas_bias, ema50_bias, choch_down, metadata)

        if latest_bos is None:
            reason = "No valid BOS was detected; waiting for effective pullback and structural break."
            setup = "no_valid_bos"
            bias = daily_bias
        else:
            reason = (
                "Waiting for price to sweep the BOS-derived dynamic structure "
                f"{'high' if latest_bos.direction == 'bearish' else 'low'}."
            )
            setup = "waiting_dynamic_liquidity_sweep"
            bias = latest_bos.direction

        return TradingHubAnalysis(
            action=SignalAction.HOLD,
            confidence=0.0,
            bias=bias,
            setup=setup,
            reasons=[reason],
            invalidation=["Do not force entries before BOS-derived liquidity is swept."],
            metadata=metadata,
        )


def _bullish_sweep_analysis(
    last: Candle,
    structure: BosStructure,
    ema12: Optional[float],
    vegas_bias: str,
    ema50_bias: str,
    choch_up: bool,
    metadata: dict,
) -> TradingHubAnalysis:
    score = 2
    reasons: List[str] = ["Sell-side liquidity from the latest bullish BOS structure was swept."]
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

    dynamic_low = float(structure.dynamic_structure_low or last.low)
    entry = round((last.close + max(dynamic_low, last.low)) / 2, 8)
    stop = round(min(last.low, dynamic_low), 8)
    risk = max(entry - stop, 0)
    target_high = metadata.get("dynamic_structure_high") or metadata.get("bos_break_price") or entry
    return TradingHubAnalysis(
        action=SignalAction.BUY if choch_up else SignalAction.HOLD,
        confidence=_confidence(score),
        bias="bullish",
        setup="sell_side_sweep_ltf_choch",
        entry=entry,
        stop_loss=stop,
        take_profit_1=round(entry + risk, 8) if risk else None,
        take_profit_2=round(float(target_high), 8),
        reasons=reasons,
        invalidation=["Bullish setup fails if price closes below the swept dynamic structure low."],
        metadata=metadata,
    )


def _bearish_sweep_analysis(
    last: Candle,
    structure: BosStructure,
    ema12: Optional[float],
    vegas_bias: str,
    ema50_bias: str,
    choch_down: bool,
    metadata: dict,
) -> TradingHubAnalysis:
    score = 2
    reasons: List[str] = ["Buy-side liquidity from the latest bearish BOS structure was swept."]
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

    dynamic_high = float(structure.dynamic_structure_high or last.high)
    entry = round((last.close + min(dynamic_high, last.high)) / 2, 8)
    stop = round(max(last.high, dynamic_high), 8)
    risk = max(stop - entry, 0)
    target_low = metadata.get("dynamic_structure_low") or metadata.get("bos_break_price") or entry
    return TradingHubAnalysis(
        action=SignalAction.SELL if choch_down else SignalAction.HOLD,
        confidence=_confidence(score),
        bias="bearish",
        setup="buy_side_sweep_ltf_choch",
        entry=entry,
        stop_loss=stop,
        take_profit_1=round(entry - risk, 8) if risk else None,
        take_profit_2=round(float(target_low), 8),
        reasons=reasons,
        invalidation=["Bearish setup fails if price closes above the swept dynamic structure high."],
        metadata=metadata,
    )


def _latest_bos_structure(candles: List[Candle], preferred_direction: Optional[str] = None) -> Optional[BosStructure]:
    swings = _swing_points(candles[:-1])
    min_index = max(0, len(candles) - BOS_LOOKBACK_CANDLES)
    bos_candidates: List[BosStructure] = []
    swing_lows = [point for point in swings if point.kind == "low" and point.index >= min_index]
    swing_highs = [point for point in swings if point.kind == "high" and point.index >= min_index]

    for swing_low in swing_lows:
        for pullback in _swings_after(swing_highs, swing_low.index):
            if not _is_effective_pullback(candles, "bearish", swing_low, pullback):
                continue
            lower_low = _current_break_swing_after(swing_lows, start=pullback.index + 1, price=swing_low.price, direction="bearish")
            if lower_low is None or lower_low.index >= len(candles) - 2:
                continue
            bos_candidates.append(_build_bos_structure(candles, "bearish", swing_low, pullback, lower_low.index))
            break

    for swing_high in swing_highs:
        for pullback in _swings_after(swing_lows, swing_high.index):
            if not _is_effective_pullback(candles, "bullish", swing_high, pullback):
                continue
            higher_high = _current_break_swing_after(swing_highs, start=pullback.index + 1, price=swing_high.price, direction="bullish")
            if higher_high is None or higher_high.index >= len(candles) - 2:
                continue
            bos_candidates.append(_build_bos_structure(candles, "bullish", swing_high, pullback, higher_high.index))
            break

    if preferred_direction in {"bearish", "bullish"}:
        bos_candidates = [candidate for candidate in bos_candidates if candidate.direction == preferred_direction]
    if not bos_candidates:
        return None
    bos_candidates = _dedupe_equal_bos_candidates(bos_candidates)
    return max(bos_candidates, key=_bos_priority)


def _dedupe_equal_bos_candidates(candidates: List[BosStructure]) -> List[BosStructure]:
    grouped: dict[tuple[str, int, float], BosStructure] = {}
    for candidate in candidates:
        key = (candidate.direction, candidate.break_index, round(float(candidate.break_price), 8))
        current = grouped.get(key)
        if current is None or candidate.broken_swing_index > current.broken_swing_index:
            grouped[key] = candidate
    return list(grouped.values())


def _bos_priority(structure: BosStructure) -> tuple[int, float, int]:
    span = structure.break_index - structure.broken_swing_index
    if structure.direction == "bearish":
        distance = abs(float(structure.break_price) - float(structure.dynamic_structure_low or structure.break_price))
    else:
        distance = abs(float(structure.dynamic_structure_high or structure.break_price) - float(structure.break_price))
    return (span, distance, structure.break_index)


def _build_bos_structure(
    candles: List[Candle],
    direction: str,
    broken_swing: SwingPoint,
    pullback: SwingPoint,
    break_index: int,
) -> BosStructure:
    # BOS 的待清扫结构点来自“被突破结构点 -> 新 HH/LL”这一段。
    # bearish: 被跌破低点到更低低点之间的最高 high 是后续等待清扫的 buy-side liquidity。
    # bullish: 被突破高点到更高高点之间的最低 low 是后续等待清扫的 sell-side liquidity。
    segment = candles[broken_swing.index:break_index + 1]
    high_point = _highest_point(segment, offset=broken_swing.index)
    low_point = _lowest_point(segment, offset=broken_swing.index)
    poi_ob = _find_poi_ob(candles, direction, broken_swing.index, high_point, low_point)
    return BosStructure(
        direction=direction,
        break_price=broken_swing.price,
        break_index=break_index,
        broken_swing_index=broken_swing.index,
        pullback_index=pullback.index,
        pullback_price=pullback.price,
        dynamic_structure_high=high_point.price if high_point else None,
        dynamic_structure_low=low_point.price if low_point else None,
        dynamic_structure_high_index=high_point.index if high_point else None,
        dynamic_structure_low_index=low_point.index if low_point else None,
        poi_ob_low=poi_ob["low"] if poi_ob else None,
        poi_ob_high=poi_ob["high"] if poi_ob else None,
        poi_ob_index=poi_ob["index"] if poi_ob else None,
        poi_ob_fvg_low=poi_ob["fvg_low"] if poi_ob else None,
        poi_ob_fvg_high=poi_ob["fvg_high"] if poi_ob else None,
        poi_ob_fvg_index=poi_ob["fvg_index"] if poi_ob else None,
    )


def _swing_points(candles: List[Candle], left: int = 2, right: int = 2) -> List[SwingPoint]:
    points: List[SwingPoint] = []
    frame = _candles_frame(candles)
    if frame.empty:
        return points
    highs = frame["high"].to_numpy(dtype=float)
    lows = frame["low"].to_numpy(dtype=float)
    for index in range(left, len(candles) - right):
        current_high = highs[index]
        current_low = lows[index]
        window_highs = highs[index - left:index + right + 1]
        window_lows = lows[index - left:index + right + 1]
        left_highs = highs[index - left:index]
        left_lows = lows[index - left:index]
        right_highs = highs[index + 1:index + right + 1]
        right_lows = lows[index + 1:index + right + 1]
        if current_high == np.max(window_highs) and bool(np.all(current_high > left_highs)) and bool(np.all(current_high > right_highs)):
            points.append(SwingPoint(index=index, price=float(current_high), kind="high"))
        if current_low == np.min(window_lows) and bool(np.all(current_low < left_lows)) and bool(np.all(current_low < right_lows)):
            points.append(SwingPoint(index=index, price=float(current_low), kind="low"))
    return sorted(points, key=lambda point: point.index)


def _first_swing_after(points: List[SwingPoint], index: int) -> Optional[SwingPoint]:
    for point in points:
        if point.index > index:
            return point
    return None


def _swings_after(points: List[SwingPoint], index: int) -> List[SwingPoint]:
    return [point for point in points if point.index > index]


def _current_break_swing_after(points: List[SwingPoint], start: int, price: float, direction: str) -> Optional[SwingPoint]:
    broken_points = [
        point
        for point in points
        if point.index >= start
        and (
            (direction == "bearish" and point.price < price)
            or (direction == "bullish" and point.price > price)
        )
    ]
    if not broken_points:
        return None
    if direction == "bearish":
        current = min(broken_points, key=lambda point: (point.price, -point.index))
        first_break = min(broken_points, key=lambda point: point.index)
        return current if current.index == first_break.index else None
    if direction == "bullish":
        current = max(broken_points, key=lambda point: (point.price, point.index))
        first_break = min(broken_points, key=lambda point: point.index)
        return current if current.index == first_break.index else None
    return None


def _is_effective_pullback(candles: List[Candle], bos_direction: str, broken_swing: SwingPoint, pullback: SwingPoint) -> bool:
    if pullback.index <= broken_swing.index + 1:
        return False
    if _structure_already_broken_before_pullback(candles, bos_direction, broken_swing, pullback):
        return False

    segment = candles[broken_swing.index:pullback.index + 1]
    if len(segment) < 3:
        return False

    threshold = _minimum_pullback_distance(candles, broken_swing.index, pullback.index)
    if bos_direction == "bearish":
        distance = pullback.price - broken_swing.price
        return distance >= threshold and _breaks_internal_structure_up(candles, broken_swing.index + 1, pullback.index)
    if bos_direction == "bullish":
        distance = broken_swing.price - pullback.price
        return distance >= threshold and _breaks_internal_structure_down(candles, broken_swing.index + 1, pullback.index)
    return False


def _structure_already_broken_before_pullback(
    candles: List[Candle],
    bos_direction: str,
    broken_swing: SwingPoint,
    pullback: SwingPoint,
) -> bool:
    between = candles[broken_swing.index + 1:pullback.index]
    if not between:
        return False
    if bos_direction == "bearish":
        return bool(np.any(_candle_values(between, "low") < float(broken_swing.price)))
    if bos_direction == "bullish":
        return bool(np.any(_candle_values(between, "high") > float(broken_swing.price)))
    return False


def _minimum_pullback_distance(candles: List[Candle], start: int, end: int) -> float:
    left = max(1, start - 10)
    right = min(len(candles), end + 1)
    frame = _candles_frame(candles[left:right])
    ranges = (frame["high"] - frame["low"]).abs().to_numpy(dtype=float) if not frame.empty else np.array([], dtype=float)
    avg_range = float(np.mean(ranges)) if ranges.size else 0.0
    price = max(abs(candles[start].close), 1.0)
    return float(max(avg_range * 0.5, price * 0.001))


def _breaks_internal_structure_up(candles: List[Candle], start: int, end: int) -> bool:
    begin = max(start, 1)
    finish = min(end, len(candles) - 1)
    if begin > finish:
        return False
    highs = _candle_values(candles, "high")
    return bool(np.any(highs[begin:finish + 1] > highs[begin - 1:finish]))


def _breaks_internal_structure_down(candles: List[Candle], start: int, end: int) -> bool:
    begin = max(start, 1)
    finish = min(end, len(candles) - 1)
    if begin > finish:
        return False
    lows = _candle_values(candles, "low")
    return bool(np.any(lows[begin:finish + 1] < lows[begin - 1:finish]))


def _first_low_break_below(candles: List[Candle], start: int, price: float) -> Optional[int]:
    offset = max(start, 0)
    lows = _candle_values(candles, "low")
    matches = np.flatnonzero(lows[offset:] < float(price))
    return int(offset + matches[0]) if matches.size else None


def _first_high_break_above(candles: List[Candle], start: int, price: float) -> Optional[int]:
    offset = max(start, 0)
    highs = _candle_values(candles, "high")
    matches = np.flatnonzero(highs[offset:] > float(price))
    return int(offset + matches[0]) if matches.size else None


def _highest_point(candles: List[Candle], offset: int) -> Optional[SwingPoint]:
    if not candles:
        return None
    frame = _candles_frame(candles)
    local_index = int(frame["high"].idxmax())
    return SwingPoint(index=offset + local_index, price=float(frame.at[local_index, "high"]), kind="high")


def _lowest_point(candles: List[Candle], offset: int) -> Optional[SwingPoint]:
    if not candles:
        return None
    frame = _candles_frame(candles)
    local_index = int(frame["low"].idxmin())
    return SwingPoint(index=offset + local_index, price=float(frame.at[local_index, "low"]), kind="low")


def _find_poi_ob(
    candles: List[Candle],
    direction: str,
    broken_swing_index: int,
    dynamic_high: Optional[SwingPoint],
    dynamic_low: Optional[SwingPoint],
) -> Optional[dict]:
    if direction == "bearish" and dynamic_high is not None:
        for index in range(broken_swing_index - 1, -1, -1):
            if candles[index].close <= dynamic_high.price:
                continue
            if dynamic_high.price > candles[index].low:
                continue
            if _ob_contained_by_previous_candles(candles, index):
                continue
            fvg = _bearish_fvg_after(candles, index)
            if fvg is None:
                continue
            return {
                "index": index,
                "low": candles[index].low,
                "high": candles[index].high,
                "fvg_low": fvg[0],
                "fvg_high": fvg[1],
                "fvg_index": index + 1,
            }
    if direction == "bullish" and dynamic_low is not None:
        for index in range(broken_swing_index - 1, -1, -1):
            if candles[index].close >= dynamic_low.price:
                continue
            if _ob_contained_by_previous_candles(candles, index):
                continue
            fvg = _bullish_fvg_after(candles, index)
            if fvg is None:
                continue
            return {
                "index": index,
                "low": candles[index].low,
                "high": candles[index].high,
                "fvg_low": fvg[0],
                "fvg_high": fvg[1],
                "fvg_index": index + 1,
            }
    return None


def _ob_contained_by_previous_candles(candles: List[Candle], index: int, lookback: int = 3) -> bool:
    if index <= 0:
        return False
    ob_low = candles[index].low
    ob_high = candles[index].high
    start = max(0, index - lookback)
    for previous in candles[start:index]:
        if previous.low <= ob_low and previous.high >= ob_high:
            return True
    return False


def _bearish_fvg_after(candles: List[Candle], index: int) -> Optional[tuple[float, float]]:
    if index + 2 >= len(candles):
        return None
    left = candles[index]
    right = candles[index + 2]
    if left.low > right.high:
        return (right.high, left.low)
    return None


def _bullish_fvg_after(candles: List[Candle], index: int) -> Optional[tuple[float, float]]:
    if index + 2 >= len(candles):
        return None
    left = candles[index]
    right = candles[index + 2]
    if left.high < right.low:
        return (left.high, right.low)
    return None


def _preferred_bos_direction(daily_bias: str) -> Optional[str]:
    if daily_bias in {"bearish", "bullish"}:
        return daily_bias
    return None


def _daily_bias(daily_candles: Optional[List[Candle]], fallback: str = "neutral") -> str:
    if not daily_candles or len(daily_candles) < 4:
        return fallback
    # 交易所 OHLCV 最后一根 1d 通常是当天未收盘 K 线，不能用它确认日线偏见。
    closed = daily_candles[:-1]
    latest = closed[-1]
    previous = closed[-2]
    if latest.close < latest.open:
        return "bearish"
    if latest.close > latest.open:
        return "bullish"
    if latest.high > previous.high and latest.close < previous.high:
        return "bearish"
    if latest.low < previous.low and latest.close > previous.low:
        return "bullish"
    if latest.close < previous.low:
        return "bearish"
    if latest.close > previous.high:
        return "bullish"
    return fallback


def _candles_frame(candles: List[Candle]) -> pd.DataFrame:
    return pd.DataFrame.from_records(
        (
            {
                "timestamp": candle.timestamp,
                "open": candle.open,
                "high": candle.high,
                "low": candle.low,
                "close": candle.close,
                "volume": candle.volume,
            }
            for candle in candles
        ),
        columns=["timestamp", "open", "high", "low", "close", "volume"],
    )


def _candle_values(candles: List[Candle], field: str) -> np.ndarray:
    frame = _candles_frame(candles)
    if frame.empty:
        return np.array([], dtype=float)
    return frame[field].to_numpy(dtype=float)


def _metadata(
    *,
    ema12: Optional[float],
    ema50: Optional[float],
    ema144: Optional[float],
    ema169: Optional[float],
    vegas_bias: str,
    daily_bias: str,
    ema50_bias: str,
    latest_bos: Optional[BosStructure],
    buy_side_sweep: bool,
    sell_side_sweep: bool,
    choch_up: bool,
    choch_down: bool,
    live_candle: Candle,
    signal_candle: Candle,
) -> dict:
    data = {
        "htf_timeframe": None,
        "ltf_timeframe": None,
        "daily_bias": daily_bias,
        "ema12": ema12,
        "ema50": ema50,
        "ema144": ema144,
        "ema169": ema169,
        "vegas_bias": vegas_bias,
        "ema50_bias": ema50_bias,
        "buy_side_sweep": buy_side_sweep,
        "sell_side_sweep": sell_side_sweep,
        "liquidity_swept": buy_side_sweep or sell_side_sweep,
        "current_price": live_candle.close,
        "signal_candle_timestamp": signal_candle.timestamp,
        "signal_candle_close": signal_candle.close,
        "choch_up": choch_up,
        "choch_down": choch_down,
        "choch_confirmed": choch_up or choch_down,
        "idm_taken": None,
        "poi_type": "dynamic_bos_structure",
        "poi_range": None,
        "pd_zone": None,
        "fvg_exists": None,
        "setup_quality": "pending",
    }
    if latest_bos is None:
        data.update({
            "bos_direction": None,
            "bos_break_price": None,
            "bos_break_index": None,
            "bos_broken_swing_index": None,
            "bos_pullback_index": None,
            "bos_pullback_price": None,
            "dynamic_structure_high": None,
            "dynamic_structure_low": None,
            "dynamic_structure_high_index": None,
            "dynamic_structure_low_index": None,
            "poi_ob_low": None,
            "poi_ob_high": None,
            "poi_ob_index": None,
            "poi_ob_fvg_low": None,
            "poi_ob_fvg_high": None,
            "poi_ob_fvg_index": None,
            "range_high": None,
            "range_low": None,
        })
        return data

    data.update({
        "bos_direction": latest_bos.direction,
        "bos_break_price": latest_bos.break_price,
        "bos_break_index": latest_bos.break_index,
        "bos_broken_swing_index": latest_bos.broken_swing_index,
        "bos_pullback_index": latest_bos.pullback_index,
        "bos_pullback_price": latest_bos.pullback_price,
        "dynamic_structure_high": latest_bos.dynamic_structure_high,
        "dynamic_structure_low": latest_bos.dynamic_structure_low,
        "dynamic_structure_high_index": latest_bos.dynamic_structure_high_index,
        "dynamic_structure_low_index": latest_bos.dynamic_structure_low_index,
        "poi_ob_low": latest_bos.poi_ob_low,
        "poi_ob_high": latest_bos.poi_ob_high,
        "poi_ob_index": latest_bos.poi_ob_index,
        "poi_ob_fvg_low": latest_bos.poi_ob_fvg_low,
        "poi_ob_fvg_high": latest_bos.poi_ob_fvg_high,
        "poi_ob_fvg_index": latest_bos.poi_ob_fvg_index,
        "range_high": latest_bos.dynamic_structure_high,
        "range_low": latest_bos.dynamic_structure_low,
    })
    if latest_bos.poi_ob_low is not None and latest_bos.poi_ob_high is not None:
        data["poi_type"] = "ob_with_fvg"
        data["poi_range"] = [latest_bos.poi_ob_low, latest_bos.poi_ob_high]
        data["fvg_exists"] = True
    if latest_bos.dynamic_structure_high is not None and latest_bos.dynamic_structure_low is not None:
        data["bos_range"] = [latest_bos.dynamic_structure_low, latest_bos.dynamic_structure_high]
    return data


def _vegas_bias(ema144: Optional[float], ema169: Optional[float]) -> str:
    if ema144 is None or ema169 is None:
        return "neutral"
    if ema144 > ema169:
        return "bullish"
    if ema144 < ema169:
        return "bearish"
    return "neutral"


def _ema(values: Iterable[float], period: int) -> Optional[float]:
    series = pd.Series(list(values), dtype="float64").dropna()
    if len(series) < period:
        return None
    alpha = 2 / (period + 1)
    ema = float(series.iloc[:period].mean())
    for value in series.iloc[period:].to_numpy(dtype=float):
        ema = alpha * value + (1 - alpha) * ema
    return float(ema)


def _confidence(score: int, max_score: int = 7) -> float:
    return round(min(score / max_score, 1.0), 2)
