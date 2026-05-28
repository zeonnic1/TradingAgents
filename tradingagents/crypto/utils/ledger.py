from __future__ import annotations

from datetime import datetime, timezone
from typing import List, Optional
from uuid import uuid4

from sqlalchemy import JSON, Boolean, DateTime, Float, Integer, String, delete, inspect, select, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from tradingagents.crypto.managers.db_manager import get_engine, get_session_factory, reset_db_manager_for_tests
from tradingagents.crypto.models import (
    ExchangeName,
    OrderRecord,
    OrderRequest,
    OrderResponse,
    OrderSide,
    OrderType,
    PerformanceSummary,
    TaskLogRecord,
    TaskRecord,
)
from tradingagents.crypto.tasks.task_fsm import TaskStatus, state_machine


class Base(DeclarativeBase):
    pass


class OrderRow(Base):
    __tablename__ = "crypto_orders"

    id: Mapped[str] = mapped_column(String(96), primary_key=True)
    exchange: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    symbol: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    side: Mapped[str] = mapped_column(String(16), nullable=False)
    type: Mapped[str] = mapped_column(String(16), nullable=False)
    amount: Mapped[float] = mapped_column(Float, nullable=False)
    entry_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    leverage: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    paper: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    status: Mapped[str] = mapped_column(String(64), nullable=False, default="created")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True, nullable=False)
    raw_order: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)


class TaskRow(Base):
    __tablename__ = "crypto_tasks"

    id: Mapped[str] = mapped_column("task_id", String(96), primary_key=True)
    status: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    progress: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    exchange: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    symbol: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    timeframe: Mapped[str] = mapped_column(String(16), nullable=False)
    execute: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    use_llm_decision: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    amount: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    leverage: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True, nullable=False)
    result: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    result_data: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    llm_logs: Mapped[Optional[list]] = mapped_column(JSON, nullable=True, default=list)
    error: Mapped[Optional[str]] = mapped_column(String(2048), nullable=True)


class TaskLogRow(Base):
    __tablename__ = "crypto_task_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(String(96), index=True, nullable=False)
    level: Mapped[str] = mapped_column(String(16), nullable=False, default="info")
    message: Mapped[str] = mapped_column(String(1024), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True, nullable=False)
    context: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)


def init_db() -> None:
    Base.metadata.create_all(get_engine())
    _ensure_task_columns()


def record_order(request: OrderRequest, response: OrderResponse) -> OrderRecord:
    init_db()
    raw = response.order
    entry_price = _resolve_entry_price(raw, request.price)
    record = OrderRecord(
        id=str(raw.get("id") or uuid4()),
        exchange=request.exchange,
        symbol=request.symbol,
        side=request.side,
        type=request.type,
        amount=float(raw.get("amount") or (raw.get("info") or {}).get("contract_amount") or request.amount),
        entry_price=entry_price,
        leverage=request.leverage,
        paper=response.paper,
        status=str(raw.get("status") or "created"),
        created_at=_resolve_created_at(raw).isoformat(),
        raw_order=raw,
    )
    row = _record_to_row(record)
    with get_session_factory()() as session:
        session.merge(row)
        session.commit()
    return record


def list_orders(exchange: Optional[str] = None, symbol: Optional[str] = None) -> List[OrderRecord]:
    init_db()
    stmt = select(OrderRow).order_by(OrderRow.created_at.desc())
    if exchange:
        stmt = stmt.where(OrderRow.exchange == exchange)
    if symbol:
        stmt = stmt.where(OrderRow.symbol == symbol.upper())
    with get_session_factory()() as session:
        return [_row_to_record(row) for row in session.scalars(stmt).all()]


def update_order_status(order_id: str, status: str, raw_patch: Optional[dict] = None) -> None:
    init_db()
    with get_session_factory()() as session:
        row = session.get(OrderRow, order_id)
        if row is None:
            return
        row.status = status
        if raw_patch:
            raw_order = dict(row.raw_order or {})
            raw_order.update(raw_patch)
            row.raw_order = raw_order
        session.commit()


