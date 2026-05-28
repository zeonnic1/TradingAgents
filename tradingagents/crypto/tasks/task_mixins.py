from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from tradingagents.crypto.managers.connect_manager import publish_task_status
from tradingagents.crypto.utils.ledger import append_task_log, update_task_record
from tradingagents.crypto.tasks.task_fsm import TaskStatus


class TaskLoggingMixin:
    """日志 Mixin。

    Mixin 只封装横切能力，不承载交易业务。Handler 继承后可以统一写入
    MySQL 日志表，避免核心处理流程里到处散落 append_task_log 调用。
    """

    def log(self, task_id: str, level: str, message: str, context: dict | None = None) -> None:
        append_task_log(task_id, level, message, context or {})


class TaskStateMixin:
    """状态更新 Mixin。

    所有状态更新都会进入 ledger.update_task_record，并由 FSM 校验合法性。
    这样 Worker、取消接口、失败处理共享同一套生命周期规则。
    """

    def set_status(
        self,
        task_id: str,
        status: TaskStatus,
        *,
        progress: int | None = None,
        result: dict | None = None,
        error: str | None = None,
        llm_log: dict | None = None,
        force: bool = False,
    ) -> None:
        update_task_record(
            task_id,
            status.value,
            result=result,
            error=error,
            progress=progress,
            llm_log=llm_log,
            enforce_fsm=not force,
        )


class RedisPublishMixin:
    """Redis Pub/Sub 推送 Mixin。

    Celery 与 FastAPI 运行在不同进程，不能直接调用 FastAPI 内存中的
    ConnectionManager。因此状态变化统一发布到 Redis，WebSocket 端点只负责订阅。
    """

    def publish(self, task_id: str, status: TaskStatus, **payload: Any) -> None:
        publish_task_status(
            task_id,
            {
                "task_id": task_id,
                "status": status.value,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                **payload,
            },
        )
