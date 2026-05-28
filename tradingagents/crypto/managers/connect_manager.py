from __future__ import annotations

import json
from typing import Dict

from fastapi import WebSocket

from tradingagents.crypto.managers.redis_manager import create_pubsub, create_redis_client


class ConnectionManager:
    _instance: "ConnectionManager | None" = None

    def __new__(cls) -> "ConnectionManager":
        # 单例保证 FastAPI 进程内所有路由看到同一个连接表。
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance.active_connections = {}
        return cls._instance

    def __init__(self) -> None:
        if not hasattr(self, "active_connections"):
            self.active_connections: Dict[str, WebSocket] = {}

    async def connect(self, task_id: str, websocket: WebSocket) -> None:
        await websocket.accept()
        self.active_connections[task_id] = websocket

    def disconnect(self, task_id: str) -> None:
        self.active_connections.pop(task_id, None)

    async def send_personal_message(self, task_id: str, message: dict) -> None:
        websocket = self.active_connections.get(task_id)
        if websocket is None:
            return
        await websocket.send_json(message)


def task_status_channel(task_id: str) -> str:
    return f"task_status_{task_id}"


def publish_task_status(task_id: str, payload: dict) -> None:
    try:
        client = create_redis_client()
    except ImportError:
        return
    try:
        client.publish(task_status_channel(task_id), json.dumps(payload, ensure_ascii=False, default=str))
    finally:
        client.close()


async def create_redis_pubsub(task_id: str):
    return await create_pubsub(task_status_channel(task_id))
