from __future__ import annotations

from tradingagents.crypto.config import get_crypto_settings


def get_redis_url() -> str:
    return get_crypto_settings().celery_broker_url


def create_redis_client():
    import redis

    return redis.Redis.from_url(get_redis_url())


async def create_async_redis_client():
    import redis.asyncio as redis

    return redis.Redis.from_url(get_redis_url())


async def create_pubsub(channel: str):
    client = await create_async_redis_client()
    pubsub = client.pubsub()
    await pubsub.subscribe(channel)
    return client, pubsub
