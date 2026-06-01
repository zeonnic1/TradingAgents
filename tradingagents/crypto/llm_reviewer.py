from __future__ import annotations

import json
from typing import Any

from pydantic import ValidationError

from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.llm_clients import create_llm_client

from .models import CryptoLLMDecision, LLMDecisionResponse, LLMRiskLevel, SignalAction


def create_rule_payload(analysis_payload: dict[str, Any]) -> dict[str, Any]:
    snapshot = analysis_payload.get("snapshot") or {}
    candles = snapshot.get("candles") or []
    return {
        "exchange": snapshot.get("exchange"),
        "symbol": snapshot.get("symbol"),
        "timeframe": snapshot.get("timeframe"),
        "analysis": analysis_payload.get("analysis") or {},
        "candidate_trade": analysis_payload.get("candidate_trade"),
        "poi_consumed": analysis_payload.get("poi_consumed"),
        "manual_decision_context": analysis_payload.get("manual_decision_context"),
        "account": analysis_payload.get("account"),
        "order": analysis_payload.get("order"),
        "candle_summary": _candle_summary(candles),
    }


def is_llm_decision_eligible(analysis_payload: dict[str, Any]) -> bool:
    analysis = analysis_payload.get("analysis") or {}
    candidate = analysis_payload.get("candidate_trade") or {}
    return (
        analysis.get("action") in {SignalAction.BUY.value, SignalAction.SELL.value}
        or candidate.get("action") in {SignalAction.BUY.value, SignalAction.SELL.value}
    )


def review_with_llm(analysis_payload: dict[str, Any], config: dict | None = None) -> LLMDecisionResponse:
    config = config or DEFAULT_CONFIG
    provider = config["llm_provider"]
    model = config["deep_think_llm"]
    llm = create_llm_client(
        provider=provider,
        model=model,
        base_url=config.get("backend_url"),
    ).get_llm()
    rule_payload = create_rule_payload(analysis_payload)
    task_conclusion = create_task_conclusion(analysis_payload)
    prompt = _build_prompt(rule_payload, task_conclusion)
    structured_llm = _bind_structured(llm, CryptoLLMDecision)

    if structured_llm is not None:
        try:
            decision = structured_llm.invoke(prompt)
            return LLMDecisionResponse(
                provider=provider,
                model=model,
                eligible=is_llm_decision_eligible(analysis_payload),
                decision=decision,
                rule_payload=rule_payload,
                task_conclusion=task_conclusion,
                prompt_messages=prompt,
            )
        except Exception:
            pass

    response = llm.invoke(prompt)
    decision = _parse_free_text_decision(response.content)
    return LLMDecisionResponse(
        provider=provider,
        model=model,
        eligible=is_llm_decision_eligible(analysis_payload),
        decision=decision,
        rule_payload=rule_payload,
        task_conclusion=task_conclusion,
        prompt_messages=prompt,
    )


def compact_llm_decision_payload(payload: dict[str, Any] | LLMDecisionResponse) -> dict[str, Any]:
    data = payload.model_dump(mode="json") if isinstance(payload, LLMDecisionResponse) else dict(payload)
    return {
        "provider": data.get("provider"),
        "model": data.get("model"),
        "eligible": data.get("eligible"),
        "decision": data.get("decision"),
        "task_conclusion": data.get("task_conclusion"),
    }


def lifecycle_llm_log(stage: str, context: dict[str, Any], config: dict | None = None) -> str:
    """任务生命周期中的轻量 LLM 钩子。

    PENDING：解析任务意图；ORDERED：生成处理中间方案；COMPLETED：总结执行结果。
    该函数只返回日志文本，不直接改变交易状态。
    """

    config = config or DEFAULT_CONFIG
    provider = config["llm_provider"]
    model = config["quick_think_llm"]
    llm = create_llm_client(
        provider=provider,
        model=model,
        base_url=config.get("backend_url"),
    ).get_llm()
    prompt = [
        {
            "role": "system",
            "content": (
                "You are a concise crypto task lifecycle assistant. Return a short operational note. "
                "Do not make unsupported trading claims."
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "stage": stage,
                    "instruction": _lifecycle_instruction(stage),
                    "context": context,
                },
                ensure_ascii=False,
                indent=2,
            ),
        },
    ]
    return llm.invoke(prompt).content


