from tradingagents.crypto.utils.exchanges import PaperExchangeClient, normalize_exchange
from tradingagents.crypto.managers.connect_manager import ConnectionManager, task_status_channel
from tradingagents.crypto.llm_reviewer import create_rule_payload, is_llm_decision_eligible
from tradingagents.crypto.utils.ledger import TaskLogRow, TaskRow, _row_to_task, _task_to_row, performance_summary
from tradingagents.crypto.tasks.task_handlers import (
    CryptoBotRunHandler,
    _build_strategy_runtime_log,
    _candidate_trade_from_poi,
    _poi_consumed_after_bos,
    _resolve_poi_ob_range,
)
from tradingagents.crypto.tasks.task_factory import TaskFactory
from tradingagents.crypto.tasks.task_fsm import InvalidTaskTransition, TaskStatus, state_machine
from tradingagents.crypto.models import (
    BalanceAsset,
    BotRunRequest,
    Candle,
    ExchangeBalance,
    ExchangeName,
    AnalyzeRequest,
    OrderRecord,
    OrderRequest,
    OrderSide,
    OrderType,
    SignalAction,
    TaskLogRecord,
    TaskRecord,
    TradingHubAnalysis,
)
from tradingagents.crypto.services.candle_cache import CandleCache
from tradingagents.crypto.services.service import analyze_market
from tradingagents.crypto.strategys.trading_hub import _latest_bos_structure, _ob_contained_by_previous_candles
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


def test_daily_bearish_bias_filters_out_bullish_ltf_bos():
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
    daily_candles = [
        Candle(timestamp=1, open=100, high=110, low=90, close=105, volume=1000),
        Candle(timestamp=2, open=105, high=106, low=91, close=104, volume=1000),
        Candle(timestamp=3, open=104, high=105, low=95, close=99, volume=1000),
        Candle(timestamp=4, open=99, high=101, low=96, close=100, volume=1000),
    ]

    analysis = analyze_trading_hub_setup(candles, daily_candles=daily_candles)

    assert analysis.bias == "bearish"
    assert analysis.metadata["daily_bias"] == "bearish"
    assert analysis.metadata["bos_direction"] is None


def test_daily_bias_ignores_unclosed_current_daily_candle():
    candles = _base_candles()
    daily_candles = [
        Candle(timestamp=1, open=100, high=110, low=90, close=104, volume=1000),
        Candle(timestamp=2, open=104, high=106, low=91, close=103, volume=1000),
        Candle(timestamp=3, open=103, high=104, low=92, close=96, volume=1000),
        Candle(timestamp=4, open=96, high=100, low=89, close=95, volume=1000),
        Candle(timestamp=5, open=95, high=98, low=88, close=92, volume=1000),
        Candle(timestamp=6, open=92, high=97, low=87, close=95, volume=1000),
    ]

    analysis = analyze_trading_hub_setup(candles, daily_candles=daily_candles)

    assert analysis.metadata["daily_bias"] == "bearish"


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


def test_bearish_bos_requires_confirmed_lower_swing_low_after_break():
    candles = _base_candles(close=96)
    candles[20] = Candle(timestamp=20, open=96, high=97, low=90, close=92, volume=1000)
    candles[21] = Candle(timestamp=21, open=92, high=94, low=91, close=93, volume=1000)
    candles[24] = Candle(timestamp=24, open=93, high=98, low=92, close=97, volume=1000)
    candles[25] = Candle(timestamp=25, open=97, high=101, low=96, close=100, volume=1000)
    candles[26] = Candle(timestamp=26, open=100, high=100, low=96, close=97, volume=1000)
    for index in range(30, 79):
        low = 91 - ((index - 30) * 0.5)
        candles[index] = Candle(
            timestamp=index,
            open=low + 1.0,
            high=low + 2.0,
            low=low,
            close=low + 0.5,
            volume=1000,
        )

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


