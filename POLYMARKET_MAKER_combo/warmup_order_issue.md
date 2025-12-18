# 报价预热后仅挂出单一订单的原因分析

观察到日志中在报价预热阶段提示仍缺少一个 token 的买一价，但随后只对单一 token（8789...）发出了挂单。出现该现象的主要逻辑原因如下：

1. **预热阶段要求拿到全部报价才会启动批量挂单**：`maker_multi_buy_follow_bid` 的预热循环会在缺少任意一个 token 的买一价时持续等待。即便超过预设超时时间，也只会打印提醒，仍然阻塞在预热阶段，直到全部 token 获得有效买一价后才进入并发子线程。因此首轮批量下单在缺价情况下根本不会启动。 【maker_execution.py†L1313-L1367】
2. **后续补单按 token 串行处理**：主流程 `Volatility_arbitrage_run` 在收到这样的 summary 后，`_ensure_targets_filled` 会再次检查各 token 的持仓，发现全部未达目标，于是进入补单逻辑。但补单是对 `missing` 列表逐个 token 调用 `maker_multi_buy_follow_bid([entry])`，并且每个调用都会等待 `_wait_until_orders_done`，即在当前 token 的订单达到终态前不会进入下一个 token。这导致只要第一个 token 的订单长时间处于 `LIVE`/`PENDING` 状态，后续 token 就不会被挂单。 【Volatility_arbitrage_run.py†L1935-L1983】

综合上述，由于预热阶段缺价阻断了首轮批量挂单，而补单流程又采用串行等待策略，最终表现为仅看到第一个 token 的挂单被发出。
