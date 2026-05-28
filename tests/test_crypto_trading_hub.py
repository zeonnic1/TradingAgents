from tradingagents.crypto.utils.exchanges import PaperExchangeClient, normalize_exchange
from tradingagents.crypto.managers.connect_manager import ConnectionManager, task_status_channel
from tradingagents.crypto.llm_reviewer import create_rule_payload, is_llm_decision_eligible
from tradingagents.crypto.utils.ledger import _row_to_task, _task_to_row, performance_summary
from tradingagents.crypto.tasks.task_handlers import CryptoBotRunHandler
from tradingagents.crypto.tasks.task_factory import TaskFactory
from tradingagents.crypto.tasks.task_fsm import InvalidTaskTransition, TaskStatus, state_machine
from tradingagents.crypto.models import (
    BalanceAsset,
    Candle,
    ExchangeBalance,
    ExchangeName,
    AnalyzeRequest,
    OrderRecord,
    OrderRequest,
    OrderSide,
    SignalAction,
    TaskLogRecord,
    TaskRecord,
    TradingHubAnalysis,
)
from tradingagents.crypto.services.candle_cache import CandleCache
from tradingagents.crypto.strategys.strategy import analyze_trading_hub_setup


def _base_candles(count=80, close=100.0):
    return [
        Candle(
            timestamp=i,
            open=close,
            high=close + 1,
            low=close - 1,
            close=close,
            volume=1000,
        )
        for i in range(count)
    ]


def test_okey_and_bybite_aliases_are_supported():
    assert normalize_exchange("okey") == ExchangeName.OKX
    assert normalize_exchange("bybite") == ExchangeName.BYBIT


def test_sell_side_sweep_with_ltf_choch_creates_buy_signal():
    candles = _base_candles()
    candles[-2] = Candle(timestamp=78, open=100, high=101, low=99, close=100, volume=1000)
    candles[-1] = Candle(timestamp=79, open=99, high=103, low=94, close=102, volume=1800)

    analysis = analyze_trading_hub_setup(candles)

    assert analysis.action == SignalAction.BUY
    assert analysis.setup == "sell_side_sweep_ltf_choch"
    assert "Sell-side liquidity was swept" in analysis.reasons[0]


def test_buy_side_sweep_with_ltf_choch_creates_sell_signal():
    candles = _base_candles()
    candles[-2] = Candle(timestamp=78, open=100, high=101, low=99, close=100, volume=1000)
    candles[-1] = Candle(timestamp=79, open=101, high=106, low=97, close=98, volume=1800)

    analysis = analyze_trading_hub_setup(candles)

    assert analysis.action == SignalAction.SELL
    assert analysis.setup == "buy_side_sweep_ltf_choch"
    assert "Buy-side liquidity was swept" in analysis.reasons[0]


def test_no_sweep_stays_flat():
    analysis = analyze_trading_hub_setup(_base_candles())

    assert analysis.action == SignalAction.HOLD
    assert analysis.setup == "no_liquidity_sweep"


def test_order_ledger_records_leverage_and_calculates_performance(tmp_path, monkeypatch):
    records = [
        OrderRecord(
            id="paper-test",
            exchange=ExchangeName.BINANCE,
            symbol="BTC/USDT",
            side=OrderSide.BUY,
            type="market",
            amount=0.1,
            entry_price=100.0,
            leverage=10,
            paper=True,
            status="paper_accepted",
            created_at="2026-05-28T00:00:00+00:00",
            raw_order={},
        )
    ]
    summary = performance_summary(records, {"binance:BTC/USDT": 110.0})

    assert records[0].leverage == 10
    assert summary.unrealized_pnl == 1.0
    assert summary.margin_used == 1.0
    assert summary.unrealized_roe == 1.0


def test_paper_order_converts_usdt_amount_to_contract_quantity(monkeypatch):
    client = PaperExchangeClient(ExchangeName.BINANCE)
    monkeypatch.setattr(client, "fetch_ticker", lambda _symbol: {"last": 100.0})

    order = client.create_order(
        OrderRequest(
            exchange=ExchangeName.BINANCE,
            symbol="BTC/USDT",
            side=OrderSide.BUY,
            amount=50.0,
            leverage=10,
        )
    )

    assert order["amount"] == 5.0
    assert order["info"]["amount_unit"] == "USDT"
    assert order["info"]["margin_usdt"] == 50.0
    assert order["info"]["notional_usdt"] == 500.0


