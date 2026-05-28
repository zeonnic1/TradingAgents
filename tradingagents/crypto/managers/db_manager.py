from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from tradingagents.crypto.config import get_crypto_settings


_engine = None
_session_factory = None


def get_engine():
    """MySQL engine 单例入口。

    ORM Model 不放在 manager 层，避免连接管理和表结构互相耦合。
    """

    global _engine
    if _engine is None:
        settings = get_crypto_settings()
        if not settings.database_url.startswith("mysql"):
            raise RuntimeError("CRYPTO_DATABASE_URL must point to a MySQL database.")
        _engine = create_engine(settings.database_url, pool_pre_ping=True, future=True)
    return _engine


def get_session_factory():
    global _session_factory
    if _session_factory is None:
        _session_factory = sessionmaker(bind=get_engine(), expire_on_commit=False, future=True)
    return _session_factory


def reset_db_manager_for_tests() -> None:
    global _engine, _session_factory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _session_factory = None
