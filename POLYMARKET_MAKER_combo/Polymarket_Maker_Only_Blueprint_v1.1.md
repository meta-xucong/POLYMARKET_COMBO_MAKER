
# Polymarket **Maker-Only** 执行改造蓝图 v1.1
> 更新时间：2025-11-11（Asia/Tokyo）  
> 适用范围：现有 `Volatility_arbitrage_*` 脚本（仅改执行侧，策略/阈值/统计保持不变）

---

## 0. 目标与边界

- **只用 Maker，不保留/不回退到 Taker（FAK/IOC/FOK/吃单）任何路径。**
- **买端（2.x）**：触发后，始终以 **当前买一（best bid）价** 挂 **GTC** 买单；每 **10s** 轮询：
  - 买一价 **上行** 高于我们挂价 → **撤单** → 以 **新买一价**、**剩余量** 重挂；
  - 否则 **不动作**；
  - 直至 **吃满目标份数** 或 **剩余 < 最小买入量（按名义额≥$1折算）** → 视为完成。
- **卖端（3.x）**：买入吃满后，计算 **地板价 `X = 加权买入均价 + 盈利阈值`**：
  - 初次挂单价 = **max(当前卖一 best ask, X)**；
  - 每 **10s** 轮询：
    - 若卖一 **下行** 且 **仍 ≥ X** → **撤单** → 以 **max(新卖一, X)**、**剩余量** 重挂；
    - **若当前卖一 `< X`** → **撤销挂单并暂停等待**（不下单），直到 **卖一 `≥ X`** 再继续以 `max(卖一, X)` 重挂；
  - 直至 **全部卖出** 或 **剩余 < 最小卖出量（份数2dp下取，<0.01视作完成）**。
- 其它保持原样：**跌幅触发、盈利阈值、倒计时、统计窗口、WS/REST 行情、日志节流**。

---

## 1. 目录与模块改动

- **新增**：`maker_execution.py`
  - `maker_buy_follow_bid(client, token_id, target_size, *, poll_sec=10.0, min_quote_amt=1.0) -> dict`
  - `maker_sell_follow_ask_with_floor_wait(client, token_id, position_size, floor_X, *, poll_sec=10.0) -> dict`
- **修改**：`Volatility_arbitrage_run.py`
  - BUY 分支 → 调用 `maker_buy_follow_bid(...)`
  - 买入吃满后 **直接** 进入 SELL 分支 → 调用 `maker_sell_follow_ask_with_floor_wait(...)`
- **删除/清空**：
  - `Volatility_buy.py` 中 **taker/FAK/IOC** 路径
  - `Volatility_sell.py` 中 **taker/FOK 阶梯** 路径
  - 针对 taker 的“**分片多次买入/卖出**”重试逻辑
- **保留复用**：
  - `execution.py` 的订单**状态归一化**与**客户端方法适配**（如 cancel/get_status 名称差异）
  - 既有的**量化口径**（买价2dp上取/量4dp上取；卖价4dp/量2dp下取；名义额≥$1）

---

## 2. 公共参数（建议默认）

```python
MAKER_POLL_SEC      = 10.0     # 轮询/重挂周期
MIN_QUOTE_AMOUNT    = 1.00     # 最小名义额（美元）
BUY_PRICE_DP        = 2        # 买价小数位（上取）
BUY_SIZE_DP         = 4        # 买量小数位（上取）
SELL_PRICE_DP       = 4        # 卖价小数位（下取后与地板价取最大）
SELL_SIZE_DP        = 2        # 卖量小数位（下取）
```

> 若项目已有统一量化函数，请直接复用，保持口径一致。

---

## 3. 买入执行函数

### 3.1 接口
```python
def maker_buy_follow_bid(
    client, token_id: str, target_size: float,
    *, poll_sec: float = 10.0, min_quote_amt: float = 1.0
) -> dict:
    """
    在买入触发后：以“当前买一价”挂GTC买单，10s查一次；
    若买一价上行>挂价，则撤单按新买一价重挂；
    直至吃满或剩余 < 最小买入量（按名义额≥$1折算）即视为完成。
    返回：{status, avg_price, filled, remaining, orders: [...]}。
    """
```