def performance_summary(records: List[OrderRecord], mark_prices: dict[str, float]) -> PerformanceSummary:
    rows = []
    notional = 0.0
    margin_used = 0.0
    unrealized_pnl = 0.0

    for record in records:
        if record.entry_price is None:
            row = record.model_dump(mode="json")
            row.update({"mark_price": None, "pnl": 0.0, "roe": 0.0, "margin": 0.0})
            rows.append(row)
            continue

        mark = mark_prices.get(f"{record.exchange.value}:{record.symbol}", record.entry_price)
        direction = 1 if record.side == OrderSide.BUY else -1
        pnl = (mark - record.entry_price) * record.amount * direction
        order_notional = record.entry_price * record.amount
        margin = order_notional / max(record.leverage, 1)
        roe = pnl / margin if margin else 0.0

        is_closed = _is_closed_order(record.status)
        notional += order_notional
        if not is_closed:
            margin_used += margin
            unrealized_pnl += pnl
        row = record.model_dump(mode="json")
        row.update({
            "mark_price": mark,
            "pnl": round(pnl, 8),
            "roe": round(roe, 6),
            "margin": round(margin, 8),
        })
        rows.append(row)

    total_roe = unrealized_pnl / margin_used if margin_used else 0.0
    return PerformanceSummary(
        total_orders=len(records),
        open_orders=sum(0 if _is_closed_order(record.status) else 1 for record in records),
        closed_orders=sum(1 if _is_closed_order(record.status) else 0 for record in records),
        notional=round(notional, 8),
        margin_used=round(margin_used, 8),
        unrealized_pnl=round(unrealized_pnl, 8),
        unrealized_roe=round(total_roe, 6),
        records=rows,
    )


def clear_orders() -> None:
    init_db()
    with get_session_factory()() as session:
        session.execute(delete(OrderRow))
        session.commit()


def create_task_record(task_id: str, request) -> TaskRecord:
    init_db()
    now = datetime.now(timezone.utc)
    record = TaskRecord(
        id=task_id,
        status=TaskStatus.PENDING.value,
        progress=0,
        exchange=request.exchange,
        symbol=request.symbol,
        timeframe=request.timeframe,
        execute=bool(getattr(request, "transaction", False) or request.execute),
        use_llm_decision=bool(getattr(request, "use_llm_decision", True)),
        amount=request.amount,
        leverage=request.leverage,
        created_at=now.isoformat(),
        updated_at=now.isoformat(),
    )
    with get_session_factory()() as session:
        session.merge(_task_to_row(record))
        session.commit()
    return record


def update_task_record(
    task_id: str,
    status: str,
    result: Optional[dict] = None,
    error: Optional[str] = None,
    progress: Optional[int] = None,
    llm_log: Optional[dict] = None,
    enforce_fsm: bool = True,
) -> None:
    init_db()
    with get_session_factory()() as session:
        row = session.get(TaskRow, task_id)
        if row is None:
            return
        next_status = TaskStatus.normalize(status).value
        if enforce_fsm:
            state_machine.assert_transition(row.status, next_status)
        row.status = next_status
        row.updated_at = datetime.now(timezone.utc)
        if progress is not None:
            row.progress = max(0, min(int(progress), 100))
        if result is not None:
            row.result = result
            row.result_data = result
        if error is not None:
            row.error = error
        if llm_log is not None:
            row.llm_logs = list(row.llm_logs or []) + [llm_log]
        session.commit()


def get_task_record(task_id: str) -> Optional[TaskRecord]:
    init_db()
    with get_session_factory()() as session:
        row = session.get(TaskRow, task_id)
        return _row_to_task(row) if row is not None else None


def append_task_log(
    task_id: str,
    level: str,
    message: str,
    context: Optional[dict] = None,
) -> TaskLogRecord:
    init_db()
    now = datetime.now(timezone.utc)
    row = TaskLogRow(
        task_id=task_id,
        level=level,
        message=message,
        created_at=now,
        context=context or {},
    )
    with get_session_factory()() as session:
        session.add(row)
        session.commit()
        session.refresh(row)
        return _row_to_task_log(row)


def list_task_records(limit: int = 50) -> List[TaskRecord]:
    init_db()
    stmt = select(TaskRow).order_by(TaskRow.created_at.desc()).limit(limit)
    with get_session_factory()() as session:
        return [_row_to_task(row) for row in session.scalars(stmt).all()]


def list_task_logs(task_id: Optional[str] = None, limit: int = 200) -> List[TaskLogRecord]:
    init_db()
    stmt = select(TaskLogRow)
    if task_id:
        stmt = stmt.where(TaskLogRow.task_id == task_id)
    stmt = stmt.order_by(TaskLogRow.created_at.desc()).limit(limit)
    with get_session_factory()() as session:
        return [_row_to_task_log(row) for row in session.scalars(stmt).all()]


def delete_task_record(task_id: str) -> bool:
    init_db()
    with get_session_factory()() as session:
        row = session.get(TaskRow, task_id)
        if row is None:
            return False
        session.execute(delete(TaskLogRow).where(TaskLogRow.task_id == task_id))
        session.delete(row)
        session.commit()
        return True


def reset_engine_for_tests() -> None:
    reset_db_manager_for_tests()