def test_bearish_bos_uses_current_lowest_lower_low_after_break():
    candles = _base_candles(count=100, close=95)
    candles[20] = Candle(timestamp=20, open=95, high=96, low=90, close=92, volume=1000)
    candles[21] = Candle(timestamp=21, open=92, high=94, low=91, close=93, volume=1000)
    candles[30] = Candle(timestamp=30, open=94, high=99, low=93, close=98, volume=1000)
    candles[31] = Candle(timestamp=31, open=98, high=100, low=97, close=99, volume=1000)
    candles[32] = Candle(timestamp=32, open=99, high=99, low=96, close=97, volume=1000)
    candles[50] = Candle(timestamp=50, open=91, high=92, low=88, close=89, volume=1000)
    candles[51] = Candle(timestamp=51, open=89, high=91, low=89, close=90, volume=1000)
    candles[60] = Candle(timestamp=60, open=90, high=102, low=89, close=101, volume=1000)
    candles[61] = Candle(timestamp=61, open=101, high=101, low=91, close=92, volume=1000)
    candles[70] = Candle(timestamp=70, open=89, high=90, low=84, close=85, volume=1000)
    candles[71] = Candle(timestamp=71, open=85, high=87, low=85, close=86, volume=1000)

    structure = _latest_bos_structure(candles, preferred_direction="bearish")

    assert structure is not None
    assert structure.broken_swing_index == 50
    assert structure.break_price == 88
    assert structure.break_index == 70
    assert structure.dynamic_structure_low == 84
    assert structure.dynamic_structure_high == 102


def test_bearish_bos_scans_past_immediate_ineffective_pullback():
    candles = _base_candles(count=90, close=73000)
    candles[20] = Candle(timestamp=20, open=73022.01, high=73329.35, low=72582.82, close=73314.62, volume=1000)
    candles[21] = Candle(timestamp=21, open=73314.35, high=73573.21, low=72885.6, close=72965.07, volume=1000)
    candles[22] = Candle(timestamp=22, open=72965.08, high=73061.71, low=72794.15, close=72855.8, volume=1000)
    candles[23] = Candle(timestamp=23, open=72855.79, high=73047.63, low=72699.22, close=73033.06, volume=1000)
    candles[24] = Candle(timestamp=24, open=73033.06, high=73180.01, low=72861.92, close=72861.93, volume=1000)
    candles[28] = Candle(timestamp=28, open=72928.73, high=73160.55, low=72741.4, close=73109.55, volume=1000)
    candles[35] = Candle(timestamp=35, open=73600.0, high=73749.69, low=73500.0, close=73650.0, volume=1000)
    candles[49] = Candle(timestamp=49, open=73800.0, high=73949.22, low=73700.0, close=73820.0, volume=1000)
    candles[70] = Candle(timestamp=70, open=73000.0, high=73100.0, low=72512.49, close=72600.0, volume=1000)
    candles[71] = Candle(timestamp=71, open=72600.0, high=72800.0, low=72600.0, close=72700.0, volume=1000)
    candles[72] = Candle(timestamp=72, open=72700.0, high=72900.0, low=72700.0, close=72800.0, volume=1000)

    analysis = analyze_trading_hub_setup(candles)

    assert analysis.metadata["bos_direction"] == "bearish"
    assert analysis.metadata["bos_broken_swing_index"] == 20
    assert analysis.metadata["bos_break_index"] == 70
    assert analysis.metadata["dynamic_structure_high"] == 73949.22
    assert analysis.metadata["dynamic_structure_low"] == 72512.49


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


def test_ob_is_invalid_when_previous_candle_contains_its_range():
    candles = _base_candles(count=8, close=100)
    candles[3] = Candle(timestamp=3, open=100, high=110, low=90, close=101, volume=1000)
    candles[4] = Candle(timestamp=4, open=101, high=108, low=92, close=102, volume=1000)
    candles[5] = Candle(timestamp=5, open=102, high=112, low=92, close=111, volume=1000)

    assert _ob_contained_by_previous_candles(candles, 4) is True
    assert _ob_contained_by_previous_candles(candles, 5) is False


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


