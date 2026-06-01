from __future__ import annotations

import json
from importlib.resources import files

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .config import get_crypto_settings
from tradingagents.crypto.managers.connect_manager import ConnectionManager, create_redis_pubsub
from tradingagents.crypto.utils.ledger import append_task_log, get_task_record, list_task_logs, update_task_record
from .models import AnalyzeRequest, BotRunRequest, OrderRequest, TaskResponse, TaskSubmitRequest
from tradingagents.crypto.services.service import (
    analyze_market,
    create_order,
    get_exchange_balance,
    get_market_snapshot,
    get_performance,
    llm_decision_for_request,
    llm_decision_for_task,
    order_history,
    run_bot_once,
    task_center,
)
from tradingagents.crypto.tasks.task_service import cancel_task as cancel_task_service
from tradingagents.crypto.tasks.task_service import delete_task as delete_task_service
from tradingagents.crypto.tasks.task_service import submit_task as submit_task_service
from tradingagents.crypto.tasks.task_service import trigger_manual_decision_order


manager = ConnectionManager()

app = FastAPI(
    title="TradingAgents Crypto Hub",
    version="0.1.0",
    description="Crypto-only Trading Hub platform with Binance, OKX, and Bybit exchange adapters.",
)

static_dir = files("tradingagents.crypto").joinpath("static")
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


@app.get("/")
def index():
    return FileResponse(str(static_dir.joinpath("index.html")))


@app.get("/api/health")
def health():
    settings = get_crypto_settings()
    return {
        "status": "ok",
        "paper_trading": settings.paper_trading,
        "default_exchange": settings.default_exchange.value,
        "default_symbol": settings.default_symbol,
        "default_timeframe": settings.default_timeframe,
    }


@app.post("/api/market/snapshot")
def market_snapshot(request: AnalyzeRequest):
    try:
        return get_market_snapshot(request)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/analyze")
def analyze(request: AnalyzeRequest):
    try:
        return analyze_market(request)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/llm/decision")
def llm_decision(request: AnalyzeRequest):
    try:
        return llm_decision_for_request(request)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/orders")
def orders(request: OrderRequest):
    try:
        return create_order(request)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/account/balance")
def account_balance(exchange: str):
    try:
        return get_exchange_balance(exchange)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/orders/history")
def orders_history(exchange: str | None = None, symbol: str | None = None):
    return {"orders": order_history(exchange, symbol)}


@app.get("/api/performance")
def performance(exchange: str | None = None, symbol: str | None = None):
    return get_performance(exchange, symbol)


@app.get("/api/tasks")
def tasks(limit: int = 50):
    try:
        return task_center(limit=limit)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/tasks/logs")
def task_logs(task_id: str | None = None, limit: int = 200):
    try:
        return {"logs": [log.model_dump(mode="json") for log in list_task_logs(task_id=task_id, limit=limit)]}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/bot/run")
def bot_run(request: BotRunRequest):
    try:
        return run_bot_once(request)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/bot/run-async", response_model=TaskResponse)
def bot_run_async(request: TaskSubmitRequest):
    return submit_task_service(request)


@app.post("/api/tasks", response_model=TaskResponse)
def submit_task_api(request: TaskSubmitRequest):
    return submit_task_service(request)


@app.post("/tasks", response_model=TaskResponse)
def submit_task(request: TaskSubmitRequest):
    return submit_task_service(request)


@app.get("/api/tasks/{task_id}")
def task_status(task_id: str):
    try:
        from tradingagents.crypto.celery import run_bot_once_task
    except ImportError as exc:
        raise HTTPException(
            status_code=503,
            detail="Celery is not installed. Install project dependencies with `pip install .`.",
        ) from exc
    result = run_bot_once_task.AsyncResult(task_id)
    payload = {
        "task_id": task_id,
        "status": result.status,
        "ready": result.ready(),
    }
    if result.ready():
        payload["result"] = result.result
        if result.successful():
            update_task_record(task_id, "COMPLETED", result=result.result, progress=100, enforce_fsm=False)
        elif result.failed():
            update_task_record(task_id, "FAILED", error=str(result.result), progress=100, enforce_fsm=False)
    return payload


@app.post("/api/tasks/{task_id}/llm-decision")
def task_llm_decision(task_id: str):
    try:
        result = llm_decision_for_task(task_id)
        append_task_log(task_id, "info", "LLM decision completed.", {
            "approved": result["llm_decision"]["decision"]["approved"],
            "action": result["llm_decision"]["decision"]["action"],
            "task_conclusion": result["llm_decision"].get("task_conclusion"),
            "prompt_messages": result["llm_decision"].get("prompt_messages"),
        })
        return result
    except Exception as exc:
        append_task_log(task_id, "error", "LLM decision failed.", {"error": str(exc)})
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/tasks/{task_id}/decision-order", response_model=TaskResponse)
def task_manual_decision_order(task_id: str):
    return trigger_manual_decision_order(task_id, use_llm=False)


@app.post("/api/tasks/{task_id}/llm-decision-order", response_model=TaskResponse)
def task_llm_decision_order(task_id: str):
    return trigger_manual_decision_order(task_id, use_llm=True)


@app.post("/api/tasks/{task_id}/cancel", response_model=TaskResponse)
def cancel_task(task_id: str, terminate: bool = False):
    return cancel_task_service(task_id, terminate=terminate)


@app.post("/tasks/{task_id}/cancel", response_model=TaskResponse)
def cancel_task_public(task_id: str, terminate: bool = False):
    return cancel_task_service(task_id, terminate=terminate)


@app.delete("/api/tasks/{task_id}", response_model=TaskResponse)
def delete_task_api(task_id: str):
    return delete_task_service(task_id)


@app.delete("/tasks/{task_id}", response_model=TaskResponse)
def delete_task(task_id: str):
    return delete_task_service(task_id)


@app.websocket("/ws/{task_id}")
async def task_status_websocket(websocket: WebSocket, task_id: str):
    await manager.connect(task_id, websocket)
    redis_client = None
    pubsub = None
    try:
        redis_client, pubsub = await create_redis_pubsub(task_id)
        await manager.send_personal_message(task_id, {
            "task_id": task_id,
            "status": "subscribed",
            "channel": f"task_status_{task_id}",
        })
        current = get_task_record(task_id)
        if current is not None:
            await manager.send_personal_message(task_id, current.model_dump(mode="json"))
        async for message in pubsub.listen():
            if message.get("type") != "message":
                continue
            data = message.get("data")
            if isinstance(data, bytes):
                data = data.decode("utf-8")
            payload = json.loads(data) if isinstance(data, str) else data
            await manager.send_personal_message(task_id, payload)
            if str(payload.get("status", "")).upper() in {"COMPLETED", "CANCELED", "FAILED", "DELETED"}:
                await websocket.close()
                break
    except WebSocketDisconnect:
        pass
    finally:
        manager.disconnect(task_id)
        if pubsub is not None:
            try:
                await pubsub.unsubscribe(f"task_status_{task_id}")
                await pubsub.close()
            except Exception:
                pass
        if redis_client is not None:
            try:
                await redis_client.close()
            except Exception:
                pass
