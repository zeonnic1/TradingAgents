# TradingAgents Crypto Hub 本地运行

本文档只描述当前加密货币交易平台的本地运行方式：FastAPI 业务服务本地调试，MySQL/Redis 通过公共中间件 compose 启动。

## 0. 异步任务架构

```text
tradingagents/crypto/
  api.py                # API 层：FastAPI 路由、WebSocket 订阅
  task_service.py       # Service 层：任务提交、取消、Celery 分发
  tasks.py              # Worker 层：Celery 统一入口
  task_factory.py       # 工厂模式：按 task_type 创建 Handler
  task_handlers.py      # Handler 层：具体任务业务流程
  task_mixins.py        # Mixin：日志、状态更新、Redis 推送
  task_fsm.py           # FSM：PENDING/ORDERED/COMPLETED/CANCELED/FAILED
  ledger.py             # ORM Model：MySQL 表结构和持久化
  models.py             # Pydantic Schema：API 请求/响应模型
  connect_manager.py    # WebSocket 连接管理 + Redis Pub/Sub
```

任务生命周期：

```text
PENDING -> ORDERED -> COMPLETED
PENDING -> CANCELED
PENDING -> FAILED
ORDERED -> CANCELED
ORDERED -> FAILED
```

语义说明：

- `PENDING`：任务正在轮询策略和 LLM 决策，尚未下单。
- `ORDERED`：策略满足后创建订单，订单进入监听。
- `COMPLETED`：订单监听命中止盈或止损后完成。
- `CANCELED` / `FAILED`：取消或失败终态。

`COMPLETED`、`CANCELED`、`FAILED` 是终态，不能再流转。

## 1. 准备 Python 环境

```powershell
cd D:\code\TradingAgents
.\venv\Scripts\Activate.ps1
pip install -e .
```

如果虚拟环境不存在：

```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -e .
```

## 2. 启动公共中间件

MySQL 和 Redis 使用独立文件：

```powershell
docker compose -f docker-compose-public.yaml up -d
```

兼容旧拼写的文件也保留了：

```powershell
docker compose -f docker-copmpose-pulbic.yaml up -d
```

检查状态：

```powershell
docker compose -f docker-compose-public.yaml ps
```

## 3. 环境变量

FastAPI、Celery 和交易所配置从环境变量读取，入口在 `tradingagents/crypto/config.py`。

最小本地配置：

```env
CRYPTO_TRADING_LIVE_ENABLED=false
CRYPTO_DEFAULT_EXCHANGE=binance
CRYPTO_DEFAULT_SYMBOL=BTC/USDT
CRYPTO_DEFAULT_TIMEFRAME=15m
CELERY_BROKER_URL=redis://localhost:6379/0
CELERY_RESULT_BACKEND=redis://localhost:6379/1
CRYPTO_DATABASE_URL=mysql+pymysql://tradingagents:tradingagents@localhost:3306/tradingagents_crypto
```

实盘模式才需要交易所 API key：

```env
CRYPTO_TRADING_LIVE_ENABLED=true
BINANCE_API_KEY=
BINANCE_API_SECRET=
OKX_API_KEY=
OKX_API_SECRET=
OKX_API_PASSWORD=
BYBIT_API_KEY=
BYBIT_API_SECRET=
```

PyCharm/IDEA 不会自动读取项目 `.env`。可以在 Run/Debug Configuration 的 `Environment variables` 中填写，或安装 EnvFile 插件指定 `D:\code\TradingAgents\.env`。当前代码未内置 `python-dotenv` 自动加载。

## 4. 启动 FastAPI

推荐显式指定端口，避免 Windows 默认端口权限/占用问题：

```powershell
.\venv\Scripts\python.exe -m uvicorn tradingagents.crypto.api:app --reload --host 127.0.0.1 --port 8000
```

打开：

```text
http://127.0.0.1:8000
```

不要直接打开 `file:///D:/code/TradingAgents/tradingagents/crypto/static/index.html` 调试接口；该方式没有 FastAPI API 服务上下文。

## 5. 常用 API

健康检查：

```powershell
curl http://127.0.0.1:8000/api/health
```

同步分析/运行：

```powershell
curl -X POST http://127.0.0.1:8000/api/bot/run `
  -H "Content-Type: application/json" `
  -d "{\"exchange\":\"binance\",\"symbol\":\"BTC/USDT\",\"timeframe\":\"15m\",\"limit\":300,\"transaction\":false,\"use_llm_decision\":true,\"amount\":0,\"leverage\":1}"
```

手动触发 LLM Trader / Portfolio 决策。该接口会先运行 Python 规则工具，自动把规则输出 JSON、最近 K 线、账户/订单上下文发送给 LLM，让 LLM 复核是否批准交易：

```powershell
curl -X POST http://127.0.0.1:8000/api/llm/decision `
  -H "Content-Type: application/json" `
  -d "{\"exchange\":\"binance\",\"symbol\":\"BTC/USDT\",\"timeframe\":\"15m\",\"limit\":300}"
```

LLM 使用原项目配置：

```env
TRADINGAGENTS_LLM_PROVIDER=openai
TRADINGAGENTS_DEEP_THINK_LLM=gpt-5.4
TRADINGAGENTS_LLM_BACKEND_URL=
OPENAI_API_KEY=
```