def test_analyze_market_expands_candle_history_when_bos_is_missing(monkeypatch):
    class MemoryCandleStore:
        def __init__(self):
            self.rows = {}

        def reset(self, exchange, symbol, timeframe):
            self.rows.pop((exchange.value, symbol, timeframe), None)

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

    def expanded_bos_candles():
        candles = _base_candles(count=120, close=95)
        candles[84] = Candle(timestamp=84, open=94, high=95, low=90, close=92, volume=1000)
        candles[85] = Candle(timestamp=85, open=92, high=94, low=91, close=93, volume=1000)
        candles[88] = Candle(timestamp=88, open=93, high=97, low=92, close=96, volume=1000)
        candles[89] = Candle(timestamp=89, open=96, high=99, low=95, close=98, volume=1000)
        candles[90] = Candle(timestamp=90, open=98, high=100, low=97, close=99, volume=1000)
        candles[91] = Candle(timestamp=91, open=99, high=99, low=96, close=97, volume=1000)
        candles[96] = Candle(timestamp=96, open=94, high=95, low=91, close=92, volume=1000)
        candles[97] = Candle(timestamp=97, open=92, high=93, low=90.5, close=90.5, volume=1000)
        candles[98] = Candle(timestamp=98, open=90.5, high=92, low=88, close=89, volume=1000)
        for index in range(99, 120):
            candles[index] = Candle(timestamp=index, open=91, high=92, low=90, close=91, volume=1000)
        return candles

    class FakeExchangeClient:
        exchange = ExchangeName.BINANCE

        def __init__(self):
            self.calls = []

        def fetch_ohlcv(self, symbol, timeframe="15m", limit=300):
            self.calls.append((symbol, timeframe, limit))
            if timeframe == "1d":
                return [
                    Candle(timestamp=day, open=100, high=102, low=98, close=101, volume=1000)
                    for day in range(58)
                ] + [
                    Candle(timestamp=58, open=100, high=101, low=90, close=95, volume=1000),
                    Candle(timestamp=59, open=95, high=98, low=88, close=96, volume=1000),
                ]
            if limit == 60:
                return _base_candles(count=60, close=100)
            if limit == 120:
                return expanded_bos_candles()
            return [expanded_bos_candles()[-1]]

    client = FakeExchangeClient()
    monkeypatch.setattr("tradingagents.crypto.services.candle_cache.get_exchange_client", lambda _exchange: client)
    monkeypatch.setattr(
        "tradingagents.crypto.services.service._candle_cache",
        CandleCache(store=MemoryCandleStore()),
    )

    payload = analyze_market(AnalyzeRequest(exchange=ExchangeName.BINANCE, symbol="BTC/USDT", timeframe="15m", limit=60))

    assert client.calls == [
        ("BTC/USDT", "15m", 60),
        ("BTC/USDT", "15m", 1),
        ("BTC/USDT", "1d", 60),
        ("BTC/USDT", "1d", 1),
        ("BTC/USDT", "15m", 120),
        ("BTC/USDT", "15m", 1),
    ]
    assert payload["candle_sync"]["expanded"] is True
    assert payload["candle_sync"]["attempts"][-1]["limit"] == 120
    assert payload["analysis"]["metadata"]["bos_history_expanded"] is True
    assert payload["analysis"]["metadata"]["bos_direction"] == "bearish"


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

    assert "Daily bias:bullish" in message
    assert "current price:101" in message
    assert "LTF:15m" in message
    assert "HTF:4h" in message
    assert "broken low:100 time:2026-05-25 17:30:00 UTC,BOS time:2026-05-25 17:45:00 UTC,lower low:90 time:2026-05-25 17:45:00 UTC" in message
    assert "waiting sweep:dynamic_structure_high:110 time:2026-05-25 17:45:00 UTC" in message
    assert "POI/OB range:no OB+FVG" in message
    assert _resolve_poi_ob_range({}, {"range_low": 90, "range_high": 110}) == "no OB+FVG"
    assert _resolve_poi_ob_range({}, {"poi_ob_low": 95, "poi_ob_high": 100}) == "95-100"


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


