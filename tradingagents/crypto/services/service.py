from __future__ import annotations

from tradingagents.crypto.utils.exchanges import get_exchange_client
from tradingagents.crypto.utils.ledger import get_task_record, list_orders, list_task_logs, list_task_records, performance_summary, record_order, update_task_record
from tradingagents.crypto.llm_reviewer import review_with_llm
from tradingagents.crypto.models import AnalyzeRequest, BalanceAsset, ExchangeBalance, MarketSnapshot, OrderRequest, OrderResponse, SignalAction
from tradingagents.crypto.services.candle_cache import CandleCache
from tradingagents.crypto.strategys.strategy import analyze_trading_hub_setup
from tradingagents.crypto.config import get_crypto_settings


_candle_cache = CandleCache()


def get_market_snapshot(request: AnalyzeRequest) -> MarketSnapshot:
    return _candle_cache.get_snapshot(request)


def analyze_market(request: AnalyzeRequest):
    snapshot = get_market_snapshot(request)
    analysis = analyze_trading_hub_setup(snapshot.candles)
    return {
        "snapshot": snapshot.model_dump(mode="json"),
        "analysis": analysis.model_dump(mode="json"),
        "llm_decision": None,
    }


def llm_decision_for_request(request: AnalyzeRequest):
    analysis_payload = analyze_market(request)
    decision = review_with_llm(analysis_payload)
    payload = decision.model_dump(mode="json")
    analysis_payload["llm_decision"] = payload
    return {
        "analysis_payload": analysis_payload,
        "llm_decision": payload,
    }


def llm_decision_for_task(task_id: str):
    task = get_task_record(task_id)
    if task is None:
        raise ValueError("Task not found.")
    if str(task.status).upper() in {"CANCELED", "FAILED"}:
        raise ValueError("LLM decision is not allowed for canceled or failed tasks.")
    if not task.result:
        request = AnalyzeRequest(
            exchange=task.exchange,
            symbol=task.symbol,
            timeframe=task.timeframe,
        )
        analysis_payload = analyze_market(request)
    else:
        analysis_payload = dict(task.result)
    decision = review_with_llm(analysis_payload)
    decision_payload = decision.model_dump(mode="json")
    analysis_payload["llm_decision"] = decision_payload
    update_task_record(task_id, task.status, result=analysis_payload)
    return {
        "task_id": task_id,
        "analysis_payload": analysis_payload,
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
    tasks = [task.model_dump(mode="json") for task in list_task_records(limit=limit)]
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


def _to_float(value) -> float:
    if value in (None, ""):
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
