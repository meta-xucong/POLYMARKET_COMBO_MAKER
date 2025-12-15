# Polymarket Volatility Arbitrage Combo Maker

## 项目简介
本项目提供一组围绕 Polymarket CLOB 的交易辅助脚本，重点覆盖波动率套利、组合买单以及实时行情订阅。脚本以最小依赖方式封装了鉴权、行情解析、拆单/跟单、套利保护等能力，便于通过命令行快速完成下单或接入自定义策略。

## 目录总览
- `Volatility_arbitrage_run.py`：交互式组合买单入口，支持在事件页选择多个子问题后按统一份数并行买入。
- `Volatility_arbitrage_main_ws.py`：最简 WebSocket 订阅器，仅负责连接/订阅并回调逐条行情事件。
- `Volatility_arbitrage_main_rest.py`：REST 客户端初始化模块，基于环境变量生成已鉴权的 `ClobClient` 单例。
- `Volatility_arbitrage_price_watch.py`：解析市场/事件链接并订阅 YES/NO 双边行情，按节流间隔输出报价快照。
- `maker_execution.py`：Maker 模式下的下单/跟单辅助，包含跟随买一/卖一的下单逻辑与套利阈值守卫。
- `trading/execution.py`：通用的拆单与重试引擎，以及 `trading.yaml` 载入/校验逻辑。
- 其他策略、测试与示例脚本可根据需要查看源码扩展。

## 环境准备
1. **Python 版本**：建议 Python 3.10+。
2. **依赖安装**：
   ```bash
   pip install py-clob-client websocket-client requests pyyaml
   ```
   `pyyaml` 可选；缺失时 `trading/execution.py` 会回退到简易解析器。
3. **必需环境变量**（用于生成 API 凭证）：
   - `POLY_KEY`：私钥（十六进制，`0x` 前缀可选）。
   - `POLY_FUNDER`：充值地址/Proxy Wallet。
4. **可选环境变量**：
   - `POLY_HOST`（默认 `https://clob.polymarket.com`）
   - `POLY_CHAIN_ID`（默认 `137`，Polygon）
   - `POLY_SIGNATURE`（默认 `2`，EIP-712）

## 配置文件
- `config/trading.yaml` 控制拆单与价格退让策略：
  - `order_slice_min` / `order_slice_max`：单笔下单的最小/最大份数。
  - `retry_attempts` / `price_tolerance_step`：价格退让次数与步长。
  - `wait_seconds` / `poll_interval_seconds`：等待成交与轮询间隔。
  - `order_interval_seconds`：拆单之间的额外延时（默认与 `wait_seconds` 一致）。
  - `min_quote_amount`：单笔最小名义金额（避免 <$1 拆单）。
  - `submarkets`：可选，指定轮询的子市场列表（字符串或包含 `id`/`name` 的字典）。

## 核心脚本与使用方法
### 1. 交互式组合买入（`Volatility_arbitrage_run.py`）
- **作用**：在事件页可一次性选择多个子问题，自动解析 tokenId 并按统一份数买入，支持 WebSocket 订阅实时买一价，屏蔽卖出与循环策略逻辑。
- **运行**：
  ```bash
  python Volatility_arbitrage_run.py
  ```
- **流程概览**：
  1. 启动后校验 API 凭证并初始化 `ClobClient`。
  2. 粘贴事件页或市场页 URL（或直接输入事件/市场 slug）。
  3. 选择需要买入的子问题及方向；可自动开启 WS 订阅以获取实时买一价。
  4. 输入每个子问题的目标份数（默认为 5），脚本将按拆单策略依次下单并输出成交摘要。

### 2. WebSocket 行情订阅（`Volatility_arbitrage_main_ws.py`）
- **作用**：提供极简订阅器，按 token_id 列表订阅并将事件回调给自定义处理函数。
- **典型用法**：
  ```python
  from Volatility_arbitrage_main_ws import ws_watch_by_ids

  ws_watch_by_ids(
      ["yes_token_id", "no_token_id"],
      label="示例订阅",
      on_event=lambda evt: print(evt),
      verbose=False,
  )
  ```
- 依赖 `websocket-client`，可与上层策略组合使用。

### 3. REST 客户端获取（`Volatility_arbitrage_main_rest.py`）
- **作用**：根据环境变量创建 `ClobClient` 并生成 API 凭证，供其他模块共享。
- **用法**：
  ```python
  from Volatility_arbitrage_main_rest import get_client

  client = get_client()
  # 之后可用于下单/查询等操作
  ```

### 4. 行情解析与节流输出（`Volatility_arbitrage_price_watch.py`）
- **作用**：解析事件/市场链接或手动输入的 `YES_id,NO_id`，持续拉取/订阅报价并每隔固定时间输出 YES/NO 的 bid/ask/last。
- **示例**：
  ```bash
  # 解析市场链接
  python - <<'PY'
  from Volatility_arbitrage_price_watch import resolve_token_ids
  print(resolve_token_ids("https://polymarket.com/market/your-market"))
  PY
  ```
  将解析结果传入 WebSocket 订阅或策略层即可。

### 5. Maker 跟单与套利守卫（`maker_execution.py`）
- **作用**：
  - `maker_buy_follow_bid`：在买一价挂单并随行就市上调，直到完成目标买量或低于最小下单份数。
  - `maker_sell_follow_ask_with_floor_wait`：在卖一价与给定 floor 之间跟单，跌破 floor 时撤单等待回升。
  - `PriceSumArbitrageGuard`：跟踪多市场的挂单价格，当价格之和达到阈值时触发停止信号，避免跨市场价格合计超过 1.0。
- **特点**：优先使用外部提供的 WS 最优价函数；缺失时回退到 REST 查询，返回值包含成交统计供策略层更新状态。

### 6. 拆单与重试引擎（`trading/execution.py`）
- **作用**：
  - `ExecutionConfig`：读取 `trading.yaml` 并进行参数校验/类型转换。
  - `ExecutionEngine`：按配置拆分买卖单、按退让步长重试、轮询成交并返回 `ExecutionResult` 摘要。
- **集成方式**：
  ```python
  from trading.execution import ExecutionConfig, ExecutionEngine, ClobPolymarketAPI

  cfg = ExecutionConfig.from_yaml("config/trading.yaml")
  engine = ExecutionEngine(api_client=ClobPolymarketAPI(...), config=cfg)
  result = engine.execute_buy(token_id="...", price=0.45, quantity=10)
  ```

## 测试与自检
- 可先运行 `Volatility_arbitrage_main_rest.py` 进行初始化自检：
  ```bash
  python Volatility_arbitrage_main_rest.py
  ```
  若环境变量配置正确，会输出当前 Host/Chain/Signature 配置并退出，不发起额外网络请求。
- 其他集成测试位于 `tests/` 目录，可根据需要扩展。

## 注意事项
- 脚本默认遵循 Polymarket 官方的最小下单份数（5 份）；如需更改可在对应函数参数中覆盖。
- WebSocket 订阅需要稳定网络连接；若 WS 不可用，脚本会自动回退至 REST 询价。
- 请在测试网或小额环境中先验证策略逻辑，确保私钥与资金安全。