### 3.2 步骤
1. **初始化**
   - `remaining = ceil_to_dp(target_size, BUY_SIZE_DP)`
   - `filled_total = 0.0`, `notional_sum = 0.0`, `active_order_id = None`
2. **下单子程序** `_place_buy(bid_px, qty)`
   - 价格：`px = round_up_to_dp(bid_px, BUY_PRICE_DP)`
   - 数量：`qty = ceil_to_dp(qty, BUY_SIZE_DP)`
   - 下单参数：`timeInForce=GTC`，`allowPartial=True`
3. **循环（每 poll_sec 秒）**
   - 若 `active_order_id is None`：
     - 获取 **bestBid**（优先 WS，退回 REST）
     - 计算 **eff_qty**：`max(remaining, ceil_to_dp(min_quote_amt / max(bestBid, eps), BUY_SIZE_DP))`
     - `active_order_id = _place_buy(bestBid, eff_qty)`；记录 `last_price = bestBid`
   - 等待 `poll_sec`，查询订单状态（使用通用归一化）：
     - 累计 `filled_total / notional_sum`；更新 `remaining = max(target_size - filled_total, 0)`
   - **完成判定**：
     - 取 `current_bestBid` 估算最小买入量 `min_buyable = ceil_to_dp(min_quote_amt / max(current_bestBid, eps), BUY_SIZE_DP)`
     - 若 `remaining < min_buyable`：**撤单（如仍开放）** → **完成**（`status="FILLED" 或 "FILLED_TRUNCATED"`）
   - **是否重挂（只跟涨）**：
     - 取新 `bestBid`，若 `bestBid >= last_price + one_tick(BUY_PRICE_DP)`：**撤单** → `active_order_id = None`（下一轮按新价重挂）
     - 否则 **维持排队**

---

## 4. 卖出执行函数（含地板价与低于地板暂停）

### 4.1 接口
```python
def maker_sell_follow_ask_with_floor_wait(
    client, token_id: str, position_size: float, floor_X: float,
    *, poll_sec: float = 10.0
) -> dict:
    """
    买入吃满后：以 max(当前卖一价, floor_X) 挂GTC卖单；
    每10s查询：若卖一价下行但仍≥floor_X，则撤单→以max(新卖一, floor_X)重挂；
    若当前卖一<floor_X，则撤消挂单并暂停，直到卖一≥floor_X再继续。
    完成条件：全部卖出或剩余<最小卖出量（份数2dp下取<0.01视作完成）。
    返回：{status, avg_price, filled, remaining, orders: [...]}。
    """
```

### 4.2 步骤
1. **初始化**
   - `remaining = floor_to_dp(position_size, SELL_SIZE_DP)`
   - `filled_total = 0.0`, `notional_sum = 0.0`, `active_order_id = None`
2. **下单子程序** `_place_sell(ask_px, qty)`
   - 价格：`px0 = round_down_to_dp(ask_px, SELL_PRICE_DP)`，`px = max(px0, floor_X)`
   - 数量：`qty = floor_to_dp(qty, SELL_SIZE_DP)`（若 `<0.01` → 视为 dust 完成）
   - 下单参数：`timeInForce=GTC`，`allowPartial=True`
3. **循环（每 poll_sec 秒）**
   - 获取 **bestAsk**
   - **若 `bestAsk < floor_X`**：
     - **撤销当前挂单**（如果有），进入 **等待态**（不下单）；
     - 持续轮询，直到 `bestAsk ≥ floor_X` 才继续；
   - 若 `active_order_id is None`（且 `bestAsk ≥ floor_X`）：
     - `active_order_id = _place_sell(bestAsk, remaining)`；记录 `last_price = placed_px`
   - 等待 `poll_sec`，查询订单状态（归一化）：
     - 累计 `filled_total / notional_sum`；更新 `remaining = max(position_size - filled_total, 0)`
   - **完成判定**：
     - 若 `floor_to_dp(remaining, SELL_SIZE_DP) < 0.01`：**撤单（如仍开放）** → **完成**
   - **是否重挂（只跟跌且不低于地板）**：
     - 取最新 `bestAsk`：
       - 若 `bestAsk < floor_X`：**撤单并暂停等待**（见上）；
       - **elif** `bestAsk <= last_price - one_tick(SELL_PRICE_DP)`：**撤单** → 以 `max(bestAsk, floor_X)` 重挂；
       - 否则 **维持排队**。

