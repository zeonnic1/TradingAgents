from __future__ import annotations

from tradingagents.crypto.tasks.task_handlers import BaseTaskHandler, CryptoBotRunHandler


class TaskFactory:
    """任务工厂。

    Factory Pattern 的作用是把“任务类型 -> 处理器”的选择集中管理。
    新增任务时只需要注册新的 Handler，不需要修改 API 层或 Celery Worker
    的核心分发代码。
    """

    _handlers: dict[str, type[BaseTaskHandler]] = {
        CryptoBotRunHandler.task_type: CryptoBotRunHandler,
    }

    @classmethod
    def create(cls, task_type: str) -> BaseTaskHandler:
        handler_cls = cls._handlers.get(task_type)
        if handler_cls is None:
            raise ValueError(f"Unsupported task type: {task_type}")
        return handler_cls()

    @classmethod
    def ensure_supported(cls, task_type: str) -> None:
        if task_type not in cls._handlers:
            raise ValueError(f"Unsupported task type: {task_type}")
