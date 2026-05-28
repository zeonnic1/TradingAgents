from __future__ import annotations

from enum import Enum


class TaskStatus(str, Enum):
    """任务生命周期的唯一状态枚举。

    这里使用有限状态机（FSM）约束状态流转，避免任务被业务代码随意改成
    不合理状态。例如任务必须先从 PENDING 进入 ORDERED，不能直接跳到
    COMPLETED；终态任务也不能再被修改。
    """

    PENDING = "PENDING"
    ORDERED = "ORDERED"
    COMPLETED = "COMPLETED"
    CANCELED = "CANCELED"
    FAILED = "FAILED"

    @classmethod
    def normalize(cls, value: str | "TaskStatus") -> "TaskStatus":
        if isinstance(value, TaskStatus):
            return value
        aliases = {
            "queued": cls.PENDING,
            "pending": cls.PENDING,
            "running": cls.ORDERED,
            "ordered": cls.ORDERED,
            "completed": cls.COMPLETED,
            "canceled": cls.CANCELED,
            "cancelled": cls.CANCELED,
            "failed": cls.FAILED,
        }
        normalized = aliases.get(str(value).strip().lower())
        if normalized is None:
            return cls(str(value).strip().upper())
        return normalized


class InvalidTaskTransition(ValueError):
    pass


class StateMachine:
    """集中校验任务状态流转。

    合法路径：
    - PENDING -> ORDERED：任务被 worker 接单并开始处理
    - PENDING -> CANCELED/FAILED：任务尚未执行时取消或提交失败
    - ORDERED -> COMPLETED/FAILED/CANCELED：处理中任务进入终态
    - 终态不可再流转，保证结果不会被后续异步回写覆盖
    """

    transitions = {
        TaskStatus.PENDING: {TaskStatus.ORDERED, TaskStatus.CANCELED, TaskStatus.FAILED},
        TaskStatus.ORDERED: {TaskStatus.COMPLETED, TaskStatus.CANCELED, TaskStatus.FAILED},
        TaskStatus.COMPLETED: set(),
        TaskStatus.CANCELED: set(),
        TaskStatus.FAILED: set(),
    }

    def can_transition(self, current: str | TaskStatus, target: str | TaskStatus) -> bool:
        current_status = TaskStatus.normalize(current)
        target_status = TaskStatus.normalize(target)
        if current_status == target_status:
            return True
        return target_status in self.transitions[current_status]

    def assert_transition(self, current: str | TaskStatus, target: str | TaskStatus) -> None:
        if not self.can_transition(current, target):
            raise InvalidTaskTransition(
                f"Illegal task status transition: {TaskStatus.normalize(current).value} -> "
                f"{TaskStatus.normalize(target).value}"
            )


state_machine = StateMachine()
