from __future__ import annotations

from typing import Any

from tradingagents.crypto.models import SignalAction


def build_candidate_trade(analysis_payload: dict[str, Any], *, ignore_consumed: bool = False) -> dict[str, Any] | None:
    if analysis_payload.get("poi_consumed") and not ignore_consumed:
        return None
    analysis = analysis_payload.get("analysis") or {}
    metadata = analysis.get("metadata") or {}
    bos_direction = metadata.get("bos_direction")
    poi_low = first_number(metadata.get("poi_ob_low"))
    poi_high = first_number(metadata.get("poi_ob_high"))
    if bos_direction not in {"bearish", "bullish"} or poi_low is None or poi_high is None:
        return None

    if bos_direction == "bearish":
        action = SignalAction.SELL.value
        entry = poi_low
        stop_loss = poi_high
        take_profit = first_number(metadata.get("dynamic_structure_low"), metadata.get("bos_break_price"))
    else:
        action = SignalAction.BUY.value
        entry = poi_high
        stop_loss = poi_low
        take_profit = first_number(metadata.get("dynamic_structure_high"), metadata.get("bos_break_price"))

    if take_profit is None:
        return None

    return {
        "source": "bos_poi_ob",
        "action": action,
        "entry": entry,
        "stop_loss": stop_loss,
        "take_profit": take_profit,
        "poi_ob_low": poi_low,
        "poi_ob_high": poi_high,
        "poi_ob_fvg_low": metadata.get("poi_ob_fvg_low"),
        "poi_ob_fvg_high": metadata.get("poi_ob_fvg_high"),
        "bos_direction": bos_direction,
        "bos_break_price": metadata.get("bos_break_price"),
        "dynamic_structure_high": metadata.get("dynamic_structure_high"),
        "dynamic_structure_low": metadata.get("dynamic_structure_low"),
        "proposal_note": "BOS + OB/POI 已生成交易提案；是否清扫不再作为是否发送给 LLM 的门槛。",
    }


def first_number(*values) -> float | None:
    for value in values:
        if value not in (None, ""):
            return float(value)
    return None