查询交易所资金。paper mode 返回模拟资金；live mode 会用交易所 API key 调用 `fetch_balance`，可用于校验 key 是否有效：

```powershell
curl "http://127.0.0.1:8000/api/account/balance?exchange=binance"
```

查询收益：

```powershell
curl http://127.0.0.1:8000/api/performance
```

查询历史订单：

```powershell
curl http://127.0.0.1:8000/api/orders/history
```

## 6. 启动 Celery Worker

前端“异步运行”依赖 Celery worker。Windows 本地调试必须使用 `--pool=solo`，否则 Celery 默认 prefork pool 可能触发 `ValueError: not enough values to unpack (expected 3, got 0)`。

```powershell
.\venv\Scripts\Activate.ps1
celery -A tradingagents.crypto.tasks.celery_app worker --loglevel=INFO --pool=solo
```

异步运行：

```powershell
curl -X POST http://127.0.0.1:8000/tasks `
  -H "Content-Type: application/json" `
  -d "{\"exchange\":\"binance\",\"symbol\":\"BTC/USDT\",\"timeframe\":\"15m\",\"limit\":300,\"transaction\":false,\"use_llm_decision\":true,\"amount\":0,\"leverage\":1}"
```

兼容旧接口仍可用：

```powershell
curl -X POST http://127.0.0.1:8000/api/bot/run-async `
  -H "Content-Type: application/json" `
  -d "{\"exchange\":\"binance\",\"symbol\":\"BTC/USDT\",\"timeframe\":\"15m\",\"limit\":300,\"transaction\":false,\"use_llm_decision\":true,\"amount\":0,\"leverage\":1}"
```

WebSocket 任务完成推送：

```text
ws://127.0.0.1:8000/ws/{task_id}
```

FastAPI 会订阅 Redis channel `task_status_{task_id}`。Celery worker 完成、失败或取消任务时会向该 channel 发布结果，WebSocket 收到消息后推送给前端并关闭连接。

查询单个任务状态：

```powershell
curl http://127.0.0.1:8000/api/tasks/{task_id}
```

取消异步任务：

```powershell
curl -X POST http://127.0.0.1:8000/api/tasks/{task_id}/cancel
```

说明：取消接口会撤销未执行的 Celery 任务，并把数据库任务状态改为 `canceled`。如果 Windows 本地使用 `--pool=solo` 且任务已经进入执行中，Celery 通常不能可靠硬中断当前函数；此时服务会保留 `canceled` 状态，任务结束后的结果不会覆盖成 `completed`。

删除终态任务：

```powershell
curl -X DELETE http://127.0.0.1:8000/api/tasks/{task_id}
```

只有 `COMPLETED`、`CANCELED`、`FAILED` 这类终态任务允许删除；`PENDING` 和 `ORDERED` 需要先取消。

任务中心，包含任务、任务日志、订单监听：

```powershell
curl http://127.0.0.1:8000/api/tasks
```

只查任务日志：

```powershell
curl "http://127.0.0.1:8000/api/tasks/logs?limit=200"
curl "http://127.0.0.1:8000/api/tasks/logs?task_id={task_id}"
```

对异步任务触发 LLM 决策。只要任务不是 `CANCELED` 或 `FAILED`，都可以调用；系统会把这条任务当前的规则 JSON 发送给 LLM，由 LLM 判断是否批准交易：

```powershell
curl -X POST http://127.0.0.1:8000/api/tasks/{task_id}/llm-decision
```

## 7. 下单前校验逻辑

当 `transaction=true` 且策略产生 `buy` 或 `sell` 时，服务会先获取交易所账户资金：

- `use_llm_decision=true`：策略满足后还需要 LLM 批准，才会下单。
- `use_llm_decision=false`：只按 Python 策略信号下单，不让 LLM 参与批准。
- `CRYPTO_TASK_POLL_MAX_ATTEMPTS=0`：任务会一直保持 `PENDING` 并轮询策略，直到满足下单条件或被取消。

- `CRYPTO_TRADING_LIVE_ENABLED=false`：使用 paper 客户端，返回模拟资金，不触发真实交易所私有接口。
- `CRYPTO_TRADING_LIVE_ENABLED=true`：先检查当前交易所必需 API key 是否完整，再调用 ccxt `fetch_balance()` 校验凭证和资金信息，之后才调用 `create_order()`。

OKX 需要 `OKX_API_PASSWORD`；Binance/Bybit 需要 key 和 secret。

## 8. 业务服务容器运行

先启动公共中间件：

```powershell
docker compose -f docker-compose-public.yaml up -d
```

再启动业务容器：

```powershell
docker compose up crypto-api crypto-worker
```

业务容器连接中间件时使用 Docker 网络服务名：

```env
CELERY_BROKER_URL=redis://redis:6379/0
CELERY_RESULT_BACKEND=redis://redis:6379/1
CRYPTO_DATABASE_URL=mysql+pymysql://tradingagents:tradingagents@mysql:3306/tradingagents_crypto
```

## 9. 测试和配置检查

```powershell
pytest tests\test_crypto_trading_hub.py
python -m compileall tradingagents\crypto tests\test_crypto_trading_hub.py
docker compose -f docker-compose-public.yaml config
docker compose config
```
