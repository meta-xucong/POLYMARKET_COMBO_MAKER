# 精度与下单失败的可能原因（按官方 CLOB 约束对照）

* 官方 CLOB 文档要求：`price` 需满足撮合端公布的 `decimals`（tick = 10^-decimals，通常在 2~6 之间），`size` 需在 6 位小数以内且为正数。只要不违反这两条，撮合端不会因为精度拒单。
* 代码中价格精度的处理：默认用 `BUY_PRICE_DP=2`，但会根据 orderbook 带回的 `decimals` 提升精度，并用 `_round_up_to_dp` 将买一价向上取整到 tick【F:maker_execution.py†L40-L67】【F:maker_execution.py†L631-L655】。
* 份数（size）的处理：无论是按固定份数还是按名义金额倒推，最终都会用 `BUY_SIZE_DP=4` 进行向上取整，再作为 `size` 下单【F:maker_execution.py†L40-L67】【F:maker_execution.py†L636-L674】。

## 本次“固定金额倒推份数”可能触发的精度问题
1. **目标金额 / buy 一价除法后的结果被强制向上取 4 位小数**：如果价格精度更高（如 `decimals=6`，tick=1e-6），`size` 只有 4 位会被接受，但 `price*size` 会比目标金额略大，可能触发交易所侧的 “quote too small/too large for given price” 校验。
2. **最小 quote 金额的换算**：`min_quote_amt / price` 也被取 4 位小数；当 price 较小（如 0.01 → 0.07 → 0.87 的剧烈波动），换算出的最小份数在重挂时可能因为向上取整叠加而大于撮合端允许的整数化 size，导致拒单【F:maker_execution.py†L636-L674】。
3. **价格精度外推不足**：如果 orderbook 响应缺失 `decimals` 字段，代码会一直用默认的 2 位价格精度；若实际 tick 更细（4~6 位），`_round_up_to_dp` 生成的价格（如 0.87）在换算份数时会与精确买一价存在偏差，引发撮合端“price not aligned to tick”或“quote amount mismatch”拒单。

> 建议：抓取失败时 `adapter.create_order` 返回的具体报错，并在日志中补充下单 payload（price/size）与市场 `decimals`、`min_quote_amt` 等元信息，才能确认是否为 tick/精度问题。必要时可将 `BUY_SIZE_DP` 调高到 6，或在缺失 `decimals` 时主动调用合约/REST 获取价格精度后再下单。
