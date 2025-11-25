# Polymarket 代码自检蓝图 v1.0（专用版｜对齐 main_autotrade_polymarket.py）

> 本版以 **main_autotrade_polymarket.py（老版本）** 为唯一权威，覆盖此前与之不一致的设定。
> 目标：保证**最小但稳定**的自动化：WS 监听 → 触发 FAK 买入 → 达标后 FOK（五档让利）卖出 → 单轮退出。

---

## 变更摘要（相对旧蓝图）
- **默认份数（留空）= 整股上取**：`ceil(1/ask)`（0 位小数），保证名义额 ≥ $1。
- **BUY 精度“三件套”** 回归老版：**价 5dp 上取、份 4dp 上取、金额 2dp 上取**（金额仅用于校验/日志，不入单）。
- **执行链路回归老版**：主控不直接下单 → **统一调用执行器**
  - BUY → `Volatility_buy.execute_auto_buy(..., FAK)`
  - SELL → `Volatility_sell.execute_auto_sell(..., FOK 五档让利)`
- 保留“单轮模式”，卖出成功后退出（若要循环，另行极小补丁）。

---

## 自检章节 & 验收项

### 1) 语法与结构
- [ ] 所有模块 `py_compile` 通过（至少：`Volatility_arbitrage_run.py / Volatility_buy.py / Volatility_sell.py / Volatility_arbitrage_main_ws.py / Volatility_arbitrage_main_rest.py / Volatility_arbitrage_price_watch.py / Volatility_arbitrage_strategy.py`）。
- [ ] **入口存在**：`Volatility_arbitrage_run.py` 内含 `def main()` 与 `if __name__ == "__main__": main()`。
- [ ] **无重复定义/残片**：无重复 `def`，无孤立片段（如 `log .`、`resp =`、`order =`、`tpg .`）。

### 2) 运行入口与交互（CLI）
- [ ] 交互提示齐全：URL/子问题选择/方向（YES|NO）/份数（可留空）/买入触发价/盈利百分比。
- [ ] 日志标签：`[INIT] / [CHOICE] / [RUN] / [PX] / [HINT] / [TRADE] / [DONE] / [WARN] / [ERR]`。

### 3) 数据源与解析
- [ ] 事件页 → 子问题解析 → tokenIds（YES/NO）正确解析；
- [ ] WS 订阅仅**目标 tokenIds**；回调只更新 `latest[token_id]`；
- [ ] 1s 节流输出：`bid/ask/last` 与 token_id（`[PX]`）。

### 4) **精度与名义额规则**（以老版为准）
- BUY：
  - [ ] **默认份数**（留空）= `ceil(1/ask)` → **整股**（0 dp）。
  - [ ] 下单前规范化（由执行器完成）：
    - 价格：**5 dp 上取**（不低于 `bestAsk`，利于 FAK 立即成交）。
    - 份数：**4 dp 上取**（taker shares ≤ 4dp）。
    - 金额（USDC）：**2 dp 上取**（用于校验/日志）。
  - [ ] 名义额兜底：若 `price*size < 1`，执行器应以 `$1/price` 为基准重算（上取），保证 ≥ $1。
- SELL（五档让利 FOK）：
  - [ ] 参考价：`bestBid`。
  - [ ] 五档让利：`0% / 1% / 2% / 3% / 4%`（从 bestBid 向下给价）。
  - [ ] 价格：**4 dp**（通常下取或按让利后再量化至 4dp）。
  - [ ] 份数：**2 dp 下取**（避免超额）。

### 5) 执行链路（必须与老版一致）
- [ ] `Volatility_arbitrage_run.py` **不**直接 `post_order`：
  - BUY 必须调用 `from Volatility_buy import execute_auto_buy`（FAK）。
  - SELL 必须调用 `from Volatility_sell import execute_auto_sell`（FOK 五档）。
- [ ] `Volatility_buy.py`：
  - 存在 `_q5_up`（价 5dp）、`_q4_up`（量 4dp）、`_q2_up`（金额 2dp）；
  - `_min_legal_pair(...)` 使用 `max(s_hint, s_need)`（$1 覆盖），并做 5/4/2 量化；
  - `execute_auto_buy(...)` 打包 FAK 订单并提交。
- [ ] `Volatility_sell.py`：
  - 存在 `_floor_2dp(...)`、`_ladder_prices(...)`（五档序列），`execute_auto_sell(...)` 构造 FOK 订单并依序尝试；
  - 卖出 **仅执行**（不含策略阈值判断）。

### 6) 状态机（单轮）
- [ ] 初始状态：未持仓 → 监听；
- [ ] 触发：`ask <= buy_trigger` → 生成份数（默认整股或用户输入） → BUY（FAK）；
- [ ] 持仓：记录 `buy_fill_px`（以下单价或回包为准）；
- [ ] 达标：`bid >= buy_fill_px * (1 + profit_pct)` → SELL（FOK 五档）；
- [ ] 卖出成功：`[DONE]` 日志，并退出程序；（**如需循环**，移除 `break` 并重置状态）。

### 7) 日志与回归
- [ ] `[PX]` 每秒输出：`bid/ask/last`、token_id；
- [ ] `[HINT]`：默认整股提示 & $1 兜底触发提示；
- [ ] `[TRADE][BUY] / [TRADE][SELL]`：回包 `status`、`px/size`（若可得）；
- [ ] `[WARN]/[ERR]`：重要失败路径有可读提示（无需冗余兜底）。

---

## 快速验收（烟囱）
1. 同一市场与方向；份数留空；买入触发价略高于当前 `ask`；盈利百分比 = `1`；
2. 期待：**FAK 买入**（成功）→ **达标** → **FOK 五档让利卖出**（成功）→ `[DONE]` 并退出；
3. 若遭遇 400（极个别市场）：先复核 URL、tokenIds、份数是否整数（默认）与 BUY 走的是执行器；必要时可临时把 BUY 价量化降为 2dp 做保底重试（**可选建议，不纳入本基线**）。

---

## 清单（可脚本化自检）
- [ ] `py_compile` 全部 OK。
- [ ] `run.py` 同时存在 `def main()` 与 `__main__` 入口。
- [ ] `run.py` **包含** `from Volatility_buy import execute_auto_buy` / `execute_auto_buy(` 调用。
- [ ] `buy.py` **包含** `Decimal("0.00001") / Decimal("0.0001") / Decimal("0.01")`（5/4/2）。
- [ ] `sell.py` **包含** `_floor_2dp`、`_ladder_prices`、FOK 下单逻辑。
- [ ] 日志关键字齐全：`[INIT]/[CHOICE]/[RUN]/[PX]/[HINT]/[TRADE]/[DONE]`。
- [ ] 无重复 `def`、无明显残片（`log .`|`resp =`|`order =`|`tpg .`）。

---

## 维护说明
- 本蓝图作为**长期基线**沿用；若未来服务端精度校验再收紧，可新增“**保底模式**”（BUY 价临时降为 2dp 的重试开关），但默认仍以老版 **5/4/2 + 整股** 为准。

