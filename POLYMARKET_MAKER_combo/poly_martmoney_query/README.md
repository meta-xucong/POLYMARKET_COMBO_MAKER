# poly_martmoney_query 使用指南（VPS 场景）

本目录专门说明如何在 VPS 上直接运行仓库中的行情查询脚本，快速查看指定 Polymarket 市场的 YES/NO 报价。

## 环境准备
1. **进入项目根目录**：
   ```bash
   cd /path/to/POLYMARKET_MAKER_combo
   ```
2. **（可选）创建并启用虚拟环境**：
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   ```
3. **安装依赖**（用于 REST/WS 请求与 YAML 配置）：
   ```bash
   pip install py-clob-client websocket-client requests pyyaml
   ```

## 必备参数与环境变量
- **行情查询**仅用到公开接口，但若后续想直接在同一终端下单，建议提前设置：
  - `POLY_KEY`：私钥（十六进制，`0x` 前缀可选）。
  - `POLY_FUNDER`：充值地址/Proxy Wallet。
  - 可选：`POLY_HOST`（默认 `https://clob.polymarket.com`）、`POLY_CHAIN_ID`（默认 `137`）、`POLY_SIGNATURE`（默认 `2`）。
- 在 VPS 里直接导出即可：
  ```bash
  export POLY_KEY="<你的私钥>"
  export POLY_FUNDER="<你的充值地址>"
  ```

## 运行步骤（直接在 VPS 命令行）
1. 进入项目根目录后，执行行情监听脚本：
   ```bash
   python Volatility_arbitrage_price_watch.py --source "https://polymarket.com/market/<market-slug>" --interval 2
   ```
   - 将 `<market-slug>` 替换为具体市场地址中的 slug。脚本会自动解析 YES/NO 的 token_id 并每 2 秒输出一次买/卖/最新价。
2. 若手头已有 YES/NO 的 token_id，可直接传入：
   ```bash
   python Volatility_arbitrage_price_watch.py --source "<YES_id>,<NO_id>" --interval 1
   ```
3. 在 VPS 后台持续运行（可选）：
   ```bash
   nohup python Volatility_arbitrage_price_watch.py --source "https://polymarket.com/market/<market-slug>" --interval 2 > price.log 2>&1 &
   tail -f price.log
   ```

## 常见问题
- **报错 `依赖 requests，请先安装`**：说明未安装 `requests`，按上方步骤重新安装依赖。
- **无输出或长时间卡住**：检查 VPS 网络是否能访问 `wss://clob.polymarket.com`，必要时降低 `--interval` 或重试。
- **想要下单/跟单**：完成上方环境变量配置后，可参考仓库根目录的 `README.md` 运行 `Volatility_arbitrage_run.py` 或 `maker_execution.py`。
