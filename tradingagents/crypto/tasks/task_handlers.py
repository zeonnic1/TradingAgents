from __future__ import annotations

import time
import logging
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any

from tradingagents.crypto.config import get_crypto_settings
from tradingagents.crypto.utils.exchanges import get_exchange_client
from tradingagents.crypto.utils.ledger import get_task_record, update_order_status
from tradingagents.crypto.llm_reviewer import lifecycle_llm_log, review_with_llm
from tradingagents.crypto.models import BotRunRequest, OrderRequest, SignalAction
from tradingagents.crypto.services.service import analyze_market, create_order
from tradingagents.crypto.tasks.task_fsm import TaskStatus
from tradingagents.crypto.tasks.task_mixins import RedisPublishMixin, TaskLoggingMixin, TaskStateMixin


logger = logging.getLogger(__name__)


class BaseTaskHandler(TaskLoggingMixin, TaskStateMixin, RedisPublishMixin, ABC):
    """Base class for Celery task handlers.

    Workflow semantics:
    - PENDING means the job is polling strategy and no order exists yet.
    - ORDERED means an order has been created and is being watched.
    - COMPLETED means the watched order hit stop loss or take profit.
    """

    task_type: str

    @abstractmethod
    def handle(self, task_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    def llm_lifecycle_hook(self, task_id: str, stage: TaskStatus, context: dict[str, Any]) -> None:
        try:
            content = lifecycle_llm_log(stage.value, context)
            log_entry = {
                "stage": stage.value,
                "content": content,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            self.set_status(task_id, stage, llm_log=log_entry)
            self.log(task_id, "info", f"LLM lifecycle hook completed at {stage.value}.", log_entry)
        except Exception as exc:
            self.log(task_id, "warn", f"LLM lifecycle hook skipped at {stage.value}.", {"error": str(exc)})

    def is_canceled(self, task_id: str) -> bool:
        record = get_task_record(task_id)
        return bool(record and str(record.status).upper() == TaskStatus.CANCELED.value)


class CryptoBotRunHandler(BaseTaskHandler):
    task_type = "crypto_bot_run"

    def handle(self, task_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        request = BotRunRequest(**payload)
        settings = get_crypto_settings()
        last_payload: dict[str, Any] = {}
        transaction_enabled = _transaction_enabled(request)

        if request.use_llm_decision:
            self.llm_lifecycle_hook(task_id, TaskStatus.PENDING, {"request": request.model_dump(mode="json")})
        self.log(task_id, "info", "Task is polling strategy while PENDING.", {
            "max_attempts": settings.task_poll_max_attempts,
            "interval_seconds": settings.task_poll_interval_seconds,
            "transaction": transaction_enabled,
            "use_llm_decision": request.use_llm_decision,
        })

        attempt = 0
        while True:
            attempt += 1
            if self.is_canceled(task_id):
                self.log(task_id, "warn", "Task canceled during strategy polling.")
                return last_payload

            self.log(task_id, "info", "Polling strategy attempt started.", {"attempt": attempt})
            last_payload = analyze_market(request)
            runtime_log = _build_strategy_runtime_log(request, last_payload)
            runtime_log_context = _build_strategy_runtime_context(request, last_payload, attempt)
            logger.info(runtime_log)
            self.log(task_id, "info", runtime_log, runtime_log_context)
            self.publish_log(task_id, "info", runtime_log, runtime_log_context)
            last_payload["order"] = None
            last_payload["account"] = None
            last_payload["strategy_poll"] = {
                "attempt": attempt,
                "next_interval_seconds": settings.task_poll_interval_seconds,
            }
            decision_payload = None

            if request.use_llm_decision:
                try:
                    decision = review_with_llm(last_payload)
                    decision_payload = decision.model_dump(mode="json")
                    last_payload["llm_decision"] = decision_payload
                except Exception as exc:
                    self.log(task_id, "warn", "LLM decision skipped during PENDING polling.", {"error": str(exc)})
            else:
                last_payload["llm_decision"] = None

            progress = _pending_progress(attempt, settings.task_poll_max_attempts)
            if self.is_canceled(task_id):
                self.log(task_id, "warn", "Task canceled after strategy analysis; skip PENDING update.", {
                    "attempt": attempt,
                })
                return last_payload

            order_request, order_block_reason = self._order_request_from_decision(request, last_payload, decision_payload)
            if order_request is not None:
                self.set_status(task_id, TaskStatus.PENDING, progress=progress, result=last_payload)
                self.publish(
                    task_id,
                    TaskStatus.PENDING,
                    ready=False,
                    progress=progress,
                    result=last_payload,
                    log=self.make_log_payload(task_id, "info", "Strategy satisfied; creating order.", {
                        "attempt": attempt,
                        "rule_action": last_payload.get("analysis", {}).get("action"),
                        "transaction": transaction_enabled,
                        "amount_usdt": request.amount,
                        "use_llm_decision": request.use_llm_decision,
                    }),
                )
                if self.is_canceled(task_id):
                    self.log(task_id, "warn", "Task canceled before order creation.", {"attempt": attempt})
                    return last_payload
                order_response = create_order(order_request)
                last_payload["order"] = order_response.model_dump(mode="json")
                last_payload["account"] = order_response.account.model_dump(mode="json") if order_response.account else None
                self.set_status(task_id, TaskStatus.ORDERED, progress=60, result=last_payload)
                order_log_context = {
                    "order_id": order_response.order.get("id"),
                    "side": order_request.side.value,
                    "amount_usdt": order_request.amount,
                    "leverage": order_request.leverage,
                }
                self.publish(
                    task_id,
                    TaskStatus.ORDERED,
                    ready=False,
                    progress=60,
                    result=last_payload,
                    log=self.make_log_payload(task_id, "info", "Order created; task moved to ORDERED.", order_log_context),
                )
                self.log(task_id, "info", "Order created; task moved to ORDERED.", order_log_context)
                return self._monitor_order_until_exit(task_id, request, last_payload)

            pending_reason = order_block_reason or _pending_reason(last_payload, decision_payload, request.use_llm_decision)
            last_payload["strategy_poll"]["reason"] = pending_reason
            log_context = {
                "attempt": attempt,
                "rule_action": last_payload.get("analysis", {}).get("action"),
                "transaction": transaction_enabled,
                "amount_usdt": request.amount,
                "use_llm_decision": request.use_llm_decision,
                "reason": pending_reason,
                "analysis_reasons": last_payload.get("analysis", {}).get("reasons", []),
            }
            self.log(task_id, "info", f"Strategy not satisfied: {pending_reason}", log_context)
            if self.is_canceled(task_id):
                self.log(task_id, "warn", "Task canceled before pending status publish.", {"attempt": attempt})
                return last_payload
            self.set_status(task_id, TaskStatus.PENDING, progress=progress, result=last_payload)
            self.publish(
                task_id,
                TaskStatus.PENDING,
                ready=False,
                progress=progress,
                result=last_payload,
                log={
                    "task_id": task_id,
                    "level": "info",
                    "message": f"Strategy not satisfied: {pending_reason}",
                    "context": log_context,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                },
            )
            time.sleep(settings.task_poll_interval_seconds)

    def _order_request_from_decision(
        self,
        request: BotRunRequest,
        analysis_payload: dict[str, Any],
        decision_payload: dict[str, Any] | None,
    ) -> tuple[OrderRequest | None, str | None]:
        analysis = analysis_payload.get("analysis") or {}
        rule_action = analysis.get("action")
        if rule_action not in {SignalAction.BUY.value, SignalAction.SELL.value}:
            return None, None

        if request.use_llm_decision:
            decision = (decision_payload or {}).get("decision") or {}
            approved = bool(decision.get("approved"))
            action = decision.get("action") or rule_action
            if not approved or action not in {SignalAction.BUY.value, SignalAction.SELL.value}:
                return None, f"strategy action is {rule_action}, but LLM did not approve actionable trade"
            leverage = int(decision.get("recommended_leverage") or request.leverage)
        else:
            action = rule_action
            leverage = request.leverage

        if not _transaction_enabled(request):
            return None, f"strategy action is {rule_action}, but transaction mode is disabled"
        if request.amount <= 0:
            return None, f"strategy action is {rule_action}, but amount_usdt must be greater than 0"

        return OrderRequest(
            exchange=request.exchange,
            symbol=request.symbol,
            side=action,
            amount=request.amount,
            leverage=leverage,
        ), None

    def _monitor_order_until_exit(
        self,
        task_id: str,
        request: BotRunRequest,
        analysis_payload: dict[str, Any],
    ) -> dict[str, Any]:
        settings = get_crypto_settings()
        order = (analysis_payload.get("order") or {}).get("order") or {}
        order_id = str(order.get("id") or "")
        decision = ((analysis_payload.get("llm_decision") or {}).get("decision") or {})
        analysis = analysis_payload.get("analysis") or {}
        side = str(order.get("side") or decision.get("action") or analysis.get("action") or "").lower()
        stop_loss = _first_number(decision.get("stop_loss"), analysis.get("stop_loss"))
        take_profit = _first_number(
            decision.get("take_profit_2"),
            decision.get("take_profit_1"),
            analysis.get("take_profit_2"),
            analysis.get("take_profit_1"),
        )

        if request.use_llm_decision:
            self.llm_lifecycle_hook(task_id, TaskStatus.ORDERED, {
                "order_id": order_id,
                "side": side,
                "stop_loss": stop_loss,
                "take_profit": take_profit,
            })

        attempt = 0
        while True:
            attempt += 1
            if self.is_canceled(task_id):
                if order_id:
                    update_order_status(order_id, "canceled_by_task")
                self.log(task_id, "warn", "Task canceled during order monitoring.", {"order_id": order_id})
                return analysis_payload

            ticker = get_exchange_client(request.exchange).fetch_ticker(request.symbol)
            mark = float(ticker.get("last") or ticker.get("close") or 0)
            exit_status = _resolve_exit_status(side, mark, stop_loss, take_profit)
            progress = min(60 + attempt, 95)
            analysis_payload["order_watch"] = {
                "order_id": order_id,
                "mark_price": mark,
                "stop_loss": stop_loss,
                "take_profit": take_profit,
                "attempt": attempt,
            }
            self.set_status(task_id, TaskStatus.ORDERED, progress=progress, result=analysis_payload)
            watch_log_context = {
                "order_id": order_id,
                "mark_price": mark,
                "stop_loss": stop_loss,
                "take_profit": take_profit,
                "attempt": attempt,
            }
            self.log(task_id, "info", "Order monitor tick.", watch_log_context)
            self.publish(
                task_id,
                TaskStatus.ORDERED,
                ready=False,
                progress=progress,
                result=analysis_payload,
                log=self.make_log_payload(task_id, "info", "Order monitor tick.", watch_log_context),
            )

            if exit_status:
                if order_id:
                    update_order_status(order_id, exit_status, {"closed_mark_price": mark})
                analysis_payload["order_watch"]["exit_status"] = exit_status
                self.set_status(task_id, TaskStatus.COMPLETED, progress=100, result=analysis_payload)
                self.publish(task_id, TaskStatus.COMPLETED, ready=True, progress=100, result=analysis_payload)
                self.log(task_id, "info", "Order monitor completed task.", {
                    "order_id": order_id,
                    "exit_status": exit_status,
                    "mark_price": mark,
                })
                return analysis_payload

            time.sleep(settings.order_monitor_interval_seconds)


def _transaction_enabled(request: BotRunRequest) -> bool:
    return bool(request.transaction or request.execute)


def _pending_progress(attempt: int, max_attempts: int) -> int:
    if max_attempts <= 0:
        return min(10 + attempt, 50)
    return min(10 + int(attempt / max(max_attempts, 1) * 40), 50)


def _first_number(*values) -> float | None:
    for value in values:
        if value not in (None, ""):
            return float(value)
    return None


def _build_strategy_runtime_log(request: BotRunRequest, analysis_payload: dict[str, Any]) -> str:
    snapshot = analysis_payload.get("snapshot") or {}
    analysis = analysis_payload.get("analysis") or {}
    candles = snapshot.get("candles") or []
    last_candle = candles[-1] if candles else {}
    current_price = last_candle.get("close", "-")
    metadata = analysis.get("metadata") or {}
    daily_bias = analysis.get("bias") or metadata.get("vegas_bias") or "-"
    ltf = request.timeframe
    htf = _resolve_htf(ltf)
    sweep_label, sweep_price, sweep_time = _resolve_waiting_sweep_point(metadata, candles)
    bos_description = _resolve_bos_description(metadata, candles)
    poi_ob = _resolve_poi_ob_range(analysis, metadata)
    return (
        f"日线偏见:{daily_bias} 当前价格:{current_price} 当前LTF:{ltf} HTF:{htf} "
        f"{bos_description} 等待清扫:{sweep_label}:{sweep_price} 时间:{sweep_time} 高POI/OB区间:{poi_ob}"
    )


def _build_strategy_runtime_context(request: BotRunRequest, analysis_payload: dict[str, Any], attempt: int) -> dict[str, Any]:
    analysis = analysis_payload.get("analysis") or {}
    metadata = analysis.get("metadata") or {}
    candles = (analysis_payload.get("snapshot") or {}).get("candles") or []
    sweep_label, sweep_price, sweep_time = _resolve_waiting_sweep_point(metadata, candles)
    return {
        "attempt": attempt,
        "symbol": request.symbol,
        "ltf": request.timeframe,
        "htf": _resolve_htf(request.timeframe),
        "daily_bias": analysis.get("bias") or metadata.get("vegas_bias"),
        "bos_direction": metadata.get("bos_direction"),
        "bos_description": _resolve_bos_description(metadata, candles),
        "bos_break_price": metadata.get("bos_break_price"),
        "bos_broken_swing_time": _format_candle_time_by_index(candles, metadata.get("bos_broken_swing_index")),
        "bos_occurrence_time": _format_candle_time_by_index(candles, metadata.get("bos_break_index")),
        "waiting_sweep_label": sweep_label,
        "waiting_sweep_price": sweep_price,
        "waiting_sweep_time": sweep_time,
        "dynamic_structure_high": metadata.get("dynamic_structure_high"),
        "dynamic_structure_low": metadata.get("dynamic_structure_low"),
        "poi_range": metadata.get("poi_range"),
        "poi_ob_low": metadata.get("poi_ob_low"),
        "poi_ob_high": metadata.get("poi_ob_high"),
        "poi_ob_time": _format_candle_time_by_index(candles, metadata.get("poi_ob_index")),
        "poi_ob_fvg_low": metadata.get("poi_ob_fvg_low"),
        "poi_ob_fvg_high": metadata.get("poi_ob_fvg_high"),
        "poi_ob_fvg_time": _format_candle_time_by_index(candles, metadata.get("poi_ob_fvg_index")),
    }


def _resolve_bos_description(metadata: dict[str, Any], candles: list[dict[str, Any]]) -> str:
    bos_direction = metadata.get("bos_direction")
    break_price = _display_value(metadata.get("bos_break_price"))
    broken_swing_time = _format_candle_time_by_index(candles, metadata.get("bos_broken_swing_index"))
    occurrence_time = _format_candle_time_by_index(candles, metadata.get("bos_break_index"))
    if bos_direction == "bearish":
        lower_low = _display_value(metadata.get("dynamic_structure_low"))
        lower_low_time = _format_candle_time_by_index(candles, metadata.get("dynamic_structure_low_index"))
        return (
            f"被跌破低点为{break_price} 时间:{broken_swing_time},"
            f"BOS发生时间:{occurrence_time},更低的低点为{lower_low} 时间:{lower_low_time}"
        )
    if bos_direction == "bullish":
        higher_high = _display_value(metadata.get("dynamic_structure_high"))
        higher_high_time = _format_candle_time_by_index(candles, metadata.get("dynamic_structure_high_index"))
        return (
            f"被突破高点为{break_price} 时间:{broken_swing_time},"
            f"BOS发生时间:{occurrence_time},更高的高点为{higher_high} 时间:{higher_high_time}"
        )
    return "BOS:-"


def _display_value(value: Any) -> Any:
    if value is None:
        return "-"
    return value


def _resolve_waiting_sweep_point(metadata: dict[str, Any], candles: list[dict[str, Any]] | None = None) -> tuple[str, Any, str]:
    candles = candles or []
    bos_direction = metadata.get("bos_direction")
    if bos_direction == "bearish":
        return (
            "dynamic_structure_high",
            metadata.get("dynamic_structure_high", "-"),
            _format_candle_time_by_index(candles, metadata.get("dynamic_structure_high_index")),
        )
    if bos_direction == "bullish":
        return (
            "dynamic_structure_low",
            metadata.get("dynamic_structure_low", "-"),
            _format_candle_time_by_index(candles, metadata.get("dynamic_structure_low_index")),
        )
    return "dynamic_structure", "-", "-"


def _format_candle_time_by_index(candles: list[dict[str, Any]], index: Any) -> str:
    try:
        candle = candles[int(index)]
    except (TypeError, ValueError, IndexError):
        return "-"
    if not isinstance(candle, dict):
        return "-"
    return _format_timestamp(candle.get("timestamp"))


def _format_timestamp(timestamp: Any) -> str:
    try:
        value = int(timestamp)
    except (TypeError, ValueError):
        return "-"
    if value <= 0:
        return "-"
    seconds = value / 1000 if value > 10_000_000_000 else value
    return datetime.fromtimestamp(seconds, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _resolve_htf(timeframe: str) -> str:
    mapping = {
        "1m": "15m",
        "3m": "15m",
        "5m": "1h",
        "15m": "4h",
        "30m": "4h",
        "1h": "1d",
        "2h": "1d",
        "4h": "1d",
        "1d": "1w",
    }
    return mapping.get(str(timeframe).lower(), "1d")


def _resolve_poi_ob_range(analysis: dict[str, Any], metadata: dict[str, Any]) -> str:
    poi_low = metadata.get("poi_ob_low")
    poi_high = metadata.get("poi_ob_high")
    if poi_low is not None and poi_high is not None:
        fvg_low = metadata.get("poi_ob_fvg_low")
        fvg_high = metadata.get("poi_ob_fvg_high")
        fvg = f" FVG:{fvg_low}-{fvg_high}" if fvg_low is not None and fvg_high is not None else ""
        return f"{poi_low}-{poi_high}{fvg}"
    entry = analysis.get("entry")
    stop = analysis.get("stop_loss")
    if entry is not None and stop is not None:
        low = min(float(entry), float(stop))
        high = max(float(entry), float(stop))
        return f"{low}-{high}"
    range_low = metadata.get("range_low")
    range_high = metadata.get("range_high")
    if range_low is not None and range_high is not None:
        return f"{range_low}-{range_high}"
    return "-"


def _pending_reason(analysis_payload: dict[str, Any], decision_payload: dict[str, Any] | None, use_llm_decision: bool) -> str:
    analysis = analysis_payload.get("analysis") or {}
    rule_action = analysis.get("action")
    reasons = analysis.get("reasons") or []
    if rule_action not in {SignalAction.BUY.value, SignalAction.SELL.value}:
        detail = "; ".join(str(reason) for reason in reasons[:3]) if reasons else "rule action is hold"
        return f"rule strategy action is {rule_action or 'missing'}; {detail}"
    if use_llm_decision:
        decision = (decision_payload or {}).get("decision") or {}
        if not decision.get("approved"):
            return "LLM did not approve the trade"
        llm_action = decision.get("action")
        if llm_action not in {SignalAction.BUY.value, SignalAction.SELL.value}:
            return f"LLM action is {llm_action or 'missing'}"
    return "order request was not created"


def _resolve_exit_status(side: str, mark: float, stop_loss: float | None, take_profit: float | None) -> str | None:
    if side == SignalAction.BUY.value:
        if stop_loss is not None and mark <= stop_loss:
            return "closed_stop_loss"
        if take_profit is not None and mark >= take_profit:
            return "closed_take_profit"
    if side == SignalAction.SELL.value:
        if stop_loss is not None and mark >= stop_loss:
            return "closed_stop_loss"
        if take_profit is not None and mark <= take_profit:
            return "closed_take_profit"
    return None
