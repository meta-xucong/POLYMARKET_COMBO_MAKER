# 报价预热后仅挂出单一订单的原因分析

观察到日志中在报价预热阶段提示仍缺少一个 token 的买一价，但随后只对单一 token（8789...）发出了挂单。出现该现象的主要逻辑原因如下：

1. **预热超时会直接返回**：`maker_multi_buy_follow_bid` 在报价预热循环里，如果在超时时间内仍有 token 未拿到有效报价，会打印“报价预热已等待约 … 仍未获取”并设置 `warmup_timeout_hit=True` 与 `warmup_missing` 列表；随后触发 `if warmup_timeout_hit and warmup_missing:` 分支，提前返回 summary，不再并发启动各子市场线程，因此首轮批量下单根本没有执行。 【maker_execution.py†L1313-L1372】
2. **后续补单按 token 串行处理**：主流程 `Volatility_arbitrage_run` 在收到这样的 summary 后，`_ensure_targets_filled` 会再次检查各 token 的持仓，发现全部未达目标，于是进入补单逻辑。但补单是对 `missing` 列表逐个 token 调用 `maker_multi_buy_follow_bid([entry])`，并且每个调用都会等待 `_wait_until_orders_done`，即在当前 token 的订单达到终态前不会进入下一个 token。这导致只要第一个 token 的订单长时间处于 `LIVE`/`PENDING` 状态，后续 token 就不会被挂单。 【Volatility_arbitrage_run.py†L1935-L1983】

综合上述，虽然你设定了“必须获取到全部报价才允许一起挂单”，但预热超时后函数直接返回，后续补单又采取串行策略并等待前一单完成，导致实际只看到第一个 token 的挂单。
