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
## 交易所
实时获取交易所k线数据,符合

## 技术分析
大周期HTF是小周的LTF的 4倍
永远以大周期的POI为支撑，在小周期寻找机会