def test_task_handler_creates_limit_order_from_bos_poi_without_llm():
    handler = CryptoBotRunHandler()
    request = BotRunRequest(
        exchange=ExchangeName.BINANCE,
        symbol="BTC/USDT",
        timeframe="15m",
        transaction=True,
        use_llm_decision=False,
        amount=50,
        leverage=5,
    )
    payload = {
        "analysis": {
            "action": "hold",
            "metadata": {
                "bos_direction": "bearish",
                "bos_break_price": 72582.82,
                "dynamic_structure_high": 73949.22,
                "dynamic_structure_low": 72512.49,
                "poi_ob_low": 74140.47,
                "poi_ob_high": 74380.39,
            },
        }
    }

    order_request, reason = handler._order_request_from_decision(request, payload, None)

    assert reason is None
    assert order_request is not None
    assert order_request.side == OrderSide.SELL
    assert order_request.type == OrderType.LIMIT
    assert order_request.price == 74140.47
    assert order_request.amount == 50
    assert order_request.leverage == 5
    assert order_request.params["stop_loss"] == 74380.39
    assert order_request.params["take_profit"] == 72512.49


def test_task_handler_uses_llm_levels_for_bos_poi_risk_control():
    handler = CryptoBotRunHandler()
    request = BotRunRequest(
        exchange=ExchangeName.BINANCE,
        symbol="BTC/USDT",
        timeframe="15m",
        transaction=True,
        use_llm_decision=True,
        amount=50,
        leverage=5,
    )
    payload = {
        "analysis": {
            "action": "hold",
            "metadata": {
                "bos_direction": "bearish",
                "dynamic_structure_low": 72512.49,
                "poi_ob_low": 74140.47,
                "poi_ob_high": 74380.39,
            },
        }
    }
    decision = {
        "decision": {
            "approved": True,
            "action": "sell",
            "recommended_leverage": 2,
            "entry": 74100,
            "stop_loss": 74400,
            "take_profit_1": 73000,
        }
    }

    order_request, reason = handler._order_request_from_decision(request, payload, decision)

    assert reason is None
    assert order_request is not None
    assert order_request.side == OrderSide.SELL
    assert order_request.type == OrderType.LIMIT
    assert order_request.price == 74100
    assert order_request.leverage == 2
    assert order_request.params["stop_loss"] == 74400
    assert order_request.params["take_profit"] == 73000


def test_consumed_poi_is_not_sent_to_decision_layer():
    payload = {
        "snapshot": {
            "candles": [
                {"timestamp": 1, "open": 100, "high": 101, "low": 99, "close": 100, "volume": 1},
                {"timestamp": 2, "open": 100, "high": 102, "low": 98, "close": 99, "volume": 1},
                {"timestamp": 3, "open": 99, "high": 100, "low": 90, "close": 92, "volume": 1},
                {"timestamp": 4, "open": 92, "high": 106, "low": 91, "close": 105, "volume": 1},
            ]
        },
        "analysis": {
            "metadata": {
                "bos_direction": "bearish",
                "poi_ob_index": 0,
                "bos_break_index": 2,
                "dynamic_structure_low": 90,
                "poi_ob_low": 104,
                "poi_ob_high": 108,
            }
        },
    }

    consumed = _poi_consumed_after_bos(payload)
    payload["poi_consumed"] = consumed

    assert consumed is not None
    assert consumed["reason"] == "poi_ob_consumed_after_bos_completion"
    assert consumed["consumed_index"] == 3
    assert _candidate_trade_from_poi(payload) is None