def test_candle_cache_loads_initial_batch_then_only_latest(monkeypatch):
    class MemoryCandleStore:
        def __init__(self):
            self.rows = {}
            self.reset_calls = []

        def reset(self, exchange, symbol, timeframe):
            key = (exchange.value, symbol, timeframe)
            self.reset_calls.append(key)
            self.rows.pop(key, None)

        def count(self, exchange, symbol, timeframe):
            return len(self.rows.get((exchange.value, symbol, timeframe), []))

        def save_many(self, exchange, symbol, timeframe, candles):
            key = (exchange.value, symbol, timeframe)
            rows = self.rows.setdefault(key, [])
            existing = {candle.timestamp for candle in rows}
            new_rows = [candle for candle in candles if candle.timestamp not in existing]
            rows.extend(new_rows)
            rows.sort(key=lambda candle: candle.timestamp)
            return len(new_rows)

        def list_recent(self, exchange, symbol, timeframe, limit):
            return self.rows.get((exchange.value, symbol, timeframe), [])[-limit:]

    class FakeExchangeClient:
        exchange = ExchangeName.BINANCE

        def __init__(self):
            self.calls = []

        def fetch_ohlcv(self, symbol, timeframe="15m", limit=300):
            self.calls.append((symbol, timeframe, limit))
            if limit == 60:
                return [
                    Candle(timestamp=i, open=i, high=i + 1, low=i - 0.5, close=i + 0.5, volume=10 + i)
                    for i in range(1, 61)
                ]
            return [Candle(timestamp=61, open=61, high=62, low=60.5, close=61.5, volume=71)]

    client = FakeExchangeClient()
    monkeypatch.setattr("tradingagents.crypto.services.candle_cache.get_exchange_client", lambda _exchange: client)
    store = MemoryCandleStore()
    cache = CandleCache(store=store)
    request = AnalyzeRequest(exchange=ExchangeName.BINANCE, symbol="BTC/USDT", timeframe="15m", limit=60)

    first = cache.get_snapshot(request)
    second = cache.get_snapshot(request)

    assert client.calls == [("BTC/USDT", "15m", 60), ("BTC/USDT", "15m", 1), ("BTC/USDT", "15m", 1)]
    assert store.reset_calls == [("binance", "BTC/USDT", "15m")]
    assert [candle.timestamp for candle in first.candles][-3:] == [59, 60, 61]
    assert [candle.timestamp for candle in second.candles][-3:] == [59, 60, 61]


def test_task_record_serializes_for_task_center():
    task = TaskRecord(
        id="task-1",
        status=TaskStatus.PENDING.value,
        exchange=ExchangeName.BINANCE,
        symbol="BTC/USDT",
        timeframe="15m",
        execute=False,
        use_llm_decision=False,
        amount=0,
        leverage=1,
        created_at="2026-05-28T00:00:00+00:00",
        updated_at="2026-05-28T00:00:00+00:00",
    )

    payload = task.model_dump(mode="json")

    assert payload["status"] == "PENDING"
    assert payload["exchange"] == "binance"
    assert payload["symbol"] == "BTC/USDT"
    assert payload["use_llm_decision"] is False


def test_task_record_maps_llm_switch_to_database_row():
    task = TaskRecord(
        id="task-llm-off",
        status=TaskStatus.PENDING.value,
        exchange=ExchangeName.BINANCE,
        symbol="ETH/USDT",
        timeframe="1h",
        execute=True,
        use_llm_decision=False,
        amount=1,
        leverage=3,
        created_at="2026-05-28T00:00:00+00:00",
        updated_at="2026-05-28T00:00:00+00:00",
    )

    row = _task_to_row(task)
    restored = _row_to_task(row)

    assert row.use_llm_decision is False
    assert restored.use_llm_decision is False


def test_exchange_balance_serializes_for_account_panel():
    balance = ExchangeBalance(
        exchange=ExchangeName.BINANCE,
        paper=True,
        validated=False,
        assets=[BalanceAsset(asset="USDT", free=1000, used=25, total=1025)],
        message="Paper balance returned.",
    )

    payload = balance.model_dump(mode="json")

    assert payload["exchange"] == "binance"
    assert payload["paper"] is True
    assert payload["assets"][0]["asset"] == "USDT"
    assert payload["assets"][0]["free"] == 1000


def test_task_log_record_serializes_for_task_center():
    log = TaskLogRecord(
        id=1,
        task_id="task-1",
        level="info",
        message="Task started.",
        created_at="2026-05-28T00:00:00+00:00",
        context={"symbol": "BTC/USDT"},
    )

    payload = log.model_dump(mode="json")

    assert payload["task_id"] == "task-1"
    assert payload["level"] == "info"
    assert payload["context"]["symbol"] == "BTC/USDT"


def test_llm_reviewer_eligibility_uses_rule_action():
    payload = {
        "snapshot": {"exchange": "binance", "symbol": "BTC/USDT", "timeframe": "15m", "candles": []},
        "analysis": TradingHubAnalysis(
            action=SignalAction.BUY,
            confidence=0.7,
            bias="bullish",
            setup="sell_side_sweep_ltf_choch",
        ).model_dump(mode="json"),
    }

    assert is_llm_decision_eligible(payload) is True

    rule_payload = create_rule_payload(payload)
    assert rule_payload["analysis"]["action"] == "buy"
    assert rule_payload["symbol"] == "BTC/USDT"