def fallback_hold_decision(reason: str) -> CryptoLLMDecision:
    return CryptoLLMDecision(
        approved=False,
        action=SignalAction.HOLD,
        confidence=0.0,
        risk_level=LLMRiskLevel.HIGH,
        recommended_leverage=1,
        position_sizing="No position.",
        trader_rationale=reason,
        portfolio_rationale="Portfolio manager blocks execution until a valid LLM review is available.",
        warnings=[reason],
    )


def create_task_conclusion(analysis_payload: dict[str, Any]) -> dict[str, Any]:
    analysis = analysis_payload.get("analysis") or {}
    metadata = analysis.get("metadata") or {}
    candidate = analysis_payload.get("candidate_trade") or {}
    snapshot = analysis_payload.get("snapshot") or {}
    candles = snapshot.get("candles") or []
    latest = candles[-1] if candles else {}
    current_price = latest.get("close") or metadata.get("current_price")
    poi_low = metadata.get("poi_ob_low")
    poi_high = metadata.get("poi_ob_high")
    poi_touched = _price_in_range(current_price, poi_low, poi_high)
    return {
        "symbol": snapshot.get("symbol"),
        "timeframe": snapshot.get("timeframe"),
        "current_price": current_price,
        "task_action": analysis.get("action"),
        "task_setup": analysis.get("setup"),
        "task_bias": analysis.get("bias"),
        "task_confidence": analysis.get("confidence"),
        "task_reasons": analysis.get("reasons") or [],
        "liquidity_swept_confirmed": metadata.get("liquidity_swept"),
        "buy_side_sweep": metadata.get("buy_side_sweep"),
        "sell_side_sweep": metadata.get("sell_side_sweep"),
        "signal_candle_timestamp": metadata.get("signal_candle_timestamp"),
        "signal_candle_close": metadata.get("signal_candle_close"),
        "sweep_confirmation_note": (
            "流动性清扫按已收盘信号K线确认，不按最新未收盘价格直接确认。"
        ),
        "bos_direction": metadata.get("bos_direction"),
        "broken_structure_price": metadata.get("bos_break_price"),
        "dynamic_structure_high": metadata.get("dynamic_structure_high"),
        "dynamic_structure_low": metadata.get("dynamic_structure_low"),
        "poi_type": metadata.get("poi_type"),
        "poi_ob_low": metadata.get("poi_ob_low"),
        "poi_ob_high": metadata.get("poi_ob_high"),
        "poi_ob_fvg_low": metadata.get("poi_ob_fvg_low"),
        "poi_ob_fvg_high": metadata.get("poi_ob_fvg_high"),
        "poi_touched_by_current_price": poi_touched,
        "poi_touch_note": (
            "当前价格已进入 POI 区域；这不同于 dynamic liquidity sweep 收盘确认。"
            if poi_touched
            else "当前价格未进入已识别 POI 区域，或当前没有有效 POI。"
        ),
        "trade_proposal_active": bool(candidate),
        "trade_proposal_source": candidate.get("source") if candidate else None,
        "trade_proposal_instruction": (
            "candidate_trade 已由 BOS + OB/POI 生成，应作为交易提案评估；"
            "liquidity_swept 表示动态结构清扫收盘确认，POI 触达由 poi_touched_by_current_price 单独表达。"
            if candidate
            else "当前没有交易提案，不能强行构造入场。"
        ),
        "candidate_trade": candidate or None,
        "poi_consumed": analysis_payload.get("poi_consumed"),
        "manual_decision_context": analysis_payload.get("manual_decision_context"),
        "external_context": analysis_payload.get("external_context") or {
            "news": "当前请求未提供新闻数据；不要编造新闻结论。",
            "whale_activity": "当前请求未提供鲸鱼活动数据；不要编造链上大额转账结论。",
            "exchange_flow": "当前请求未提供交易所流入/流出数据；不要编造资金流结论。",
        },
    }