def test_poi_touch_before_bos_completion_does_not_consume_poi():
    payload = {
        "snapshot": {
            "candles": [
                {"timestamp": 1, "open": 100, "high": 101, "low": 99, "close": 100, "volume": 1},
                {"timestamp": 2, "open": 100, "high": 106, "low": 103, "close": 105, "volume": 1},
                {"timestamp": 3, "open": 105, "high": 107, "low": 102, "close": 103, "volume": 1},
                {"timestamp": 4, "open": 103, "high": 104, "low": 90, "close": 92, "volume": 1},
            ]
        },
        "analysis": {
            "metadata": {
                "bos_direction": "bearish",
                "poi_ob_index": 0,
                "bos_break_index": 3,
                "dynamic_structure_low": 90,
                "poi_ob_low": 104,
                "poi_ob_high": 108,
            }
        },
    }

    consumed = _poi_consumed_after_bos(payload)

    assert consumed is None


def test_current_price_touch_after_bos_completion_consumes_poi():
    payload = {
        "snapshot": {
            "candles": [
                {"timestamp": 1, "open": 100, "high": 101, "low": 99, "close": 100, "volume": 1},
                {"timestamp": 2, "open": 100, "high": 102, "low": 98, "close": 99, "volume": 1},
                {"timestamp": 3, "open": 99, "high": 100, "low": 90, "close": 92, "volume": 1},
            ]
        },
        "analysis": {
            "metadata": {
                "bos_direction": "bearish",
                "poi_ob_index": 0,
                "bos_break_index": 2,
                "dynamic_structure_low": 90,
                "current_price": 105,
                "poi_ob_low": 104,
                "poi_ob_high": 108,
            }
        },
    }

    consumed = _poi_consumed_after_bos(payload)

    assert consumed is not None
    assert consumed["reason"] == "poi_ob_consumed_by_current_price_after_bos_completion"
    assert consumed["current_price"] == 105


def test_order_creation_blocks_consumed_poi_even_with_existing_candidate():
    handler = CryptoBotRunHandler()
    request = BotRunRequest(
        exchange=ExchangeName.BINANCE,
        symbol="BTC/USDT",
        timeframe="15m",
        limit=100,
        transaction=True,
        execute=True,
        use_llm_decision=False,
        amount=50,
        leverage=1,
    )
    payload = {
        "snapshot": {
            "candles": [
                {"timestamp": 1, "open": 100, "high": 101, "low": 99, "close": 100, "volume": 1},
                {"timestamp": 2, "open": 100, "high": 102, "low": 98, "close": 99, "volume": 1},
                {"timestamp": 3, "open": 99, "high": 100, "low": 90, "close": 92, "volume": 1},
                {"timestamp": 4, "open": 92, "high": 106, "low": 91, "close": 105, "volume": 1},
            ]
        },
        "analysis": {
            "action": "hold",
            "metadata": {
                "bos_direction": "bearish",
                "poi_ob_index": 0,
                "bos_break_index": 2,
                "dynamic_structure_low": 90,
                "poi_ob_low": 104,
                "poi_ob_high": 108,
            },
        },
        "candidate_trade": {
            "source": "bos_poi_ob",
            "action": "sell",
            "entry": 104,
            "stop_loss": 108,
            "take_profit": 90,
        },
    }

    order_request, reason = handler._order_request_from_decision(request, payload, None)

    assert order_request is None
    assert "POI already consumed by a later candle" in reason
    assert payload["poi_consumed"]["consumed_index"] == 3
    assert payload["candidate_trade"] is None