---

## 5. 主控编排：`Volatility_arbitrage_run.py`

- **BUY 阶段**
  1) 策略触发（跌幅达到买入阈值）；计算目标买入份数（保留“未指定按$1反推整股上取”的规则）。
  2) 调用 `maker_buy_follow_bid(...)` → 得到 `buy_avg` 与 `filled_buy`。
  3) `filled_buy == 0` → 终止本轮；否则进入 SELL 阶段。

- **SELL 阶段**
  1) 计算地板价 `floor_X = buy_avg + 盈利阈值`（或 `buy_avg * (1+profit_pct)`，与你项目的一致）。
  2) 调用 `maker_sell_follow_ask_with_floor_wait(...)`。
  3) 卖出完成 → 清仓 → 按既有逻辑执行冷却/下一轮。

- **彻底删除 Taker 路径**
  - 移除 `execute_auto_buy()` / `execute_auto_sell()` 的任何调用与引用；
  - 移除阶梯让利、切单退让、taker 拆单重试等废逻辑与参数。

---

## 6. 量化与最小单量口径（与现有保持一致）

- **买端**：
  - 价格 **2dp 上取**；份数 **4dp 上取**；
  - 名义额：按 **eff_qty ≥ ceil($1 / price, 4dp)** 约束。
- **卖端**：
  - 价格 **4dp 下取**，再与 `floor_X` 取最大；
  - 份数 **2dp 下取**；`<0.01` 视为 dust 完成。

---

## 7. 行情、撤单与状态归一化

- 行情：优先 **WS**（bestBid/bestAsk/last），异常时临时回落 **REST**。
- 撤单：统一 `_cancel_order(client, order_id)`，容错不同 SDK 的方法名（`cancel_order/cancel/delete_order...`）。
- 订单状态：复用 `execution.py` 的 **归一化**，获得 `filledAmount/avgPrice/status` 等标准字段。
- 日志：每次 **下单/撤单/重挂** 输出简要日志（价、量、原因：上行/下行/低于地板暂停等）；10s 轮询无变化时不刷屏。

---

## 8. 状态机示意

```
FLAT
 └─(策略触发BUY)→ BUYING_MAKER
                     ├─(bid上行≥1tick)→ 撤单→重挂(新bid)
                     ├─(剩余<最小买入量)→ 完成
                     └─(吃满)→ LONG
LONG
 └─(计算 floor_X)→ SELLING_MAKER
                     ├─(ask < floor_X)→ 撤单→ WAIT_FOR_FLOOR
                     │                    └─(ask ≥ floor_X)→ 继续 SELLING_MAKER
                     ├─(ask下行≥1tick且≥X)→ 撤单→重挂(max(ask, X))
                     ├─(剩余<最小卖出量)→ 完成
                     └─(全部卖出)→ FLAT（并进入既有冷却）
```

---

## 9. 伪代码骨架

### 9.1 买端
```python
def maker_buy_follow_bid(client, token_id, target_size, poll_sec=10.0, min_quote_amt=1.0):
    remaining = ceil_to_dp(target_size, BUY_SIZE_DP)
    filled_total, notional_sum = 0.0, 0.0
    active_order_id, last_price = None, None

    while True:
        if active_order_id is None:
            bid = get_best_bid()  # WS优先，REST兜底
            eff_qty = max(remaining, ceil_to_dp(min_quote_amt / max(bid, eps), BUY_SIZE_DP))
            px = round_up_to_dp(bid, BUY_PRICE_DP)
            active_order_id = post_GTC_buy(client, token_id, px, eff_qty)
            last_price = px

        sleep(poll_sec)
        st = get_order_status_norm(client, active_order_id)
        filled_delta = st.filledAmount - accounted(active_order_id)
        filled_total += filled_delta
        notional_sum += filled_delta * (st.avgPrice or last_price)
        remaining = max(target_size - filled_total, 0)

        # 完成判定（名义额≥$1）
        cur_bid = get_best_bid()
        min_buyable = ceil_to_dp(min_quote_amt / max(cur_bid, eps), BUY_SIZE_DP)
        if remaining < min_buyable:
            cancel_if_open(client, active_order_id)
            return done(avg=notional_sum/filled_total, filled=filled_total)

        # 只跟涨重挂
        new_bid = get_best_bid()
        if new_bid >= last_price + one_tick(BUY_PRICE_DP):
            cancel_if_open(client, active_order_id)
            active_order_id = None
```

