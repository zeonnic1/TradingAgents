from __future__ import annotations

import os

from celery import Celery

from .config import get_crypto_settings
from tradingagents.crypto.managers.connect_manager import publish_task_status
from tradingagents.crypto.utils.ledger import append_task_log, get_task_record, update_task_record
from .models import BotRunRequest
from tradingagents.crypto.tasks.task_factory import TaskFactory
from tradingagents.crypto.tasks.task_fsm import TaskStatus


settings = get_crypto_settings()

celery_app = Celery(
    "tradingagents_crypto",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    worker_pool=os.getenv("CELERY_WORKER_POOL", "solo" if os.name == "nt" else "prefork"),
)


@celery_app.task(name="tradingagents.crypto.dispatch_task")
def dispatch_task(task_type: str, payload: dict):
    """Worker 层统一入口。

    Celery 不直接写具体业务逻辑，只通过 TaskFactory 找到对应 Handler。
    这样后续增加“LLM复盘任务”“订单同步任务”等类型时，不需要改 Worker 主体。
    """

    task_id = getattr(dispatch_task.request, "id", None)
    if not task_id:
        raise RuntimeError("Celery task id is missing.")
    try:
        handler = TaskFactory.create(task_type)
        return handler.handle(task_id, payload)
    except Exception as exc:
        _fail_task(task_id, exc)
        raise


@celery_app.task(name="tradingagents.crypto.run_bot_once")
def run_bot_once_task(payload: dict):
    """兼容旧任务名。

    旧接口 /api/bot/run-async 仍可使用，但内部已经转给工厂化任务处理器。
    """

    task_id = getattr(run_bot_once_task.request, "id", None)
    if not task_id:
        raise RuntimeError("Celery task id is missing.")
    try:
        handler = TaskFactory.create(BotRunRequest(**payload).task_type)
        return handler.handle(task_id, payload)
    except Exception as exc:
        _fail_task(task_id, exc)
        raise


def _fail_task(task_id: str, exc: Exception) -> None:
    current = get_task_record(task_id)
    if current and current.status == TaskStatus.CANCELED.value:
        return
    try:
        update_task_record(task_id, TaskStatus.FAILED.value, error=str(exc), progress=100)
    except Exception:
        update_task_record(task_id, TaskStatus.FAILED.value, error=str(exc), progress=100, enforce_fsm=False)
    try:
        append_task_log(task_id, "error", "Task failed.", {"error": str(exc)})
    except Exception:
        pass
    publish_task_status(task_id, {
        "task_id": task_id,
        "status": TaskStatus.FAILED.value,
        "ready": True,
        "progress": 100,
        "error": str(exc),
    })
