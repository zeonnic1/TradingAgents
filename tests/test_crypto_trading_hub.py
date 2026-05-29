from tradingagents.crypto.utils.exchanges import PaperExchangeClient, normalize_exchange
from tradingagents.crypto.managers.connect_manager import ConnectionManager, task_status_channel
from tradingagents.crypto.llm_reviewer import create_rule_payload, is_llm_decision_eligible
from tradingagents.crypto.utils.ledger import TaskLogRow, TaskRow, _row_to_task, _task_to_row, performance_summary
from tradingagents.crypto.tasks.task_handlers import CryptoBotRunHandler, _build_strategy_runtime_log
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
    candles[48] = Candle(timestamp=48, open=103, high=104, low=101, close=103, volume=1000)
    candles[49] = Candle(timestamp=49, open=104, high=106, low=102, close=105, volume=1000)
    candles[50] = Candle(timestamp=50, open=106, high=110, low=104, close=108, volume=1200)
    candles[51] = Candle(timestamp=51, open=107, high=108, low=103, close=104, volume=1000)
    candles[52] = Candle(timestamp=52, open=104, high=105, low=101, close=102, volume=1000)
    candles[53] = Candle(timestamp=53, open=102, high=103, low=100.5, close=101, volume=1000)
    candles[54] = Candle(timestamp=54, open=101, high=102, low=100, close=101, volume=1000)
    candles[55] = Candle(timestamp=55, open=102, high=104, low=101, close=103, volume=1000)
    candles[56] = Candle(timestamp=56, open=104, high=106, low=103, close=105, volume=1000)
    candles[57] = Candle(timestamp=57, open=106, high=109, low=105, close=108, volume=1000)
    candles[58] = Candle(timestamp=58, open=109, high=112, low=108, close=111, volume=1500)
    for index in range(59, 79):
        candles[index] = Candle(timestamp=index, open=106, high=107, low=106, close=106.5, volume=1000)
    candles[70] = Candle(timestamp=70, open=107, high=108, low=105, close=106, volume=1000)
    candles[78] = Candle(timestamp=78, open=106, high=109, low=99, close=108, volume=1800)
    candles[79] = Candle(timestamp=79, open=108, high=109.5, low=107.5, close=108.5, volume=900)

    analysis = analyze_trading_hub_setup(candles)

    assert analysis.action == SignalAction.BUY
    assert analysis.setup == "sell_side_sweep_ltf_choch"
    assert "Sell-side liquidity from the latest bullish BOS structure was swept" in analysis.reasons[0]
    assert analysis.metadata["bos_direction"] == "bullish"
    assert analysis.metadata["dynamic_structure_low"] == 100


def test_buy_side_sweep_with_ltf_choch_creates_sell_signal():
    candles = _base_candles()
    candles[48] = Candle(timestamp=48, open=97, high=99, low=94, close=96, volume=1000)
    candles[49] = Candle(timestamp=49, open=96, high=98, low=92, close=94, volume=1000)
    candles[50] = Candle(timestamp=50, open=94, high=96, low=90, close=92, volume=1200)
    candles[51] = Candle(timestamp=51, open=93, high=97, low=92, close=95, volume=1000)
    candles[52] = Candle(timestamp=52, open=95, high=98, low=94, close=97, volume=1000)
    candles[53] = Candle(timestamp=53, open=97, high=99, low=96, close=98, volume=1000)
    candles[54] = Candle(timestamp=54, open=98, high=100, low=97, close=99, volume=1000)
    candles[55] = Candle(timestamp=55, open=98, high=99, low=95, close=96, volume=1000)
    candles[56] = Candle(timestamp=56, open=96, high=97, low=92, close=93, volume=1000)
    candles[57] = Candle(timestamp=57, open=93, high=94, low=90, close=91, volume=1000)
    candles[58] = Candle(timestamp=58, open=91, high=92, low=88, close=89, volume=1500)
    for index in range(59, 79):
        candles[index] = Candle(timestamp=index, open=93, high=94, low=92, close=93, volume=1000)
    candles[70] = Candle(timestamp=70, open=92, high=95, low=91, close=94, volume=1000)
    candles[78] = Candle(timestamp=78, open=95, high=101, low=91, close=91.5, volume=1800)
    candles[79] = Candle(timestamp=79, open=91.5, high=92.5, low=90.5, close=91.8, volume=900)

    analysis = analyze_trading_hub_setup(candles)

    assert analysis.action == SignalAction.SELL
    assert analysis.setup == "buy_side_sweep_ltf_choch"
    assert "Buy-side liquidity from the latest bearish BOS structure was swept" in analysis.reasons[0]
    assert analysis.metadata["bos_direction"] == "bearish"
    assert analysis.metadata["dynamic_structure_high"] == 100


