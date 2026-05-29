# TradingAgents 项目知识

本文档记录我通读项目后整理出的开发上下文，供后续 Codex/Agent 协作时快速对齐。

## 项目概览

TradingAgents 是一个基于 LangGraph 的多智能体 LLM 金融交易研究框架，当前包版本为 `0.2.5`。项目把一次交易分析拆成多个角色：分析师团队先生成市场、情绪、新闻、基本面报告；多空研究员展开投资辩论；Trader 生成交易方案；风险管理团队进行激进、中性、保守视角辩论；Portfolio Manager 输出最终交易评级/决策。

项目定位是研究工具，不是投资建议。README 明确说明交易表现受模型、温度、交易区间、数据质量和非确定性影响。

## 快速运行

- 安装包：`pip install .`
- 交互式 CLI：`tradingagents` 或 `python -m cli.main`
- 示例脚本：`python main.py`
- 测试：`pytest`
- Docker：`docker compose run --rm tradingagents`
- Ollama profile：`docker compose --profile ollama run --rm tradingagents-ollama`

`main.py` 展示了最小用法：

```python
from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.default_config import DEFAULT_CONFIG

config = DEFAULT_CONFIG.copy()
ta = TradingAgentsGraph(debug=True, config=config)
_, decision = ta.propagate("NVDA", "2024-05-10")
print(decision)
```

## 关键目录

- `tradingagents/graph/`：LangGraph 工作流编排、条件跳转、传播、checkpoint、反思和信号处理。
- `tradingagents/agents/`：各类 agent 的 prompt、节点工厂和结构化输出 schema。
- `tradingagents/agents/utils/`：工具包装、状态类型、记忆日志、评级解析、结构化输出辅助。
- `tradingagents/dataflows/`：yfinance / Alpha Vantage 数据源实现，以及数据供应商路由。
- `tradingagents/llm_clients/`：多 Provider LLM 客户端抽象、模型目录、API key 环境变量解析。
- `cli/`：Typer + Rich + questionary 实现的交互式命令行界面。
- `tests/`：单元/集成/烟测，覆盖配置、provider、checkpoint、ticker 安全、结构化 agent 等。
- `assets/`：README 和 CLI 截图/示意图资源。

## 核心执行流

主入口类是 `tradingagents.graph.trading_graph.TradingAgentsGraph`。

初始化阶段：

1. 合并/应用配置，并调用 `tradingagents.dataflows.config.set_config()` 更新全局数据流配置。
2. 根据 `llm_provider`、`deep_think_llm`、`quick_think_llm` 创建 deep/quick 两类 LLM。
3. 创建四类 ToolNode：`market`、`social`、`news`、`fundamentals`。
4. 创建 `ConditionalLogic`、`GraphSetup`、`Propagator`、`Reflector`、`SignalProcessor`。
5. `GraphSetup.setup_graph()` 构建 LangGraph `StateGraph(AgentState)`。

默认图顺序：

1. 选中的分析师节点依次运行，每个分析师可通过 tool call 进入对应工具节点，再回到分析师。
2. 每个分析师结束后进入 `create_msg_delete()` 清空消息，只保留 `HumanMessage("Continue")` 占位，兼容 Anthropic。
3. 最后一个分析师结束后进入 `Bull Researcher` / `Bear Researcher` 轮流辩论。
4. 达到 `max_debate_rounds` 后进入 `Research Manager`。
5. `Trader` 生成交易提案。
6. `Aggressive Analyst`、`Conservative Analyst`、`Neutral Analyst` 轮流做风险讨论。
7. 达到 `max_risk_discuss_rounds` 后进入 `Portfolio Manager`，然后结束。

状态类型在 `tradingagents/agents/utils/agent_states.py`。核心字段包括：

- `company_of_interest`、`asset_type`、`trade_date`
- 四类分析报告：`market_report`、`sentiment_report`、`news_report`、`fundamentals_report`
- `investment_debate_state` 和 `risk_debate_state`
- `investment_plan`、`trader_investment_plan`、`final_trade_decision`
- `past_context`，来自历史记忆日志

## Agent 与结构化输出

`tradingagents/agents/__init__.py` 统一导出所有 agent 工厂：

- 分析师：`create_market_analyst`、`create_sentiment_analyst`、`create_news_analyst`、`create_fundamentals_analyst`
- 研究员：`create_bull_researcher`、`create_bear_researcher`
- 管理者：`create_research_manager`、`create_portfolio_manager`
- Trader：`create_trader`
- 风险辩论：`create_aggressive_debator`、`create_neutral_debator`、`create_conservative_debator`

