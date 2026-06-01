from __future__ import annotations

import time
import logging
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any

from tradingagents.crypto.config import get_crypto_settings
from tradingagents.crypto.utils.exchanges import get_exchange_client
from tradingagents.crypto.utils.ledger import get_task_record, update_order_status
from tradingagents.crypto.llm_reviewer import compact_llm_decision_payload, lifecycle_llm_log, review_with_llm
from tradingagents.crypto.models import BotRunRequest, OrderRequest, OrderType, SignalAction
from tradingagents.crypto.services.service import analyze_market, create_order
from tradingagents.crypto.services.trade_proposal import build_candidate_trade
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
            poi_consumed = _poi_consumed_after_bos(last_payload)
            last_payload["poi_consumed"] = poi_consumed
            if poi_consumed:
                runtime_log = _build_strategy_runtime_log(request, last_payload)
                runtime_log_context = _build_strategy_runtime_context(request, last_payload, attempt)
                logger.info(runtime_log)
                self.log(task_id, "info", runtime_log, runtime_log_context)
                self.publish_log(task_id, "info", runtime_log, runtime_log_context)
                message = "POI/OB zone was touched by a later candle and marked consumed; auto async task will not submit it to the LLM decision layer."
                self.log(task_id, "info", message, poi_consumed)
                self.set_status(task_id, TaskStatus.COMPLETED, progress=100, result=last_payload)
                self.publish(
                    task_id,
                    TaskStatus.COMPLETED,
                    ready=True,
                    progress=100,
                    result=last_payload,
                    log=self.make_log_payload(task_id, "info", message, poi_consumed),
                )
                return last_payload
            candidate_trade = _candidate_trade_from_poi(last_payload)
            last_payload["candidate_trade"] = candidate_trade
            runtime_log = _build_strategy_runtime_log(request, last_payload)
            runtime_log_context = _build_strategy_runtime_context(request, last_payload, attempt)
            logger.info(runtime_log)
            self.log(task_id, "info", runtime_log, runtime_log_context)
            self.publish_log(task_id, "info", runtime_log, runtime_log_context)
            if candidate_trade:
                self.log(task_id, "info", "BOS confirmed; high-probability POI sent to decision layer.", candidate_trade)
                self.publish_log(task_id, "info", "BOS confirmed; high-probability POI sent to decision layer.", candidate_trade)
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
                    decision_payload = compact_llm_decision_payload(decision)
                    last_payload["llm_decision"] = decision_payload
                    self.log(task_id, "info", "LLM reference advice completed.", {
                        "approved": decision_payload.get("decision", {}).get("approved"),
                        "action": decision_payload.get("decision", {}).get("action"),
                        "risk_level": decision_payload.get("decision", {}).get("risk_level"),
                        "trader_rationale": decision_payload.get("decision", {}).get("trader_rationale"),
                        "portfolio_rationale": decision_payload.get("decision", {}).get("portfolio_rationale"),
                    })
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
                        "candidate_action": (last_payload.get("candidate_trade") or {}).get("action"),
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
                    "type": order_request.type.value,
                    "entry_price": order_request.price,
                    "amount_usdt": order_request.amount,
                    "leverage": order_request.leverage,
                    "stop_loss": order_request.params.get("stop_loss"),
                    "take_profit": order_request.params.get("take_profit"),
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

    def manual_decision_order(self, task_id: str, *, use_llm: bool = False) -> dict[str, Any]:
        task = get_task_record(task_id)
        if task is None:
            raise ValueError("Task not found.")
        status = str(task.status or "").upper()
        if status != TaskStatus.COMPLETED.value:
            raise ValueError(f"Manual decision order is only allowed for COMPLETED tasks, current status is {status}.")
        if not (task.result or task.result_data):
            raise ValueError("Completed task has no analysis result to send to the decision layer.")

        request = BotRunRequest(
            exchange=task.exchange,
            symbol=task.symbol,
            timeframe=task.timeframe,
            limit=_resolve_task_limit(task.result or task.result_data),
            execute=task.execute,
            transaction=task.execute,
            use_llm_decision=use_llm,
            amount=task.amount,
            leverage=task.leverage,
        )
        if not _transaction_enabled(request):
            raise ValueError("Transaction mode is disabled for this task.")
        if request.amount <= 0:
            raise ValueError("Task amount_usdt must be greater than 0 before manual order placement.")

        analysis_payload = dict(task.result or task.result_data or {})
        if not analysis_payload:
            analysis_payload = analyze_market(request)

        candidate_trade = _candidate_trade_from_poi(analysis_payload, ignore_consumed=True)
        if candidate_trade is None:
            analysis_payload["manual_order_block_reason"] = "BOS POI candidate trade is missing"
            self.set_status(task_id, TaskStatus.COMPLETED, result=analysis_payload, force=True)
            self.publish(
                task_id,
                TaskStatus.COMPLETED,
                ready=True,
                result=analysis_payload,
                log=self.make_log_payload(task_id, "warn", "Manual decision did not create an order.", {
                    "reason": analysis_payload["manual_order_block_reason"],
                }),
            )
            return analysis_payload
        analysis_payload["candidate_trade"] = candidate_trade
        _promote_candidate_trade_for_manual_decision(analysis_payload, candidate_trade, use_llm=use_llm)
        decision_log = _build_manual_order_log(request, candidate_trade, use_llm=use_llm)
        self.log(task_id, "info", "LLM decision order requested." if use_llm else "Manual direct order requested.", {
            "candidate_action": (candidate_trade or {}).get("action"),
            "entry": candidate_trade.get("entry"),
            "stop_loss": candidate_trade.get("stop_loss"),
            "take_profit": candidate_trade.get("take_profit"),
            "amount_usdt": request.amount,
            "leverage": request.leverage,
            "use_llm": use_llm,
        })
        logger.info(decision_log)
        self.log(task_id, "info", decision_log, {
            "candidate_trade": candidate_trade,
            "use_llm": use_llm,
        })

        decision_payload = None
        if use_llm:
            decision = review_with_llm(analysis_payload)
            decision_payload = compact_llm_decision_payload(decision)
            analysis_payload["llm_decision"] = decision_payload
            self.log(task_id, "info", "LLM order decision completed.", {
                "approved": decision_payload.get("decision", {}).get("approved"),
                "action": decision_payload.get("decision", {}).get("action"),
            })
        else:
            analysis_payload["llm_decision"] = None

        order_request, block_reason = self._order_request_from_decision(
            request,
            analysis_payload,
            decision_payload,
            enforce_poi_consumed=False,
        )
        if order_request is None:
            analysis_payload["manual_order_block_reason"] = block_reason or "decision layer did not create an order"
            self.set_status(task_id, TaskStatus.COMPLETED, result=analysis_payload, force=True)
            self.publish(
                task_id,
                TaskStatus.COMPLETED,
                ready=True,
                result=analysis_payload,
                log=self.make_log_payload(task_id, "warn", "Manual decision did not create an order.", {
                    "reason": analysis_payload["manual_order_block_reason"],
                }),
            )
            return analysis_payload

        order_response = create_order(order_request)
        analysis_payload["order"] = order_response.model_dump(mode="json")
        analysis_payload["account"] = order_response.account.model_dump(mode="json") if order_response.account else None
        self.set_status(task_id, TaskStatus.ORDERED, progress=60, result=analysis_payload, force=True)
        order_log_context = {
            "order_id": order_response.order.get("id"),
            "side": order_request.side.value,
            "type": order_request.type.value,
            "entry_price": order_request.price,
            "amount_usdt": order_request.amount,
            "leverage": order_request.leverage,
            "stop_loss": order_request.params.get("stop_loss"),
            "take_profit": order_request.params.get("take_profit"),
        }
        created_message = "LLM decision created order; task moved to ORDERED." if use_llm else "Manual direct order created; task moved to ORDERED."
        self.log(task_id, "info", created_message, order_log_context)
        self.publish(
            task_id,
            TaskStatus.ORDERED,
            ready=False,
            progress=60,
            result=analysis_payload,
            log=self.make_log_payload(task_id, "info", created_message, order_log_context),
        )
        return self._monitor_order_until_exit(task_id, request, analysis_payload)

    def _order_request_from_decision(
        self,
        request: BotRunRequest,
        analysis_payload: dict[str, Any],
        decision_payload: dict[str, Any] | None,
        *,
        enforce_poi_consumed: bool = True,
    ) -> tuple[OrderRequest | None, str | None]:
        analysis = analysis_payload.get("analysis") or {}
        if enforce_poi_consumed:
            poi_consumed = analysis_payload.get("poi_consumed") or _poi_consumed_after_bos(analysis_payload)
            if poi_consumed:
                analysis_payload["poi_consumed"] = poi_consumed
                analysis_payload["candidate_trade"] = None
                return None, (
                    "POI already consumed by a later candle at "
                    f"{poi_consumed.get('consumed_time')}; skip decision layer and order creation"
                )
        candidate = analysis_payload.get("candidate_trade") or _candidate_trade_from_poi(analysis_payload) or {}
        rule_action = candidate.get("action") or analysis.get("action")
        if rule_action not in {SignalAction.BUY.value, SignalAction.SELL.value}:
            return None, None

        entry = _first_number(candidate.get("entry"), analysis.get("entry"))
        stop_loss = _first_number(candidate.get("stop_loss"), analysis.get("stop_loss"))
        take_profit = _first_number(candidate.get("take_profit"), analysis.get("take_profit_2"), analysis.get("take_profit_1"))

        if request.use_llm_decision:
            decision = (decision_payload or {}).get("decision") or {}
            approved = bool(decision.get("approved"))
            action = decision.get("action") or rule_action
            if not approved or action not in {SignalAction.BUY.value, SignalAction.SELL.value}:
                return None, f"strategy action is {rule_action}, but LLM did not approve actionable trade"
            leverage = int(decision.get("recommended_leverage") or request.leverage)
            entry = _first_number(decision.get("entry"), entry)
            stop_loss = _first_number(decision.get("stop_loss"), stop_loss)
            take_profit = _first_number(decision.get("take_profit_2"), decision.get("take_profit_1"), take_profit)
        else:
            action = rule_action
            leverage = request.leverage

        if not _transaction_enabled(request):
            return None, f"strategy action is {rule_action}, but transaction mode is disabled"
        if request.amount <= 0:
            return None, f"strategy action is {rule_action}, but amount_usdt must be greater than 0"
        if entry is None or stop_loss is None or take_profit is None:
            return None, f"strategy action is {rule_action}, but entry/stop_loss/take_profit is incomplete"

        return OrderRequest(
            exchange=request.exchange,
            symbol=request.symbol,
            side=action,
            type=OrderType.LIMIT,
            amount=request.amount,
            price=entry,
            leverage=leverage,
            params={
                "stop_loss": stop_loss,
                "take_profit": take_profit,
                "stopLossPrice": stop_loss,
                "takeProfitPrice": take_profit,
                "reduceOnly": False,
                "candidate_trade": candidate,
            },
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
        order_info = order.get("info") or {}
        order_params = order_info.get("params") or {}
        entry_price = _first_number(order.get("price"), order.get("average"))
        stop_loss = _first_number(order_params.get("stop_loss"), decision.get("stop_loss"), analysis.get("stop_loss"))
        take_profit = _first_number(
            order_params.get("take_profit"),
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
            filled = bool((analysis_payload.get("order_watch") or {}).get("filled"))
            if not filled:
                filled = _entry_filled(side, mark, entry_price)
                if filled and order_id:
                    update_order_status(order_id, "filled", {"filled_mark_price": mark})
            exit_status = _resolve_exit_status(side, mark, stop_loss, take_profit) if filled else None
            progress = min(60 + attempt, 95)
            analysis_payload["order_watch"] = {
                "order_id": order_id,
                "entry_price": entry_price,
                "mark_price": mark,
                "filled": filled,
                "stop_loss": stop_loss,
                "take_profit": take_profit,
                "attempt": attempt,
            }
            self.set_status(task_id, TaskStatus.ORDERED, progress=progress, result=analysis_payload)
            watch_log_context = {
                "order_id": order_id,
                "entry_price": entry_price,
                "mark_price": mark,
                "filled": filled,
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


def _resolve_task_limit(result: dict[str, Any] | None) -> int:
    candles = ((result or {}).get("snapshot") or {}).get("candles") or []
    if candles:
        return max(60, min(len(candles), 1000))
    return 300


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
        f"Daily bias:{daily_bias} current price:{current_price} LTF:{ltf} HTF:{htf} "
        f"{bos_description} waiting sweep:{sweep_label}:{sweep_price} time:{sweep_time} POI/OB range:{poi_ob}"
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


def _build_manual_order_log(request: BotRunRequest, candidate_trade: dict[str, Any], *, use_llm: bool) -> str:
    mode = "LLM挂单" if use_llm else "手动挂单"
    return (
        f"{mode}: symbol:{request.symbol} action:{candidate_trade.get('action')} "
        f"entry:{candidate_trade.get('entry')} stop:{candidate_trade.get('stop_loss')} "
        f"take_profit:{candidate_trade.get('take_profit')} amount_usdt:{request.amount} "
        f"leverage:{request.leverage}x，不等待清扫条件。"
    )


def _resolve_bos_description(metadata: dict[str, Any], candles: list[dict[str, Any]]) -> str:
    bos_direction = metadata.get("bos_direction")
    break_price = _display_value(metadata.get("bos_break_price"))
    broken_swing_time = _format_candle_time_by_index(candles, metadata.get("bos_broken_swing_index"))
    occurrence_time = _format_candle_time_by_index(candles, metadata.get("bos_break_index"))
    if bos_direction == "bearish":
        lower_low = _display_value(metadata.get("dynamic_structure_low"))
        lower_low_time = _format_candle_time_by_index(candles, metadata.get("dynamic_structure_low_index"))
        return (
            f"broken low:{break_price} time:{broken_swing_time},"
            f"BOS time:{occurrence_time},lower low:{lower_low} time:{lower_low_time}"
        )
    if bos_direction == "bullish":
        higher_high = _display_value(metadata.get("dynamic_structure_high"))
        higher_high_time = _format_candle_time_by_index(candles, metadata.get("dynamic_structure_high_index"))
        return (
            f"broken high:{break_price} time:{broken_swing_time},"
            f"BOS time:{occurrence_time},higher high:{higher_high} time:{higher_high_time}"
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
    return "no OB+FVG"


def _pending_reason(analysis_payload: dict[str, Any], decision_payload: dict[str, Any] | None, use_llm_decision: bool) -> str:
    analysis = analysis_payload.get("analysis") or {}
    candidate = analysis_payload.get("candidate_trade") or {}
    rule_action = analysis.get("action")
    reasons = analysis.get("reasons") or []
    if candidate.get("action") in {SignalAction.BUY.value, SignalAction.SELL.value}:
        rule_action = candidate.get("action")
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


def _candidate_trade_from_poi(
    analysis_payload: dict[str, Any],
    *,
    ignore_consumed: bool = False,
) -> dict[str, Any] | None:
    return build_candidate_trade(analysis_payload, ignore_consumed=ignore_consumed)


def _promote_candidate_trade_for_manual_decision(
    analysis_payload: dict[str, Any],
    candidate_trade: dict[str, Any],
    *,
    use_llm: bool,
) -> None:
    analysis = dict(analysis_payload.get("analysis") or {})
    original_setup = analysis.get("setup")
    reasons = list(analysis.get("reasons") or [])
    manual_reason = (
        "LLM decision order: the confirmed BOS high-probability POI/OB candidate is sent "
        "to the LLM decision layer for immediate limit-order review."
        if use_llm
        else "Manual direct order: the confirmed BOS high-probability POI/OB candidate is used "
        "for immediate limit-order placement without LLM review."
    )
    analysis.update({
        "action": candidate_trade.get("action"),
        "setup": "manual_bos_poi_decision",
        "entry": candidate_trade.get("entry"),
        "stop_loss": candidate_trade.get("stop_loss"),
        "take_profit_1": candidate_trade.get("take_profit"),
        "take_profit_2": candidate_trade.get("take_profit"),
        "confidence": max(float(analysis.get("confidence") or 0), 0.65),
        "reasons": [manual_reason, *reasons],
    })
    analysis_payload["analysis"] = analysis
    analysis_payload["manual_decision_context"] = {
        "source": "completed_task_manual_button",
        "original_setup": original_setup,
        "candidate_trade": candidate_trade,
        "execution_mode": "llm_decision_layer_limit_order" if use_llm else "manual_direct_limit_order",
        "use_llm": use_llm,
        "sweep_gate_enabled": False,
        "instruction": (
            "Review candidate_trade for immediate limit-order placement. Do not wait for or require "
            "a liquidity sweep condition; only reject if risk, levels, or portfolio constraints are invalid."
            if use_llm
            else "Place candidate_trade immediately without LLM review. Do not wait for or require a liquidity sweep condition."
        ),
    }


def _poi_consumed_after_bos(analysis_payload: dict[str, Any]) -> dict[str, Any] | None:
    analysis = analysis_payload.get("analysis") or {}
    metadata = analysis.get("metadata") or {}
    candles = (analysis_payload.get("snapshot") or {}).get("candles") or []
    poi_low = _first_number(metadata.get("poi_ob_low"))
    poi_high = _first_number(metadata.get("poi_ob_high"))
    break_index = metadata.get("bos_break_index")
    if poi_low is None or poi_high is None or break_index is None:
        return None
    try:
        start_index = int(break_index) + 1
    except (TypeError, ValueError):
        return None
    zone_low = min(poi_low, poi_high)
    zone_high = max(poi_low, poi_high)
    for index in range(start_index, len(candles)):
        candle = candles[index]
        if not isinstance(candle, dict):
            continue
        candle_low = _first_number(candle.get("low"))
        candle_high = _first_number(candle.get("high"))
        if candle_low is None or candle_high is None:
            continue
        if candle_high >= zone_low and candle_low <= zone_high:
            return {
                "reason": "poi_ob_consumed_after_bos_completion",
                "poi_ob_low": poi_low,
                "poi_ob_high": poi_high,
                "poi_ob_index": metadata.get("poi_ob_index"),
                "bos_break_index": break_index,
                "consumed_index": index,
                "consumed_time": _format_candle_time_by_index(candles, index),
                "consumed_low": candle_low,
                "consumed_high": candle_high,
            }
    current_price = _first_number(metadata.get("current_price"))
    if current_price is not None and zone_low <= current_price <= zone_high:
        return {
            "reason": "poi_ob_consumed_by_current_price_after_bos_completion",
            "poi_ob_low": poi_low,
            "poi_ob_high": poi_high,
            "poi_ob_index": metadata.get("poi_ob_index"),
            "bos_break_index": break_index,
            "consumed_index": None,
            "consumed_time": None,
            "consumed_low": current_price,
            "consumed_high": current_price,
            "current_price": current_price,
        }
    return None


def _entry_filled(side: str, mark: float, entry_price: float | None) -> bool:
    if entry_price is None:
        return True
    if side == SignalAction.BUY.value:
        return mark <= entry_price
    if side == SignalAction.SELL.value:
        return mark >= entry_price
    return False


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