def test_no_sweep_stays_flat():
    analysis = analyze_trading_hub_setup(_base_candles())

    assert analysis.action == SignalAction.HOLD
    assert analysis.setup == "no_valid_bos"


def test_bos_requires_effective_pullback_before_break():
    candles = _base_candles()
    candles[48] = Candle(timestamp=48, open=100, high=100.2, low=99.5, close=99.8, volume=1000)
    candles[49] = Candle(timestamp=49, open=99.8, high=100.0, low=94.0, close=95.0, volume=1000)
    candles[50] = Candle(timestamp=50, open=95.0, high=95.1, low=90.0, close=91.0, volume=1000)
    candles[51] = Candle(timestamp=51, open=91.0, high=90.01, low=90.0, close=90.01, volume=1000)
    candles[52] = Candle(timestamp=52, open=90.01, high=90.02, low=90.0, close=90.02, volume=1000)
    candles[53] = Candle(timestamp=53, open=90.02, high=90.03, low=90.0, close=90.03, volume=1000)
    candles[54] = Candle(timestamp=54, open=90.03, high=90.04, low=90.0, close=90.03, volume=1000)
    candles[55] = Candle(timestamp=55, open=90.03, high=90.02, low=89.5, close=89.8, volume=1000)
    candles[56] = Candle(timestamp=56, open=89.8, high=89.9, low=88.5, close=89.0, volume=1000)
    candles[57] = Candle(timestamp=57, open=89.0, high=89.2, low=87.5, close=88.0, volume=1000)
    for index in range(58, 80):
        candles[index] = Candle(timestamp=index, open=88.0, high=89.0, low=87.8, close=88.4, volume=1000)

    analysis = analyze_trading_hub_setup(candles)

    assert analysis.setup == "no_valid_bos"
    assert analysis.metadata["bos_direction"] is None


def test_bearish_bos_uses_equal_low_structure_before_lower_low():
    candles = _base_candles(close=95)
    candles[20] = Candle(timestamp=20, open=95, high=96, low=90, close=92, volume=1000)
    candles[21] = Candle(timestamp=21, open=92, high=94, low=91, close=93, volume=1000)
    candles[44] = Candle(timestamp=44, open=94, high=95, low=90, close=92, volume=1000)
    candles[45] = Candle(timestamp=45, open=92, high=94, low=91, close=93, volume=1000)
    candles[48] = Candle(timestamp=48, open=93, high=97, low=92, close=96, volume=1000)
    candles[49] = Candle(timestamp=49, open=96, high=99, low=95, close=98, volume=1000)
    candles[50] = Candle(timestamp=50, open=98, high=100, low=97, close=99, volume=1000)
    candles[51] = Candle(timestamp=51, open=99, high=99, low=96, close=97, volume=1000)
    candles[56] = Candle(timestamp=56, open=94, high=95, low=91, close=92, volume=1000)
    candles[57] = Candle(timestamp=57, open=92, high=93, low=90.5, close=90.5, volume=1000)
    candles[58] = Candle(timestamp=58, open=90.5, high=92, low=88, close=89, volume=1000)

    analysis = analyze_trading_hub_setup(candles)

    assert analysis.metadata["bos_direction"] == "bearish"
    assert analysis.metadata["bos_broken_swing_index"] == 44
    assert analysis.metadata["bos_break_index"] == 58
    assert analysis.metadata["dynamic_structure_high"] == 100
    assert analysis.metadata["dynamic_structure_low"] == 88