`tradingagents/agents/schemas.py` 给 Research Manager、Trader、Portfolio Manager 增加 Pydantic 结构化输出：

- `ResearchPlan`：五档 `PortfolioRating`，即 `Buy / Overweight / Hold / Underweight / Sell`
- `TraderProposal`：三档 `TraderAction`，即 `Buy / Hold / Sell`
- `PortfolioDecision`：最终五档评级、摘要、投资论点、可选价格目标和时间周期

结构化对象会被 render helper 转回 markdown，保持 CLI 展示、记忆日志和报告保存兼容。

## 配置与环境变量

默认配置在 `tradingagents/default_config.py`，并会自动应用 `TRADINGAGENTS_*` 环境变量覆盖。重要配置：

- 目录：`results_dir`、`data_cache_dir`、`memory_log_path`
- LLM：`llm_provider`、`deep_think_llm`、`quick_think_llm`、`backend_url`
- 推理选项：`google_thinking_level`、`openai_reasoning_effort`、`anthropic_effort`
- 运行控制：`checkpoint_enabled`、`output_language`、`max_debate_rounds`、`max_risk_discuss_rounds`、`max_recur_limit`
- 数据源：`data_vendors`、`tool_vendors`
- 新闻：`news_article_limit`、`global_news_article_limit`、`global_news_lookback_days`、`global_news_queries`
- 反思基准：`benchmark_ticker`、`benchmark_map`

支持的 LLM provider 包括 OpenAI、Google、Anthropic、xAI、DeepSeek、Qwen/Qwen-CN、GLM/GLM-CN、MiniMax/MiniMax-CN、OpenRouter、Ollama、Azure OpenAI。工厂入口是 `tradingagents/llm_clients/factory.py`。

常见 API key 环境变量：

- `OPENAI_API_KEY`
- `GOOGLE_API_KEY`
- `ANTHROPIC_API_KEY`
- `XAI_API_KEY`
- `DEEPSEEK_API_KEY`
- `DASHSCOPE_API_KEY` / `DASHSCOPE_CN_API_KEY`
- `ZHIPU_API_KEY` / `ZHIPU_CN_API_KEY`
- `MINIMAX_API_KEY` / `MINIMAX_CN_API_KEY`
- `OPENROUTER_API_KEY`
- `AZURE_OPENAI_API_KEY`
- `ALPHA_VANTAGE_API_KEY`

## 数据流与工具路由

Agent 使用的工具函数从 `tradingagents/agents/utils/agent_utils.py` 统一导出，实际能力分散在：

- `core_stock_tools.py`
- `technical_indicators_tools.py`
- `fundamental_data_tools.py`
- `news_data_tools.py`

底层数据源路由在 `tradingagents/dataflows/interface.py`：

- `core_stock_apis`：`get_stock_data`
- `technical_indicators`：`get_indicators`
- `fundamental_data`：`get_fundamentals`、`get_balance_sheet`、`get_cashflow`、`get_income_statement`
- `news_data`：`get_news`、`get_global_news`、`get_insider_transactions`

支持 vendor：`yfinance` 和 `alpha_vantage`。配置支持 category-level 和 tool-level 覆盖；tool-level 优先。Alpha Vantage rate limit 会触发 fallback 到后续 vendor。

## 持久化、记忆与 checkpoint

结果日志默认写入 `~/.tradingagents/logs`，缓存默认写入 `~/.tradingagents/cache`。

决策记忆由 `TradingMemoryLog` 维护，默认路径是 `~/.tradingagents/memory/trading_memory.md`。每次完成分析后追加 pending 决策。下一次同 ticker 运行时会尝试拉取后续收益和相对 benchmark 的 alpha，生成 reflection，并把同 ticker 历史和跨 ticker 经验注入 Portfolio Manager 的 prompt。

checkpoint 是可选能力：

- CLI：`tradingagents analyze --checkpoint`
- 清空 checkpoint：`tradingagents analyze --clear-checkpoints`
- Python：设置 `config["checkpoint_enabled"] = True`

checkpoint 使用 LangGraph SQLite saver，按 ticker/date 的 thread id 恢复；成功完成后会清理对应 checkpoint。

## CLI 行为

CLI 主入口是 `cli/main.py`，Typer app 名为 `TradingAgents`。交互式流程会询问 ticker、日期、LLM provider、模型、分析师选择、研究深度、输出语言等，并用 Rich Live 展示：

