from __future__ import annotations

from uuid import uuid4

from fastapi import HTTPException

from tradingagents.crypto.managers.connect_manager import publish_task_status
from tradingagents.crypto.utils.ledger import append_task_log, create_task_record, delete_task_record, get_task_record, update_task_record
from tradingagents.crypto.models import TaskResponse, TaskSubmitRequest
from tradingagents.crypto.tasks.task_factory import TaskFactory
from tradingagents.crypto.tasks.task_fsm import TaskStatus, state_machine


def submit_task(request: TaskSubmitRequest) -> TaskResponse:
    """Service 层任务提交入口。

    API 层只负责接收请求；这里负责初始化 PENDING 状态、校验任务类型、
    分发 Celery，并立即返回 task_id。
    """

    TaskFactory.ensure_supported(request.task_type)
    task_id = str(uuid4())
    create_task_record(task_id, request)
    append_task_log(task_id, "info", "Task initialized as PENDING.", {
        "task_type": request.task_type,
        "exchange": request.exchange.value,
        "symbol": request.symbol,
        "transaction": bool(request.transaction or request.execute),
        "use_llm_decision": request.use_llm_decision,
    })
    publish_task_status(task_id, {
        "task_id": task_id,
        "status": TaskStatus.PENDING.value,
        "ready": False,
        "progress": 0,
    })
    try:
        from tradingagents.crypto.celery import dispatch_task

        task = dispatch_task.apply_async(args=[request.task_type, request.model_dump(mode="json")], task_id=task_id)
    except Exception as exc:
        update_task_record(task_id, TaskStatus.FAILED.value, error=str(exc), progress=100)
        append_task_log(task_id, "error", "Task enqueue failed.", {"error": str(exc)})
        publish_task_status(task_id, {
            "task_id": task_id,
            "status": TaskStatus.FAILED.value,
            "ready": True,
            "error": str(exc),
        })
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return TaskResponse(task_id=task.id, status=TaskStatus.PENDING.value)


def cancel_task(task_id: str, terminate: bool = False) -> TaskResponse:
    record = get_task_record(task_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Task not found.")
    if not state_machine.can_transition(record.status, TaskStatus.CANCELED):
        append_task_log(task_id, "warn", f"Cancel ignored; task is already {record.status}.")
        return TaskResponse(task_id=task_id, status=record.status)
    try:
        from tradingagents.crypto.celery import celery_app
    except ImportError as exc:
        raise HTTPException(status_code=503, detail="Celery is not installed.") from exc

    celery_app.control.revoke(task_id, terminate=terminate)
    update_task_record(task_id, TaskStatus.CANCELED.value, progress=100)
    append_task_log(task_id, "warn", "Task cancellation requested.", {"terminate": terminate})
    publish_task_status(task_id, {
        "task_id": task_id,
        "status": TaskStatus.CANCELED.value,
        "ready": True,
        "progress": 100,
    })
    return TaskResponse(task_id=task_id, status=TaskStatus.CANCELED.value)


def delete_task(task_id: str) -> TaskResponse:
    record = get_task_record(task_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Task not found.")
    status = TaskStatus.normalize(record.status)
    if status not in {TaskStatus.COMPLETED, TaskStatus.CANCELED, TaskStatus.FAILED}:
        raise HTTPException(status_code=409, detail="Only terminal tasks can be deleted. Cancel the task first.")
    deleted = delete_task_record(task_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Task not found.")
    publish_task_status(task_id, {
        "task_id": task_id,
        "status": "DELETED",
        "ready": True,
        "progress": 100,
    })
    return TaskResponse(task_id=task_id, status="DELETED")