def _record_to_row(record: OrderRecord) -> OrderRow:
    return OrderRow(
        id=record.id,
        exchange=record.exchange.value,
        symbol=record.symbol,
        side=record.side.value,
        type=record.type.value,
        amount=record.amount,
        entry_price=record.entry_price,
        leverage=record.leverage,
        paper=record.paper,
        status=record.status,
        created_at=datetime.fromisoformat(record.created_at),
        raw_order=record.raw_order,
    )


def _row_to_record(row: OrderRow) -> OrderRecord:
    created_at = row.created_at
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    return OrderRecord(
        id=row.id,
        exchange=ExchangeName(row.exchange),
        symbol=row.symbol,
        side=OrderSide(row.side),
        type=OrderType(row.type),
        amount=row.amount,
        entry_price=row.entry_price,
        leverage=row.leverage,
        paper=row.paper,
        status=row.status,
        created_at=created_at.isoformat(),
        raw_order=row.raw_order or {},
    )


def _task_to_row(record: TaskRecord) -> TaskRow:
    return TaskRow(
        id=record.id,
        status=TaskStatus.normalize(record.status).value,
        progress=record.progress,
        exchange=record.exchange.value,
        symbol=record.symbol,
        timeframe=record.timeframe,
        execute=record.execute,
        use_llm_decision=record.use_llm_decision,
        amount=record.amount,
        leverage=record.leverage,
        created_at=datetime.fromisoformat(record.created_at),
        updated_at=datetime.fromisoformat(record.updated_at),
        result=record.result,
        result_data=record.result_data or record.result,
        llm_logs=record.llm_logs,
        error=record.error,
    )


def _row_to_task(row: TaskRow) -> TaskRecord:
    created_at = row.created_at
    updated_at = row.updated_at
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    if updated_at.tzinfo is None:
        updated_at = updated_at.replace(tzinfo=timezone.utc)
    return TaskRecord(
        id=row.id,
        status=row.status,
        progress=row.progress,
        exchange=ExchangeName(row.exchange),
        symbol=row.symbol,
        timeframe=row.timeframe,
        execute=row.execute,
        use_llm_decision=row.use_llm_decision,
        amount=row.amount,
        leverage=row.leverage,
        created_at=created_at.isoformat(),
        updated_at=updated_at.isoformat(),
        result=row.result_data or row.result,
        result_data=row.result_data or row.result,
        llm_logs=row.llm_logs or [],
        error=row.error,
    )


def _ensure_task_columns() -> None:
    """为本地开发环境补齐新增列。

    SQLAlchemy 的 create_all 不会修改已存在的表。这里做一个轻量级兼容：
    如果用户之前已经启动过旧版 MySQL 表，就自动补齐 progress、llm_logs、
    result_data 等列，避免手动删库。
    """
    engine = get_engine()
    inspector = inspect(engine)
    if not inspector.has_table("crypto_tasks"):
        return
    columns = {column["name"] for column in inspector.get_columns("crypto_tasks")}
    statements = []
    if "progress" not in columns:
        statements.append("ALTER TABLE crypto_tasks ADD COLUMN progress INT NOT NULL DEFAULT 0")
    if "task_id" not in columns and "id" in columns:
        statements.append("ALTER TABLE crypto_tasks CHANGE COLUMN id task_id VARCHAR(96) NOT NULL")
    if "llm_logs" not in columns:
        statements.append("ALTER TABLE crypto_tasks ADD COLUMN llm_logs JSON NULL")
    if "result_data" not in columns:
        statements.append("ALTER TABLE crypto_tasks ADD COLUMN result_data JSON NULL")
    if "use_llm_decision" not in columns:
        statements.append("ALTER TABLE crypto_tasks ADD COLUMN use_llm_decision BOOLEAN NOT NULL DEFAULT TRUE")
    with engine.begin() as conn:
        for statement in statements:
            conn.execute(text(statement))


def _row_to_task_log(row: TaskLogRow) -> TaskLogRecord:
    created_at = row.created_at
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    return TaskLogRecord(
        id=row.id,
        task_id=row.task_id,
        level=row.level,
        message=row.message,
        created_at=created_at.isoformat(),
        context=row.context or {},
    )


def _resolve_entry_price(raw: dict, fallback: Optional[float]) -> Optional[float]:
    for key in ("average", "price"):
        value = raw.get(key)
        if value not in (None, ""):
            return float(value)
    return fallback


def _resolve_created_at(raw: dict) -> datetime:
    value = raw.get("datetime")
    if value:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return datetime.now(timezone.utc)


def _is_closed_order(status: str) -> bool:
    lowered = str(status or "").lower()
    return any(token in lowered for token in ("closed", "canceled", "cancelled", "rejected", "take_profit", "stop_loss"))