- agent 进度
- LLM/tool 调用统计
- token 粗略统计
- 当前报告片段
- 完成后是否保存完整报告

CLI 会把中间消息、工具调用和报告片段写入结果目录。报告片段按 section 保存为 markdown。

## 测试与开发注意事项

- 项目使用 `pytest`，配置在 `pyproject.toml`。
- `tests/conftest.py` 自动注入 placeholder API key，避免测试收集或 mock 场景因为缺 key 卡住。
- 测试 marker：`unit`、`integration`、`smoke`。
- 修改 LLM provider、模型、结构化输出时，优先查看 `tests/test_capabilities.py`、`tests/test_model_validation.py`、`tests/test_structured_agents.py`、`tests/test_*_api_key.py`。
- 修改 checkpoint 时查看 `tests/test_checkpoint_resume.py`。
- 修改 ticker/path 处理时查看 `tests/test_safe_ticker_component.py` 和 `tests/test_ticker_symbol_handling.py`。
- 修改数据源配置时查看 `tests/test_dataflows_config.py`。
- 项目要求 Python `>=3.10`，Dockerfile 使用 Python 3.12 slim。

## 代码风格和约定

- 保持现有模块边界：图编排放 `graph/`，agent prompt/节点放 `agents/`，外部数据实现放 `dataflows/`，provider 差异放 `llm_clients/`。
- 新增配置优先接入 `DEFAULT_CONFIG`；若需要环境变量覆盖，把映射加到 `_ENV_OVERRIDES`。
- 新增数据工具时，通常需要同时更新工具包装、`dataflows/interface.py` 的 category/vendor 映射，以及相应 agent 的 ToolNode。
- 新增 provider 时，优先实现 `BaseLLMClient` 子类，并在 `create_llm_client()` 中注册；如是 OpenAI-compatible provider，可走 `OpenAIClient`。
- 不要破坏 exchange-qualified ticker，例如 `.TO`、`.L`、`.HK`、`.T`、`-USD`；相关提示由 `build_instrument_context()` 强调。
- `output_language` 只影响最终报告/agent 可见输出，内部辩论仍可保持英文以提高推理质量。
- 写磁盘路径时注意 ticker 安全，现有日志路径使用 `safe_ticker_component()` 防止路径穿越。

## 当前工作区状态备注

读取项目时观察到：

- `AGENTS.md` 已存在且原本为空，git 状态为新增已暂存。
- `.idea/` 是未跟踪目录。

本文件只记录项目知识，不代表代码行为变更。
## Crypto Trading Hub 交易系统理解

本节是后续实现 `tradingagents/crypto/strategys/` 的硬性上下文。策略代码必须按这里的交易语义推进，不能用简化窗口统计替代结构逻辑。

### 代码边界

- 策略层只判断行情条件是否满足，不负责交易开关、订单金额、API key、账户余额和下单。
- 任务层负责轮询、状态流转、LLM 开关、下单和订单监听。
- K 线数据由服务层获取和缓存，策略只消费 candles。
- Trading Hub 的每个概念都要逐步规则化实现，不能只停留在文档描述。

### 核心流程

标准交易流程：

```text
Daily bias / HTF bias
-> HTF 流动性或 POI
-> LTF 等待流动性清扫
-> LTF CHOCH / BOS
-> FVG / OB / 未缓解 POI
-> 回调入场
-> 目标流动性
```

策略判断顺序：

1. 确认当前使用的周期组合，HTF 通常是 LTF 的 4 倍或更高一级。
2. 永远以 HTF 的方向、POI、流动性目标为背景。
3. LTF 只负责寻找精确入场机会，不单独决定方向。
4. 没有 HTF 背景时，LTF 信号质量下降，应保持 `hold` 或降低权重。

### Daily Bias

- Daily bias 不是预测，而是定义当天优先等待哪一侧交易。
- 上涨背景下，突破并收盘高于昨日高点，通常继续看向更高流动性。
- 突破昨日高点后又收回昨日高点下方，可能是扫流动性后的反转。
- 看跌逻辑反向处理。
- 方向不清晰时，不应强制在小周期交易。

### BOS 与动态结构点

BOS 必须以“有效回调 + 再次突破/跌破关键结构点”为前提。没有有效回调，不能确认新的 BOS。

结构 high / low 不能用固定 window 最高价 / 最低价代替。正确结构点来自最新有效 BOS：