def test_bearish_poi_ob_scans_before_broken_low_for_close_above_dynamic_high_with_fvg():
    candles = _base_candles(close=73000)
    candles[8] = Candle(timestamp=8, open=74287.43, high=74380.39, low=74140.47, close=74212.0, volume=1000)
    candles[9] = Candle(timestamp=9, open=74212.01, high=74242.34, low=73500.0, close=73746.57, volume=1000)
    candles[10] = Candle(timestamp=10, open=73746.58, high=73786.37, low=73360.36, close=73430.01, volume=1000)
    candles[11] = Candle(timestamp=11, open=73430.0, high=73560.0, low=73060.59, close=73258.01, volume=1000)
    candles[12] = Candle(timestamp=12, open=73258.0, high=73260.01, low=72728.85, close=73082.08, volume=1000)
    candles[13] = Candle(timestamp=13, open=73082.07, high=73126.59, low=72813.67, close=73004.4, volume=1000)
    candles[26] = Candle(timestamp=26, open=73259.03, high=73512.14, low=73259.03, close=73502.0, volume=1000)
    candles[36] = Candle(timestamp=36, open=73541.25, high=73616.0, low=73500.0, close=73579.66, volume=1000)
    candles[49] = Candle(timestamp=49, open=73338.35, high=73338.35, low=73000.0, close=73160.52, volume=1000)
    candles[52] = Candle(timestamp=52, open=73022.01, high=73329.35, low=72582.82, close=73314.62, volume=1000)

    analysis = analyze_trading_hub_setup(candles)

    assert analysis.metadata["bos_direction"] == "bearish"
    assert analysis.metadata["bos_broken_swing_index"] == 12
    assert analysis.metadata["dynamic_structure_high"] == 73616.0
    assert analysis.metadata["poi_ob_index"] == 8
    assert analysis.metadata["poi_ob_low"] == 74140.47
    assert analysis.metadata["poi_ob_high"] == 74380.39
    assert analysis.metadata["poi_ob_fvg_low"] == 73786.37
    assert analysis.metadata["poi_ob_fvg_high"] == 74140.47
    assert analysis.metadata["dynamic_structure_high"] <= analysis.metadata["poi_ob_low"]


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

        def save_many(self, exchange, symbol, timeframe, candles, update_existing=False):
            key = (exchange.value, symbol, timeframe)
            rows = self.rows.setdefault(key, [])
            if update_existing:
                incoming = {candle.timestamp for candle in candles}
                rows[:] = [candle for candle in rows if candle.timestamp not in incoming]
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
            self.latest_close = 61.5

        def fetch_ohlcv(self, symbol, timeframe="15m", limit=300):
            self.calls.append((symbol, timeframe, limit))
            if limit == 60:
                return [
                    Candle(timestamp=i, open=i, high=i + 1, low=i - 0.5, close=i + 0.5, volume=10 + i)
                    for i in range(1, 61)
                ]
            self.latest_close += 1
            return [Candle(timestamp=61, open=61, high=62, low=60.5, close=self.latest_close, volume=71)]

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
    assert first.candles[-1].close == 62.5
    assert second.candles[-1].close == 63.5


def test_strategy_runtime_log_includes_bias_price_timeframes_and_poi():
    request = AnalyzeRequest(exchange=ExchangeName.BINANCE, symbol="BTC/USDT", timeframe="15m", limit=60)
    message = _build_strategy_runtime_log(request, {
        "snapshot": {
            "candles": [
                {"timestamp": 1779730200000, "open": 99, "high": 102, "low": 98, "close": 101, "volume": 10},
                {"timestamp": 1779731100000, "open": 100, "high": 110, "low": 90, "close": 101, "volume": 10},
                {"timestamp": 1779732000000, "open": 99, "high": 102, "low": 98, "close": 101, "volume": 10},
            ]
        },
        "analysis": {
            "bias": "bullish",
            "entry": 100,
            "stop_loss": 95,
            "metadata": {
                "bos_direction": "bearish",
                "bos_break_price": 100,
                "bos_broken_swing_index": 0,
                "bos_break_index": 1,
                "dynamic_structure_high": 110,
                "dynamic_structure_high_index": 1,
                "dynamic_structure_low": 90,
                "dynamic_structure_low_index": 1,
            },
        },
    })

    assert "日线偏见:bullish" in message
    assert "当前价格:101" in message
    assert "当前LTF:15m" in message
    assert "HTF:4h" in message
    assert "被跌破低点为100 时间:2026-05-25 17:30:00 UTC,BOS发生时间:2026-05-25 17:45:00 UTC,更低的低点为90 时间:2026-05-25 17:45:00 UTC" in message
    assert "等待清扫:dynamic_structure_high:110 时间:2026-05-25 17:45:00 UTC" in message
    assert "高POI/OB区间:95.0-100.0" in message


def test_task_logs_are_one_to_many_cascade_children():
    assert TaskRow.logs.property.mapper.class_ is TaskLogRow
    assert TaskRow.logs.property.cascade.delete_orphan
    task_id_column = TaskLogRow.__table__.c.task_id
    assert any(fk.column.table.name == "crypto_tasks" for fk in task_id_column.foreign_keys)


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