class _FakeWebSocket:
    def __init__(self):
        self.accepted = False
        self.messages = []

    async def accept(self):
        self.accepted = True

    async def send_json(self, message):
        self.messages.append(message)


def test_connection_manager_tracks_personal_messages():
    import asyncio

    manager = ConnectionManager()
    websocket = _FakeWebSocket()

    async def run():
        await manager.connect("task-1", websocket)
        await manager.send_personal_message("task-1", {"status": "completed"})

    asyncio.run(run())
    manager.disconnect("task-1")

    assert websocket.accepted is True
    assert websocket.messages == [{"status": "completed"}]
    assert manager.active_connections == {}
    assert task_status_channel("task-1") == "task_status_task-1"


def test_task_state_machine_allows_only_valid_lifecycle():
    assert state_machine.can_transition(TaskStatus.PENDING, TaskStatus.ORDERED)
    assert state_machine.can_transition(TaskStatus.ORDERED, TaskStatus.COMPLETED)
    assert not state_machine.can_transition(TaskStatus.PENDING, TaskStatus.COMPLETED)

    try:
        state_machine.assert_transition(TaskStatus.COMPLETED, TaskStatus.ORDERED)
    except InvalidTaskTransition:
        pass
    else:
        raise AssertionError("COMPLETED must be terminal")


def test_task_factory_creates_crypto_handler():
    handler = TaskFactory.create("crypto_bot_run")

    assert handler.task_type == "crypto_bot_run"


def test_crypto_task_keeps_pending_until_order_or_cancel(monkeypatch):
    handler = CryptoBotRunHandler()
    statuses = []
    publishes = []
    logs = []
    attempts = {"count": 0}

    def fake_analyze_market(_request):
        attempts["count"] += 1
        return {
            "analysis": {
                "action": "hold",
                "confidence": 0.2,
                "reasons": ["not ready"],
            }
        }

    monkeypatch.setattr("tradingagents.crypto.tasks.task_handlers.analyze_market", fake_analyze_market)
    monkeypatch.setattr("tradingagents.crypto.tasks.task_handlers.time.sleep", lambda _seconds: None)
    monkeypatch.setattr(handler, "log", lambda _task_id, _level, message, context=None: logs.append((message, context or {})))
    monkeypatch.setattr(handler, "set_status", lambda _task_id, status, **_kwargs: statuses.append(status.value))
    monkeypatch.setattr(handler, "publish", lambda _task_id, status, **_kwargs: publishes.append(status.value))
    monkeypatch.setattr(handler, "is_canceled", lambda _task_id: attempts["count"] >= 3)

    result = handler.handle("task-pending", {
        "exchange": "binance",
        "symbol": "BTC/USDT",
        "timeframe": "15m",
        "limit": 100,
        "transaction": False,
        "execute": False,
        "use_llm_decision": False,
        "amount": 0,
        "leverage": 1,
    })

    assert result["analysis"]["action"] == "hold"
    assert attempts["count"] == 3
    assert statuses == [TaskStatus.PENDING.value, TaskStatus.PENDING.value]
    assert publishes == [TaskStatus.PENDING.value, TaskStatus.PENDING.value]
    assert TaskStatus.COMPLETED.value not in statuses
    assert any("Strategy not satisfied" in message for message, _context in logs)
    assert any(context.get("reason") for _message, context in logs)


def test_crypto_task_does_not_reopen_canceled_task_after_analysis(monkeypatch):
    handler = CryptoBotRunHandler()
    statuses = []
    attempts = {"count": 0}

    def fake_analyze_market(_request):
        attempts["count"] += 1
        return {
            "analysis": {
                "action": "hold",
                "confidence": 0.2,
                "reasons": ["not ready"],
            }
        }

    monkeypatch.setattr("tradingagents.crypto.tasks.task_handlers.analyze_market", fake_analyze_market)
    monkeypatch.setattr(handler, "log", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(handler, "set_status", lambda _task_id, status, **_kwargs: statuses.append(status.value))
    monkeypatch.setattr(handler, "publish", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(handler, "is_canceled", lambda _task_id: attempts["count"] >= 1)

    result = handler.handle("task-canceled-race", {
        "exchange": "binance",
        "symbol": "BTC/USDT",
        "timeframe": "15m",
        "limit": 100,
        "transaction": True,
        "execute": True,
        "use_llm_decision": False,
        "amount": 10,
        "leverage": 1,
    })

    assert result["analysis"]["action"] == "hold"
    assert statuses == []


def test_task_routes_expose_websocket_and_delete():
    from tradingagents.crypto.api import app

    routes = {(route.path, ",".join(sorted(getattr(route, "methods", []) or []))) for route in app.routes}

    assert any(path == "/ws/{task_id}" for path, _ in routes)
    assert ("/api/tasks/{task_id}", "DELETE") in routes
    assert ("/tasks/{task_id}", "DELETE") in routes