- Bearish BOS：价格经过有效回调后再次跌破前一个有效低点，并形成更低的低点；被跌破前低到这个更低低点之间的反弹最高点，是当前关注的结构 high，也是后续等待被清扫的 buy-side liquidity。
- Bullish BOS：价格经过有效回调后再次突破前一个有效高点，并形成更高的高点；被突破前高到这个更高高点之间的回调最低点，是当前关注的结构 low，也是后续等待被清扫的 sell-side liquidity。

实现必须保存或计算：

- BOS 方向。
- BOS 断点价格。
- BOS 断点 K 线位置。
- BOS 后到当前价格之间的动态结构 high / low。
- 该 high / low 是否被清扫。

禁止事项：

- 禁止只用最近 N 根 K 线最高/最低作为结构 high / low。
- 禁止用 `_recent_range(candles, lookback=20)` 直接代表 liquidity sweep 结构点。
- 禁止没有有效回调就确认 BOS。

### 流动性清扫

- 流动性是系统核心，价格经常先扫一侧流动性，再朝真实方向运行。
- 流动性清扫必须针对 BOS 后动态结构 high / low，而不是任意窗口极值。
- Bearish 背景下，等待价格反弹清扫 BOS 后形成的结构 high。
- Bullish 背景下，等待价格回落清扫 BOS 后形成的结构 low。
- 日志中的“等待价格清扫 low_high”必须输出这个动态结构 high / low。

### IDM / Inducement

- IDM 是防止过早进场的核心。
- BOS 后第一波回调经常是 IDM。
- 没有取走 IDM 前，许多 OB / POI 可能只是陷阱。
- 策略应区分 minor IDM 和 major IDM，并记录当前是否已取走诱因。

### POI / OB / FVG

高质量 POI 通常满足：

- 是造成 MSB / CHOCH 的最后一根异色 K 线。
- 之前存在流动性清扫或 IDM 被取走。
- 后面有 FVG / imbalance。
- 位于 HTF 关键区域、日线高低点、周线高低点、支撑阻力互换区或 Vegas 通道附近。
- 第一次回到 POI 的反应最重要。

低质量 POI：

- 没有流动性或诱因。
- 没有 FVG / imbalance。
- 不在关键结构位置。
- 只由固定窗口极值推导。

### Premium / Discount

- Bullish 背景下，优先在 discount 区找多。
- Bearish 背景下，优先在 premium 区找空。
- PD 不直接触发交易，只用于过滤位置质量。

### LTF 入场

LTF 入场必须建立在 HTF 背景上：

1. HTF 到达或接近 POI。
2. LTF 清扫动态结构流动性。
3. LTF 出现 CHOCH / BOS。
4. LTF 产生 FVG 或未缓解 OB。
5. 回调到 FVG / OB / 极限 OB 时才考虑入场。

LTF 的 CHOCH 可比 HTF 更灵活，但不能脱离 HTF POI 和流动性背景。

### Flip 与失败处理

- 如果预期 POI 失败，并且价格取走有效回调流动性、打破相反结构，应切换为 Flip 模型。
- Flip 不是随意反手，而是确认原供需区失败后的方向切换。
- 任务日志和策略 metadata 应能说明 POI 是否失败、失败后是否满足 Flip 条件。

### Vegas 与 EMA

- Vegas 通道使用 EMA144 / EMA169 判断趋势轨道。
- EMA12 用于确认通道支撑或压制是否有效。
- EMA50 是趋势恢复和回调过滤工具。
- EMA / Vegas 只能作为过滤和加分项，不能单独触发交易。

### 高概率 Setup

高质量 setup 应尽量满足：

- Daily / HTF bias 清晰。
- HTF 已到达或清扫关键流动性。
- 价格处于 HTF POI、OB、FVG、IFC、日线高低点或 Vegas 通道附近。
- IDM / ENGL / EQH / EQL 被取走。
- LTF 出现 CHOCH / BOS。
- LTF 存在 FVG / 未缓解 OB。
- 入场处于 PD 有利区域。
- TP 指向明确内部或外部流动性。

### 程序化输出要求

策略结果 metadata 应逐步补齐：

- `htf_timeframe`
- `ltf_timeframe`
- `daily_bias`
- `bos_direction`
- `bos_break_price`
- `bos_break_index`
- `dynamic_structure_high`
- `dynamic_structure_low`
- `liquidity_swept`
- `idm_taken`
- `poi_type`
- `poi_range`
- `pd_zone`
- `choch_confirmed`
- `fvg_exists`
- `setup_quality`
