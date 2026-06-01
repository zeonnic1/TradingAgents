from __future__ import annotations

from typing import Any

from tradingagents.crypto.utils.exchanges import get_exchange_client
from tradingagents.crypto.utils.ledger import get_task_record, list_orders, list_task_logs, list_task_records, performance_summary, record_order, update_task_record
from tradingagents.crypto.llm_reviewer import compact_llm_decision_payload, review_with_llm
from tradingagents.crypto.models import AnalyzeRequest, BalanceAsset, ExchangeBalance, MarketSnapshot, OrderRequest, OrderResponse, SignalAction
from tradingagents.crypto.services.candle_cache import CandleCache
from tradingagents.crypto.services.trade_proposal import build_candidate_trade
from tradingagents.crypto.strategys.strategy import analyze_trading_hub_setup
from tradingagents.crypto.config import get_crypto_settings


_candle_cache = CandleCache()
MAX_BOS_EXPANSION_LIMIT = 1000


def get_market_snapshot(request: AnalyzeRequest, *, refresh_full: bool = False) -> MarketSnapshot:
    return _candle_cache.get_snapshot(request, refresh_full=refresh_full)


def analyze_market(request: AnalyzeRequest):
    snapshot = get_market_snapshot(request)
    daily_snapshot = get_market_snapshot(_daily_request(request))
    analysis = analyze_trading_hub_setup(snapshot.candles, daily_candles=daily_snapshot.candles)
    sync_attempts = [_sync_attempt(request.limit, snapshot, analysis, expanded=False)]

    while _analysis_needs_more_bos_history(analysis):
        next_limit = _next_candle_limit(sync_attempts[-1]["limit"])
        if next_limit is None:
            break
        expanded_request = request.model_copy(update={"limit": next_limit})
        snapshot = get_market_snapshot(expanded_request, refresh_full=True)
        analysis = analyze_trading_hub_setup(snapshot.candles, daily_candles=daily_snapshot.candles)
        sync_attempts.append(_sync_attempt(next_limit, snapshot, analysis, expanded=True))

    analysis.metadata["candle_limit_used"] = sync_attempts[-1]["limit"]
    analysis.metadata["candle_count_used"] = sync_attempts[-1]["candles"]
    analysis.metadata["bos_history_expanded"] = len(sync_attempts) > 1
    return {
        "snapshot": snapshot.model_dump(mode="json"),
        "daily_snapshot": daily_snapshot.model_dump(mode="json"),
        "analysis": analysis.model_dump(mode="json"),
        "llm_decision": None,
        "candle_sync": {
            "expanded": len(sync_attempts) > 1,
            "attempts": sync_attempts,
            "max_limit": MAX_BOS_EXPANSION_LIMIT,
        },
    }


def llm_decision_for_request(request: AnalyzeRequest):
    analysis_payload = analyze_market(request)
    decision = review_with_llm(analysis_payload)
    payload = compact_llm_decision_payload(decision)
    analysis_payload["llm_decision"] = payload
    return {
        "llm_decision": payload,
    }


def llm_decision_for_task(task_id: str):
    task = get_task_record(task_id)
    if task is None:
        raise ValueError("Task not found.")
    if str(task.status).upper() in {"CANCELED", "FAILED"}:
        raise ValueError("LLM decision is not allowed for canceled or failed tasks.")
    request = AnalyzeRequest(
        exchange=task.exchange,
        symbol=task.symbol,
        timeframe=task.timeframe,
        limit=_task_result_limit(task.result or task.result_data),
    )
    analysis_payload = analyze_market(request)
    analysis_payload["candidate_trade"] = build_candidate_trade(analysis_payload, ignore_consumed=True)
    analysis_payload["llm_source"] = {
        "task_id": task_id,
        "mode": "current_market_refresh",
        "previous_status": task.status,
    }
    decision = review_with_llm(analysis_payload)
    decision_payload = compact_llm_decision_payload(decision)
    analysis_payload["llm_decision"] = decision_payload
    update_task_record(task_id, task.status, result=analysis_payload)
    return {
        "task_id": task_id,
        "llm_decision": decision_payload,
    }


def get_exchange_balance(exchange) -> ExchangeBalance:
    settings = get_crypto_settings()
    client = get_exchange_client(exchange)
    raw_balance = client.fetch_balance()
    assets = _extract_balance_assets(raw_balance)
    return ExchangeBalance(
        exchange=client.exchange,
        paper=settings.paper_trading,
        validated=not settings.paper_trading,
        assets=assets,
        message=(
            "Paper balance returned; live API keys were not used."
            if settings.paper_trading
            else "Exchange API credentials validated by fetching account balance."
        ),
    )


def create_order(request: OrderRequest) -> OrderResponse:
    client = get_exchange_client(request.exchange)
    account = get_exchange_balance(request.exchange)
    order = client.create_order(request)
    response = OrderResponse(
        exchange=request.exchange,
        paper=get_crypto_settings().paper_trading,
        order=order,
        account=account,
    )
    record_order(request, response)
    return response


def order_history(exchange: str | None = None, symbol: str | None = None):
    return [record.model_dump(mode="json") for record in list_orders(exchange, symbol)]