def _build_prompt(rule_payload: dict[str, Any], task_conclusion: dict[str, Any]) -> list[dict[str, str]]:
    payload_json = json.dumps(rule_payload, ensure_ascii=False, indent=2)
    conclusion_json = json.dumps(task_conclusion, ensure_ascii=False, indent=2)
    return [
        {
            "role": "system",
            "content": (
                "You are a crypto trading decision agent composed of two roles: Trader and Portfolio Manager. "
                "The Python strategy tool has already produced deterministic market features and a candidate signal. "
                "Your job is to review the JSON, combine Trading Hub 3.0 structure logic, POI/OB context, "
                "available news, whale activity, and exchange inflow/outflow context, then return a strict "
                "structured decision. Do not invent market data. If news/whale/flow data is missing, explicitly "
                "say it is not provided and base the recommendation on the provided structure/POI data. Do not approve "
                "a trade when both the strategy action and candidate_trade are missing and no POI proposal exists, stop loss is missing, "
                "or risk is unclear. When candidate_trade is present, it is the active trade proposal generated "
                "from BOS + OB/POI; evaluate that proposal directly. Treat liquidity_swept as the closed-candle "
                "dynamic-structure sweep confirmation field, and treat poi_touched_by_current_price as the POI touch field. "
                "Do not confuse those two facts when explaining the recommendation. "
                "When manual_decision_context is present, the user has "
                "manually sent a completed BOS/POI task to the decision layer for immediate limit-order placement. "
                "Evaluate candidate_trade as the proposed order and judge only risk, levels, leverage, and approval. "
                "The liquidity-sweep waiting gate is intentionally disabled in this manual path; do not require "
                "waiting_dynamic_liquidity_sweep to be satisfied before approving. Respond in Chinese for all "
                "free-text fields, including trader_rationale, portfolio_rationale, position_sizing, and warnings."
            ),
        },
        {
            "role": "user",
            "content": (
                "First review this task processing conclusion. Treat it as the worker's current conclusion, "
                "then inspect the full JSON payload for supporting details.\n\n"
                f"TASK_PROCESSING_CONCLUSION:\n{conclusion_json}\n\n"
                "FULL_RULE_PAYLOAD:\n"
                "Review this Trading Hub crypto rule payload. The Trader should judge entry quality and levels. "
                "The Trader should also explain how the POI relates to Trading Hub 3.0 BOS/IDM/OB/FVG rules. "
                "The Portfolio Manager should judge portfolio risk, leverage, and whether execution is allowed. "
                "If external news, whale activity, or exchange flow data is absent, state that limitation in Chinese "
                "instead of inventing it. "
                "Return only the structured decision requested by the schema. 所有文字说明必须使用中文。\n\n"
                f"{payload_json}"
            ),
        },
    ]


def _candle_summary(candles: list[dict[str, Any]]) -> dict[str, Any]:
    if not candles:
        return {
            "count": 0,
            "latest": None,
        }
    latest = candles[-1]
    window = candles[-20:] if len(candles) > 20 else candles
    highs = [_safe_float(candle.get("high")) for candle in window]
    lows = [_safe_float(candle.get("low")) for candle in window]
    closes = [_safe_float(candle.get("close")) for candle in window]
    volumes = [_safe_float(candle.get("volume")) for candle in window]
    return {
        "count": len(candles),
        "window_count": len(window),
        "latest": {
            "timestamp": latest.get("timestamp"),
            "open": latest.get("open"),
            "high": latest.get("high"),
            "low": latest.get("low"),
            "close": latest.get("close"),
            "volume": latest.get("volume"),
        },
        "window_high": max(highs) if highs else None,
        "window_low": min(lows) if lows else None,
        "window_close_change": (
            round(closes[-1] - closes[0], 8)
            if len(closes) >= 2 and closes[0] is not None and closes[-1] is not None
            else None
        ),
        "window_volume": round(sum(value for value in volumes if value is not None), 8),
    }


def _safe_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _price_in_range(price: Any, low: Any, high: Any) -> bool:
    price_value = _safe_float(price)
    low_value = _safe_float(low)
    high_value = _safe_float(high)
    if price_value is None or low_value is None or high_value is None:
        return False
    return min(low_value, high_value) <= price_value <= max(low_value, high_value)


def _lifecycle_instruction(stage: str) -> str:
    instructions = {
        "PENDING": "Parse the task request and identify the required market decision workflow.",
        "ORDERED": "Draft the intermediate handling plan and risk checks while the worker is processing.",
        "COMPLETED": "Summarize the final result and whether it deserves a deeper trading review.",
    }
    return instructions.get(stage, "Summarize the task lifecycle stage.")


def _bind_structured(llm: Any, schema: type[CryptoLLMDecision]) -> Any | None:
    try:
        return llm.with_structured_output(schema)
    except (NotImplementedError, AttributeError):
        return None


def _parse_free_text_decision(content: str) -> CryptoLLMDecision:
    try:
        parsed = json.loads(_extract_json_object(content))
        return CryptoLLMDecision.model_validate(parsed)
    except (json.JSONDecodeError, ValidationError, ValueError) as exc:
        return fallback_hold_decision(f"LLM response could not be parsed as a structured decision: {exc}")


def _extract_json_object(content: str) -> str:
    stripped = content.strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        return stripped
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("No JSON object found in LLM response.")
    return stripped[start:end + 1]