### 9.2 卖端（含地板等待）
```python
def maker_sell_follow_ask_with_floor_wait(client, token_id, position_size, floor_X, poll_sec=10.0):
    remaining = floor_to_dp(position_size, SELL_SIZE_DP)
    filled_total, notional_sum = 0.0, 0.0
    active_order_id, last_price = None, None

    while True:
        ask = get_best_ask()

        # 低于地板价：撤单并暂停等待
        if ask < floor_X:
            cancel_if_open(client, active_order_id); active_order_id = None
            sleep(poll_sec); continue  # 等待直到 ask ≥ floor_X（下轮重试）

        if active_order_id is None:
            px = max(round_down_to_dp(ask, SELL_PRICE_DP), floor_X)
            qty = floor_to_dp(remaining, SELL_SIZE_DP)
            if qty < 0.01: return done(avg=notional_sum/max(filled_total,eps), filled=filled_total)
            active_order_id = post_GTC_sell(client, token_id, px, qty)
            last_price = px

        sleep(poll_sec)
        st = get_order_status_norm(client, active_order_id)
        filled_delta = st.filledAmount - accounted(active_order_id)
        filled_total += filled_delta
        notional_sum += filled_delta * (st.avgPrice or last_price)
        remaining = max(position_size - filled_total, 0)

        # 完成判定：卖量2dp下取 < 0.01 视为完成
        if floor_to_dp(remaining, SELL_SIZE_DP) < 0.01:
            cancel_if_open(client, active_order_id)
            return done(avg=notional_sum/max(filled_total,eps), filled=filled_total)

        # 只跟跌重挂（不低于地板）
        new_ask = get_best_ask()
        if new_ask < floor_X:
            cancel_if_open(client, active_order_id); active_order_id = None
            continue
        if new_ask <= last_price - one_tick(SELL_PRICE_DP):
            cancel_if_open(client, active_order_id); active_order_id = None
```

---

## 10. 测试与验收清单

1. **买端-连续上行**：bid 连续上行 3 次 → 发生 3 次“撤单→重挂”，最终吃满；`avg/filled` 统计正确。
2. **买端-碎片最小量**：剩余量按 `$1` 名义额折算，低于最小买入量即完成。
3. **卖端-地板价等待**：ask 跌破 `floor_X` → 立刻撤单并暂停；ask 回到 `≥X` → 自动恢复重挂。
4. **卖端-只跟跌**：ask 在 `≥X` 区间下行≥1tick → 撤单并以 `max(new_ask, X)` 重挂；从不上提价格。
5. **dust 判定**：卖端 `2dp` 下取 `<0.01` 视为完成，不再下单。
6. **日志节流**：10s 轮询无状态变化不打印；仅在下单/撤单/重挂/成交事件打印。
7. **WS/REST 切换**：WS 中断时 REST 能兜底取得 bestBid/bestAsk，不影响循环稳定性。
8. **冷却周期**：一腿完成后，沿用既有冷却进入下一轮，计时与旧版一致。

---

## 11. 变更记录
- **v1.1**（当前）：纳入两项强制要求：
  1) **彻底删除 Taker**，不提供回退路径；
  2) **卖出低于地板价即撤单并暂停等待**，直至卖一恢复到 `≥X`。
- **v1.0**：Maker 执行基础版（买跟 bid、卖跟 ask、10s 轮询重挂）。

---

**说明**：本蓝图仅改“执行层”并保持 **策略/统计/阈值/窗口/倒计时** 不变。若需要，我可以据此在工程中：新增 `maker_execution.py`，改 `Volatility_arbitrage_run.py` 调度分支，清空/删除 Taker 模块；其余文件与参数保持原样，确保最小侵入、可控回归。