def get_performance(exchange: str | None = None, symbol: str | None = None):
    records = list_orders(exchange, symbol)
    mark_prices = {}
    for record in records:
        key = f"{record.exchange.value}:{record.symbol}"
        if key in mark_prices:
            continue
        try:
            ticker = get_exchange_client(record.exchange).fetch_ticker(record.symbol)
            mark_prices[key] = float(ticker.get("last") or ticker.get("close") or record.entry_price or 0)
        except Exception:
            mark_prices[key] = float(record.entry_price or 0)
    return performance_summary(records, mark_prices).model_dump(mode="json")


def task_center(limit: int = 50):
    tasks = [_public_task_record(task) for task in list_task_records(limit=limit)]
    logs = [log.model_dump(mode="json") for log in list_task_logs(limit=limit * 4)]
    performance = get_performance()
    watched_orders = []
    for row in performance.get("records", []):
        watched_orders.append({
            "id": row.get("id"),
            "exchange": row.get("exchange"),
            "symbol": row.get("symbol"),
            "side": row.get("side"),
            "status": row.get("status"),
            "paper": row.get("paper"),
            "entry_price": row.get("entry_price"),
            "mark_price": row.get("mark_price"),
            "pnl": row.get("pnl"),
            "roe": row.get("roe"),
            "leverage": row.get("leverage"),
            "watch_status": _order_watch_status(row),
        })
    return {
        "tasks": tasks,
        "logs": logs,
        "watched_orders": watched_orders,
    }


def _public_task_record(task) -> dict[str, Any]:
    row = task.model_dump(mode="json")
    row["result"] = _compact_analysis_payload(row.get("result") or row.get("result_data"))
    row["result_data"] = None
    return row


def _compact_analysis_payload(payload: dict | None) -> dict | None:
    if not payload:
        return None
    compact = dict(payload)
    for key in ("snapshot", "daily_snapshot"):
        snapshot = compact.get(key)
        if isinstance(snapshot, dict):
            candles = snapshot.get("candles") or []
            compact[key] = {
                "exchange": snapshot.get("exchange"),
                "symbol": snapshot.get("symbol"),
                "timeframe": snapshot.get("timeframe"),
                "candle_count": len(candles),
                "latest": candles[-1] if candles else None,
            }
    if isinstance(compact.get("llm_decision"), dict):
        compact["llm_decision"] = {
            "provider": compact["llm_decision"].get("provider"),
            "model": compact["llm_decision"].get("model"),
            "eligible": compact["llm_decision"].get("eligible"),
            "decision": compact["llm_decision"].get("decision"),
            "task_conclusion": compact["llm_decision"].get("task_conclusion"),
        }
    return compact


def run_bot_once(request):
    analysis_payload = analyze_market(request)
    analysis = analysis_payload["analysis"]
    if (request.execute or getattr(request, "transaction", False)) and request.amount > 0 and analysis["action"] in {
        SignalAction.BUY.value,
        SignalAction.SELL.value,
    }:
        order = create_order(
            OrderRequest(
                exchange=request.exchange,
                symbol=request.symbol,
                side=analysis["action"],
                amount=request.amount,
                leverage=request.leverage,
            )
        )
        analysis_payload["order"] = order.model_dump(mode="json")
        analysis_payload["account"] = order.account.model_dump(mode="json") if order.account else None
    else:
        analysis_payload["order"] = None
        analysis_payload["account"] = None
    return analysis_payload


def _order_watch_status(row: dict) -> str:
    status = str(row.get("status") or "").lower()
    if "closed" in status or "canceled" in status or "rejected" in status:
        return "finished"
    pnl = float(row.get("pnl") or 0)
    if pnl > 0:
        return "profit"
    if pnl < 0:
        return "drawdown"
    return "watching"


def _extract_balance_assets(raw_balance: dict) -> list[BalanceAsset]:
    free = raw_balance.get("free") or {}
    used = raw_balance.get("used") or {}
    total = raw_balance.get("total") or {}
    names = sorted(set(free) | set(used) | set(total))
    assets = []
    for name in names:
        total_value = _to_float(total.get(name))
        free_value = _to_float(free.get(name))
        used_value = _to_float(used.get(name))
        if total_value == 0 and free_value == 0 and used_value == 0:
            continue
        assets.append(
            BalanceAsset(
                asset=str(name),
                free=free_value,
                used=used_value,
                total=total_value if total_value else free_value + used_value,
            )
        )
    return assets[:50]


def _analysis_needs_more_bos_history(analysis) -> bool:
    metadata = analysis.metadata or {}
    return metadata.get("bos_direction") is None


def _daily_request(request: AnalyzeRequest) -> AnalyzeRequest:
    return request.model_copy(update={"timeframe": "1d", "limit": 60})


def _task_result_limit(result: dict | None) -> int:
    candles = ((result or {}).get("snapshot") or {}).get("candles") or []
    if candles:
        return max(60, min(len(candles), MAX_BOS_EXPANSION_LIMIT))
    return 300


def _next_candle_limit(current_limit: int) -> int | None:
    if current_limit >= MAX_BOS_EXPANSION_LIMIT:
        return None
    return min(current_limit * 2, MAX_BOS_EXPANSION_LIMIT)


def _sync_attempt(limit: int, snapshot: MarketSnapshot, analysis, *, expanded: bool) -> dict[str, Any]:
    return {
        "limit": limit,
        "candles": len(snapshot.candles),
        "expanded": expanded,
        "bos_found": (analysis.metadata or {}).get("bos_direction") is not None,
    }


def _to_float(value) -> float:
    if value in (None, ""):
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