def test_task_completes_when_poi_was_consumed_after_bos(monkeypatch):
    handler = CryptoBotRunHandler()
    statuses = []
    logs = []
    publishes = []

    def fake_analyze_market(_request):
        return {
            "snapshot": {
                "candles": [
                    {"timestamp": 1, "open": 100, "high": 101, "low": 99, "close": 100, "volume": 1},
                    {"timestamp": 2, "open": 100, "high": 102, "low": 98, "close": 99, "volume": 1},
                    {"timestamp": 3, "open": 99, "high": 100, "low": 90, "close": 92, "volume": 1},
                    {"timestamp": 4, "open": 92, "high": 106, "low": 91, "close": 105, "volume": 1},
                ]
            },
            "analysis": {
                "action": "hold",
                "bias": "bearish",
                "metadata": {
                    "bos_direction": "bearish",
                    "poi_ob_index": 0,
                    "bos_break_index": 2,
                    "dynamic_structure_low": 90,
                    "dynamic_structure_high": 110,
                    "poi_ob_low": 104,
                    "poi_ob_high": 108,
                },
            },
        }

    monkeypatch.setattr("tradingagents.crypto.tasks.task_handlers.analyze_market", fake_analyze_market)
    monkeypatch.setattr(handler, "log", lambda _task_id, _level, message, context=None: logs.append((message, context or {})))
    monkeypatch.setattr(handler, "set_status", lambda _task_id, status, **_kwargs: statuses.append(status.value))
    monkeypatch.setattr(handler, "publish", lambda _task_id, status, **_kwargs: publishes.append(status.value))
    monkeypatch.setattr(handler, "publish_log", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(handler, "is_canceled", lambda _task_id: False)

    result = handler.handle("task-consumed-poi", {
        "exchange": "binance",
        "symbol": "BTC/USDT",
        "timeframe": "15m",
        "limit": 100,
        "transaction": True,
        "execute": True,
        "use_llm_decision": False,
        "amount": 50,
        "leverage": 1,
    })

    assert result["poi_consumed"]["reason"] == "poi_ob_consumed_after_bos_completion"
    assert statuses == [TaskStatus.COMPLETED.value]
    assert any("auto async task will not submit it to the LLM decision layer" in message for message, _context in logs)


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


def test_llm_reviewer_eligibility_uses_bos_poi_candidate_trade():
    payload = {
        "snapshot": {"exchange": "binance", "symbol": "BTC/USDT", "timeframe": "15m", "candles": []},
        "analysis": TradingHubAnalysis(
            action=SignalAction.HOLD,
            confidence=0.0,
            bias="bearish",
            setup="waiting_dynamic_liquidity_sweep",
        ).model_dump(mode="json"),
        "candidate_trade": {
            "source": "bos_poi_ob",
            "action": "sell",
            "entry": 74140.47,
            "stop_loss": 74380.39,
            "take_profit": 72512.49,
        },
    }

    assert is_llm_decision_eligible(payload) is True


def test_llm_rule_payload_includes_candidate_trade_for_manual_decision():
    payload = {
        "snapshot": {"exchange": "binance", "symbol": "BTC/USDT", "timeframe": "15m", "candles": []},
        "analysis": {"action": "sell", "setup": "manual_bos_poi_decision"},
        "candidate_trade": {
            "source": "bos_poi_ob",
            "action": "sell",
            "entry": 74140.47,
            "stop_loss": 74380.39,
            "take_profit": 72512.49,
        },
        "manual_decision_context": {
            "source": "completed_task_manual_button",
            "original_setup": "waiting_dynamic_liquidity_sweep",
            "execution_mode": "llm_decision_layer_limit_order",
            "use_llm": True,
            "sweep_gate_enabled": False,
        },
    }

    rule_payload = create_rule_payload(payload)

    assert rule_payload["candidate_trade"]["action"] == "sell"
    assert rule_payload["manual_decision_context"]["original_setup"] == "waiting_dynamic_liquidity_sweep"
    assert rule_payload["manual_decision_context"]["use_llm"] is True
    assert rule_payload["manual_decision_context"]["sweep_gate_enabled"] is False


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
    assert state_machine.can_transition(TaskStatus.PENDING, TaskStatus.COMPLETED)
    assert state_machine.can_transition(TaskStatus.ORDERED, TaskStatus.COMPLETED)

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
    assert ("/api/tasks/{task_id}/decision-order", "POST") in routes
    assert ("/api/tasks/{task_id}/llm-decision-order", "POST") in routes
    assert ("/api/tasks/{task_id}", "DELETE") in routes
    assert ("/tasks/{task_id}", "DELETE") in routes
