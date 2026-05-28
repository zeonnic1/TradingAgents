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
    last_candles = candles[-20:] if len(candles) > 20 else candles
    return {
        "exchange": snapshot.get("exchange"),
        "symbol": snapshot.get("symbol"),
        "timeframe": snapshot.get("timeframe"),
        "analysis": analysis_payload.get("analysis") or {},
        "account": analysis_payload.get("account"),
        "order": analysis_payload.get("order"),
        "recent_candles": last_candles,
    }


def is_llm_decision_eligible(analysis_payload: dict[str, Any]) -> bool:
    analysis = analysis_payload.get("analysis") or {}
    return analysis.get("action") in {SignalAction.BUY.value, SignalAction.SELL.value}


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
    prompt = _build_prompt(rule_payload)
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
    )


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


def _build_prompt(rule_payload: dict[str, Any]) -> list[dict[str, str]]:
    payload_json = json.dumps(rule_payload, ensure_ascii=False, indent=2)
    return [
        {
            "role": "system",
            "content": (
                "You are a crypto trading decision agent composed of two roles: Trader and Portfolio Manager. "
                "The Python strategy tool has already produced deterministic market features and a candidate signal. "
                "Your job is to review the JSON, decide whether the candidate trade should be approved, reduced, "
                "or rejected, and return a strict structured decision. Do not invent market data. Do not approve "
                "a trade when the strategy action is hold, confidence is weak, stop loss is missing, or risk is unclear."
            ),
        },
        {
            "role": "user",
            "content": (
                "Review this Trading Hub crypto rule payload. The Trader should judge entry quality and levels. "
                "The Portfolio Manager should judge portfolio risk, leverage, and whether execution is allowed. "
                "Return only the structured decision requested by the schema.\n\n"
                f"{payload_json}"
            ),
        },
    ]


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
