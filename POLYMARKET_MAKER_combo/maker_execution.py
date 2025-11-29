# -*- coding: utf-8 -*-
"""Maker-only execution helpers for Polymarket trading.

This module provides two high-level routines used by the volatility arbitrage
script:

``maker_buy_follow_bid``
    Place a GTC buy order at the current best bid and keep adjusting the order
    upward whenever the market bid rises. The routine polls every ``poll_sec``
    seconds, accumulates fills, and exits once the requested quantity is filled
    (or the remainder falls below the minimum notional requirement).

``maker_sell_follow_ask_with_floor_wait``
    Place a GTC sell order at ``max(best_ask, floor_X)`` and follow the ask
    downward without crossing below the provided floor price. If the ask drops
    below the floor the routine cancels the working order and waits until the
    market recovers above the floor before re-posting.

Both helpers favour websocket snapshots supplied by the caller via
``best_bid_fn`` / ``best_ask_fn``. When these callables are absent or return
``None`` the helpers fall back to best-effort REST lookups using the provided
client.

The functions return lightweight dictionaries that summarise order history and
fill statistics so that the strategy layer can update its internal state.
"""
from __future__ import annotations

import math
import time
import threading
from collections import deque
from collections.abc import Callable, Iterable, Mapping
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Iterable, List, Mapping, NamedTuple, Optional, Tuple

from trading.execution import ClobPolymarketAPI


BUY_PRICE_DP = 2
# 官方 CLOB 对 size 支持到 6 位小数；按名义金额倒推份数时使用 6 位精度能减少
# price*size 与目标 quote 之间的偏差，避免因精度不足被拒单。
BUY_SIZE_DP = 6
SELL_PRICE_DP = 4
SELL_SIZE_DP = 2
_MIN_FILL_EPS = 1e-9
# 官方文档要求 size 不能低于 5，默认遵循该限制；如需其他阈值可通过参数覆写。
DEFAULT_MIN_ORDER_SIZE = 5.0


class PriceSumArbitrageGuard:
    """Track working maker prices and stop when the sum crosses the cap.

    该守卫用于跨市场的套利校验：如果所有挂单价格之和达到或超过设定阈值
    （默认 1.0），则触发停止信号，供上层统一撤单或转入卖出流程。
    """

    def __init__(self, *, threshold: float = 1.0) -> None:
        self.threshold = float(threshold)
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._violation_total: Optional[float] = None
        self.prices: Dict[str, float] = {}
        self.fills: Dict[str, Dict[str, float]] = {}

    def _mark_violation(self, total: float) -> None:
        self._violation_total = total
        self._stop_event.set()

    def should_stop(self) -> bool:
        return self._stop_event.is_set()

    def violation_total(self) -> Optional[float]:
        return self._violation_total

    def record_fill(self, token_id: str, filled_size: float, avg_price: Optional[float]) -> None:
        with self._lock:
            self.fills[token_id] = {
                "size": float(filled_size),
                "avg_price": float(avg_price) if avg_price is not None else 0.0,
            }

    def clear_price(self, token_id: str) -> None:
        with self._lock:
            self.prices.pop(token_id, None)

    def validate_proposed_prices(self, proposed: Mapping[str, float]) -> bool:
        with self._lock:
            total = sum(float(v) for v in proposed.values())
            if total >= self.threshold - 1e-12:
                self._mark_violation(total)
                return False
            return True

    def commit_price(self, token_id: str, price: float) -> bool:
        with self._lock:
            self.prices[token_id] = float(price)
            total = sum(self.prices.values())
            if total >= self.threshold - 1e-12:
                self._mark_violation(total)
                return False
            return True


class UnifiedQtyController:
    """Recalculate uniform order size when best bids change.

    通过跟踪所有子市场的最新买一价并结合总预算，动态倒推出统一的下单份数。
    当份数发生显著变化时会递增版本号，供下游撮合线程感知并调整挂单。
    """

    def __init__(
        self,
        total_budget: float,
        initial_prices: Mapping[str, float],
        *,
        min_qty_delta: float = 0.01,
        min_price_delta: float = 1e-6,
    ) -> None:
        self.total_budget = float(total_budget)
        self._lock = threading.Lock()
        self._version = 0
        self._last_change: Optional[Dict[str, float]] = None
        self._event = threading.Event()
        self.prices: Dict[str, float] = {str(k): float(v) for k, v in initial_prices.items() if float(v) > 0}
        self.target_size: float = _floor_to_dp(self._compute_size(), BUY_SIZE_DP)
        self.min_qty_delta = max(float(min_qty_delta), 0.0)
        self.min_price_delta = max(float(min_price_delta), 0.0)

    def _compute_size(self) -> float:
        total_price = sum(self.prices.values())
        if total_price <= 0:
            return 0.0
        return self.total_budget / total_price

    def update_price(self, token_id: str, new_price: float, *, source: str = "ws") -> None:
        token_id = str(token_id).strip()
        if not token_id:
            return
        try:
            price_val = float(new_price)
        except (TypeError, ValueError):
            return
        if price_val <= 0:
            return
        with self._lock:
            old_price = self.prices.get(token_id)
            if old_price is not None and abs(old_price - price_val) < self.min_price_delta:
                return
            self.prices[token_id] = price_val
            new_qty = _floor_to_dp(self._compute_size(), BUY_SIZE_DP)
            if abs(new_qty - self.target_size) < self.min_qty_delta:
                return
            old_qty = self.target_size
            self.target_size = new_qty
            self._version += 1
            self._last_change = {
                "old_price": float(old_price) if old_price is not None else 0.0,
                "new_price": price_val,
                "old_qty": old_qty,
                "new_qty": new_qty,
                "version": self._version,
                "token_id": token_id,
                "source": source,
            }
            self._event.set()

    def current_version(self) -> int:
        with self._lock:
            return self._version

    def consume_update(self, last_seen: int) -> Optional[Dict[str, float]]:
        with self._lock:
            if self._version == last_seen or self._last_change is None:
                return None
            change = dict(self._last_change)
            self._event.clear()
            return change

    def wait_for_update(self, last_seen: int, timeout: Optional[float] = None) -> Optional[Dict[str, float]]:
        if self._version != last_seen:
            return self.consume_update(last_seen)
        if not self._event.wait(timeout=timeout):
            return None
        return self.consume_update(last_seen)


def _round_up_to_dp(value: float, dp: int) -> float:
    factor = 10 ** dp
    return math.ceil(value * factor - 1e-12) / factor


def _round_down_to_dp(value: float, dp: int) -> float:
    factor = 10 ** dp
    return math.floor(value * factor + 1e-12) / factor


def _ceil_to_dp(value: float, dp: int) -> float:
    factor = 10 ** dp
    return math.ceil(value * factor - 1e-12) / factor


def _floor_to_dp(value: float, dp: int) -> float:
    factor = 10 ** dp
    return math.floor(value * factor + 1e-12) / factor


def _coerce_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        try:
            return float(raw)
        except ValueError:
            return None
    return None


class PriceSample(NamedTuple):
    price: float
    decimals: Optional[int]


def _infer_price_decimals(value: Any, *, max_dp: int = 6) -> Optional[int]:
    candidate: Optional[Decimal] = None
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        try:
            candidate = Decimal(raw)
        except (InvalidOperation, ValueError):
            return None
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            candidate = Decimal(str(value))
        except (InvalidOperation, ValueError):
            return None
    else:
        return None

    candidate = candidate.normalize()
    if candidate.is_zero():
        return 0
    exponent = candidate.as_tuple().exponent
    if exponent >= 0:
        return 0
    return min(-int(exponent), max_dp)


def _extract_best_price(payload: Any, side: str) -> Optional[PriceSample]:
    def _is_pending_state(obj: Mapping[str, Any]) -> bool:
        """Detect pending/initializing states where prices are unreliable."""

        pending_keys = (
            "state",
            "status",
            "market_state",
            "marketState",
            "clob_state",
            "phase",
        )
        for key in pending_keys:
            val = obj.get(key)
            if isinstance(val, str) and val.strip().lower().startswith("pending"):
                return True
        if obj.get("active") is False:
            return True
        return False

    numeric = _coerce_float(payload)
    if numeric is not None:
        decimals = _infer_price_decimals(payload)
        return PriceSample(float(numeric), decimals)

    if isinstance(payload, Mapping):
        if _is_pending_state(payload):
            return None
        primary_keys = {
            "bid": (
                "best_bid",
                "bestBid",
                "bid",
                "highestBid",
                "bestBidPrice",
                "bidPrice",
                "buy",
            ),
            "ask": (
                "best_ask",
                "bestAsk",
                "ask",
                "offer",
                "best_offer",
                "bestOffer",
                "lowestAsk",
                "sell",
            ),
        }[side]
        for key in primary_keys:
            if key in payload:
                extracted = _extract_best_price(payload[key], side)
                if extracted is not None:
                    return extracted

        ladder_keys = {
            "bid": ("bids", "bid_levels", "buy_orders", "buyOrders"),
            "ask": ("asks", "ask_levels", "sell_orders", "sellOrders", "offers"),
        }[side]
        for key in ladder_keys:
            if key in payload:
                ladder = payload[key]
                if isinstance(ladder, Iterable) and not isinstance(ladder, (str, bytes, bytearray)):
                    for entry in ladder:
                        if isinstance(entry, Mapping) and "price" in entry:
                            decimals = _infer_price_decimals(entry.get("price"))
                            candidate = _coerce_float(entry.get("price"))
                            if candidate is not None:
                                return PriceSample(float(candidate), decimals)
                        extracted = _extract_best_price(entry, side)
                        if extracted is not None:
                            return extracted

        for value in payload.values():
            extracted = _extract_best_price(value, side)
            if extracted is not None:
                return extracted
        return None

    if isinstance(payload, Iterable) and not isinstance(payload, (str, bytes, bytearray)):
        for item in payload:
            extracted = _extract_best_price(item, side)
            if extracted is not None:
                return extracted
        return None

    return None


def _fetch_best_price(client: Any, token_id: str, side: str) -> Optional[PriceSample]:
    method_candidates = (
        ("get_market_orderbook", {"market": token_id}),
        ("get_market_orderbook", {"token_id": token_id}),
        ("get_market_orderbook", {"market_id": token_id}),
        ("get_order_book", {"market": token_id}),
        ("get_order_book", {"token_id": token_id}),
        ("get_orderbook", {"market": token_id}),
        ("get_orderbook", {"token_id": token_id}),
        ("get_market", {"market": token_id}),
        ("get_market", {"token_id": token_id}),
        ("get_market_data", {"market": token_id}),
        ("get_market_data", {"token_id": token_id}),
        ("get_ticker", {"market": token_id}),
        ("get_ticker", {"token_id": token_id}),
    )

    for name, kwargs in method_candidates:
        fn = getattr(client, name, None)
        if not callable(fn):
            continue
        try:
            resp = fn(**kwargs)
        except TypeError:
            continue
        except Exception:
            continue

        payload = resp
        if isinstance(resp, tuple) and len(resp) == 2:
            payload = resp[1]
        if isinstance(payload, Mapping) and {"data", "status"} <= set(payload.keys()):
            payload = payload.get("data")

        best = _extract_best_price(payload, side)
        if best is not None:
            return PriceSample(float(best.price), best.decimals)
    return None


def _best_price_info(
    client: Any,
    token_id: str,
    best_fn: Optional[Callable[[], Optional[float]]],
    side: str,
) -> Optional[PriceSample]:
    """Return the best price, filtering out placeholder/invalid values."""

    def _normalize(sample: Optional[PriceSample]) -> Optional[PriceSample]:
        if sample is None:
            return None
        try:
            price_val = float(sample.price)
        except (TypeError, ValueError):
            return None

        # WS 初始占位价常为 0.001，属于明显无效值，需过滤为 None 以防误用。
        if price_val <= 0.001 + 1e-12:
            return None
        return PriceSample(price_val, sample.decimals)

    if best_fn is not None:
        try:
            val = best_fn()
        except Exception:
            val = None
        if val is not None and val > 0:
            return _normalize(PriceSample(float(val), _infer_price_decimals(val)))

    return _normalize(_fetch_best_price(client, token_id, side))


def _best_bid(
    client: Any, token_id: str, best_bid_fn: Optional[Callable[[], Optional[float]]]
) -> Optional[float]:
    info = _best_price_info(client, token_id, best_bid_fn, "bid")
    if info is None:
        return None
    return info.price


def _best_bid_info(
    client: Any, token_id: str, best_bid_fn: Optional[Callable[[], Optional[float]]]
) -> Optional[PriceSample]:
    return _best_price_info(client, token_id, best_bid_fn, "bid")


def _best_ask(
    client: Any, token_id: str, best_ask_fn: Optional[Callable[[], Optional[float]]]
) -> Optional[float]:
    info = _best_price_info(client, token_id, best_ask_fn, "ask")
    if info is None:
        return None
    return info.price


def _cancel_order(client: Any, order_id: Optional[str]) -> bool:
    if not order_id:
        return False
    method_names = (
        "cancel_order",
        "cancelOrder",
        "cancel",
        "cancel_orders",
        "cancelOrders",
        "delete_order",
        "deleteOrder",
        "cancel_limit_order",
        "cancelLimitOrder",
        "cancel_open_order",
        "cancelOpenOrder",
    )

    targets: deque[Any] = deque([client])
    visited: set[int] = set()
    while targets:
        obj = targets.popleft()
        if obj is None:
            continue
        obj_id = id(obj)
        if obj_id in visited:
            continue
        visited.add(obj_id)
        for name in method_names:
            method = getattr(obj, name, None)
            if not callable(method):
                continue
            try:
                method(order_id)
                return True
            except TypeError:
                try:
                    method(id=order_id)
                    return True
                except Exception:
                    continue
            except Exception:
                continue
        for attr in ("client", "api", "private"):
            nested = getattr(obj, attr, None)
            if nested is not None:
                targets.append(nested)
    return False


def _order_tick(dp: int) -> float:
    return 10 ** (-dp)


def _update_fill_totals(
    order_id: str,
    status_payload: Dict[str, Any],
    accounted: Dict[str, float],
    notional_sum: float,
    last_known_price: float,
    *,
    status_text: Optional[str] = None,
    expected_full_size: Optional[float] = None,
) -> Tuple[float, float, float]:
    filled_amount = float(status_payload.get("filledAmount", 0.0) or 0.0)
    avg_price = status_payload.get("avgPrice")
    if avg_price is None:
        avg_price = last_known_price
    else:
        avg_price = float(avg_price)

    if filled_amount <= _MIN_FILL_EPS and status_text:
        status_upper = status_text.upper()
        if status_upper in {"FILLED", "MATCHED", "COMPLETED", "EXECUTED"}:
            if expected_full_size is not None and expected_full_size > 0:
                filled_amount = max(filled_amount, float(expected_full_size))

    previous = accounted.get(order_id, 0.0)
    delta = max(filled_amount - previous, 0.0)
    accounted[order_id] = filled_amount
    notional_sum += delta * avg_price
    return filled_amount, avg_price, notional_sum


def maker_buy_follow_bid(
    client: Any,
    token_id: str,
    target_size: Optional[float] = None,
    *,
    target_notional: Optional[float] = None,
    poll_sec: float = 10.0,
    min_quote_amt: float = 1.0,
    min_order_size: float = DEFAULT_MIN_ORDER_SIZE,
    best_bid_fn: Optional[Callable[[], Optional[float]]] = None,
    stop_check: Optional[Callable[[], bool]] = None,
    sleep_fn: Callable[[float], None] = time.sleep,
    progress_probe: Optional[Callable[[], None]] = None,
    progress_probe_interval: float = 60.0,
    state_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
    price_dp: Optional[int] = None,
    external_fill_probe: Optional[Callable[[], Optional[float]]] = None,
    shared_active_prices: Optional[Dict[str, float]] = None,
    price_update_guard: Optional[Callable[[Mapping[str, float]], bool]] = None,
    price_sum_guard: Optional[PriceSumArbitrageGuard] = None,
    qty_controller: Optional[UnifiedQtyController] = None,
) -> Dict[str, Any]:
    """Continuously maintain a maker buy order following the market bid.

    当 ``target_notional`` 提供时，优先按等额（USDC）买入目标执行，并在报价上
    行重挂时同步倒推出新的份数，保证每次挂单的名义金额一致；否则退化为按
    份数执行的旧逻辑。
    """

    use_notional = target_notional is not None
    if not use_notional and target_size is None:
        raise ValueError("must provide either target_notional or target_size")

    goal_notional = max(float(target_notional or 0.0), 0.0) if use_notional else None
    goal_size = max(_ceil_to_dp(float(target_size or 0.0), BUY_SIZE_DP), 0.0) if not use_notional else 0.0
    api_min_qty = 0.0
    if min_order_size and min_order_size > 0:
        api_min_qty = _ceil_to_dp(float(min_order_size), BUY_SIZE_DP)
        if not use_notional:
            goal_size = max(goal_size, api_min_qty)
    if (use_notional and (goal_notional or 0.0) <= 0.0) or (not use_notional and goal_size <= 0):
        return {
            "status": "SKIPPED",
            "avg_price": None,
            "filled": 0.0,
            "remaining": 0.0,
            "orders": [],
        }

    adapter = ClobPolymarketAPI(client)
    orders: List[Dict[str, Any]] = []
    records: Dict[str, Dict[str, Any]] = {}
    accounted: Dict[str, float] = {}

    remaining = goal_size
    filled_total = 0.0
    notional_sum = 0.0

    active_order: Optional[str] = None
    active_price: Optional[float] = None
    missing_orderbook_count = 0

    final_status = "PENDING"
    base_price_dp = BUY_PRICE_DP if price_dp is None else max(int(price_dp), 0)
    price_dp_active = base_price_dp
    size_dp_active = min(price_dp_active, BUY_SIZE_DP)
    tick = _order_tick(price_dp_active)
    # 采用统一的两级缩减步长，先用 0.01，多次失败后升级到 0.1
    size_tick = 0.01
    shortage_retry_count = 0
    base_min_shrink_interval = 1.0
    min_shrink_interval = base_min_shrink_interval
    last_shrink_time = 0.0

    no_fill_poll_count = 0

    next_probe_at = 0.0
    controller_version = qty_controller.current_version() if qty_controller else 0

    def _remaining_qty_est(price_hint: Optional[float]) -> float:
        if not use_notional:
            return max(goal_size - filled_total, 0.0)
        remaining_quote = max((goal_notional or 0.0) - notional_sum, 0.0)
        if remaining_quote <= 0:
            return 0.0
        px = price_hint if price_hint and price_hint > 0 else active_price
        px = px if px and px > 0 else None
        if px is None:
            return _ceil_to_dp(remaining_quote, size_dp_active)
        return _ceil_to_dp(remaining_quote / px, size_dp_active)

    def _should_stop() -> bool:
        if price_sum_guard and price_sum_guard.should_stop():
            return True
        return bool(stop_check and stop_check())

    def _emit_state(status_text: Optional[str] = None) -> None:
        if state_callback is None:
            return
        payload = {
            "token_id": token_id,
            "status": status_text or final_status,
            "filled": notional_sum if use_notional else filled_total,
            "remaining": (max((goal_notional or 0.0) - notional_sum, 0.0) if use_notional else max(goal_size - filled_total, 0.0)),
        }
        try:
            state_callback(payload)
        except Exception as exc:
            print(f"[MAKER][BUY] 状态回调异常：{exc}")

    def _maybe_update_price_dp(observed: Optional[int]) -> None:
        nonlocal price_dp_active, size_dp_active, tick
        if observed is None:
            return
        desired = max(base_price_dp, int(observed))
        if desired != price_dp_active:
            price_dp_active = desired
            size_dp_active = min(price_dp_active, BUY_SIZE_DP)
            tick = _order_tick(price_dp_active)
            print(f"[MAKER][BUY] 检测到市场价格精度 -> decimals={price_dp_active}")

    def _is_insufficient_balance(value: object) -> bool:
        def _text_has_shortage(text: str) -> bool:
            lowered = text.lower()
            shortage_keywords = ("insufficient", "not enough")
            balance_keywords = ("balance", "fund", "allowance")
            return any(key in lowered for key in shortage_keywords) and any(
                key in lowered for key in balance_keywords
            )

        if hasattr(value, "error_message"):
            try:
                if _is_insufficient_balance(getattr(value, "error_message")):
                    return True
            except Exception:
                pass
        if hasattr(value, "response"):
            try:
                if _is_insufficient_balance(getattr(value, "response")):
                    return True
            except Exception:
                pass
        if hasattr(value, "args"):
            try:
                for arg in getattr(value, "args", ()):
                    if _is_insufficient_balance(arg):
                        return True
            except Exception:
                pass

        if isinstance(value, dict):
            for key in ("error", "message", "detail", "reason", "status"):
                if key in value and _is_insufficient_balance(value[key]):
                    return True
        try:
            return _text_has_shortage(str(value))
        except Exception:
            return False

    def _reset_shortage_recovery(note: str) -> None:
        nonlocal shortage_retry_count, min_shrink_interval, last_shrink_time

        if shortage_retry_count > 0 or min_shrink_interval != base_min_shrink_interval:
            shortage_retry_count = 0
            min_shrink_interval = base_min_shrink_interval
            last_shrink_time = time.monotonic()
            print(note)

    def _handle_balance_shortage(reason: str, min_viable: float, *, fatal: bool = False) -> bool:
        nonlocal goal_size, goal_notional, remaining, active_order, active_price, final_status, shortage_retry_count, size_tick, last_shrink_time, min_shrink_interval

        print(reason)
        min_shrink_interval = max(min_shrink_interval, base_min_shrink_interval)
        if active_order:
            _cancel_order(client, active_order)
            rec = records.get(active_order)
            if rec is not None:
                rec["status"] = "CANCELLED"
        active_order = None
        active_price = None
        if fatal:
            final_status = "INSUFFICIENT_BALANCE"
            cancel_all = getattr(client, "cancel_open_orders", None)
            if callable(cancel_all):
                try:
                    cancel_all()
                except Exception:
                    pass
            return True

        current_remaining = _remaining_qty_est(active_price)
        remaining_quote = max((goal_notional or 0.0) - notional_sum, 0.0) if use_notional else None
        if (not use_notional and current_remaining <= _MIN_FILL_EPS) or (use_notional and remaining_quote is not None and remaining_quote <= _MIN_FILL_EPS):
            final_status = "FILLED" if filled_total > _MIN_FILL_EPS else final_status
            return True
        shortage_retry_count += 1
        if shortage_retry_count > 100 and size_tick < 0.1:
            size_tick = 0.1
            print("[MAKER][BUY] 余额不足重试超过 100 次，提升缩减步长至 0.1。")

        now = time.monotonic()
        elapsed = now - last_shrink_time
        if elapsed < min_shrink_interval:
            sleep_duration = min_shrink_interval - elapsed
            if sleep_duration > 0:
                sleep_fn(sleep_duration)
            now = time.monotonic()
        last_shrink_time = now

        shrink_candidate = _ceil_to_dp(max(current_remaining - size_tick, 0.0), size_dp_active)
        min_viable = max(min_viable or 0.0, api_min_qty or 0.0)
        if shrink_candidate > _MIN_FILL_EPS and (
            not min_viable or shrink_candidate + _MIN_FILL_EPS >= min_viable
        ):
            print(
                "[MAKER][BUY] 重新调整买入目标 -> "
                f"old={current_remaining:.{size_dp_active}f} new={shrink_candidate:.{size_dp_active}f}"
            )
            if use_notional:
                price_hint = active_price if active_price and active_price > 0 else 1.0
                goal_notional = notional_sum + shrink_candidate * price_hint
            else:
                goal_size = filled_total + shrink_candidate
            remaining = _remaining_qty_est(active_price)
            return False
        print("[MAKER][BUY] 无法在满足最小下单量的前提下继续缩减，终止买入。")
        final_status = "FILLED_TRUNCATED" if filled_total > _MIN_FILL_EPS else "SKIPPED_TOO_SMALL"
        return True

    def _validate_price_cap(price_to_set: Optional[float]) -> bool:
        proposed: Dict[str, float] = {}
        if shared_active_prices is not None:
            proposed.update(shared_active_prices)
        if price_to_set is None:
            proposed.pop(token_id, None)
        else:
            proposed[token_id] = float(price_to_set)
        if price_sum_guard and not price_sum_guard.validate_proposed_prices(proposed):
            return False
        if price_update_guard and not price_update_guard(proposed):
            return False
        return True

    def _maybe_apply_unified_qty(price_hint: Optional[float]) -> bool:
        """Apply latest unified qty when WS-driven prices shift.

        Returns True when the caller should restart the polling cycle (e.g.,
        after cancelling an outdated挂单).
        """

        nonlocal goal_size, remaining, controller_version, active_order, active_price

        if qty_controller is None or use_notional:
            return False

        update = qty_controller.consume_update(controller_version)
        if update is None:
            return False

        try:
            controller_version = int(update.get("version", controller_version))
        except Exception:
            controller_version += 1

        new_qty = _ceil_to_dp(float(update.get("new_qty", goal_size)), size_dp_active)
        old_qty = goal_size
        goal_size = max(new_qty, filled_total)
        remaining = _remaining_qty_est(price_hint)
        old_px = _coerce_float(update.get("old_price")) or 0.0
        new_px = _coerce_float(update.get("new_price")) or 0.0
        reason = update.get("source") or "price-change"
        print(
            "[MAKER][BUY] 统一份数重算(%s) -> old_px=%.{dp}f new_px=%.{dp}f old_qty=%.{sq}f new_qty=%.{sq}f".format(
                dp=price_dp_active, sq=size_dp_active
            )
            % (reason, old_px, new_px, old_qty, goal_size)
        )

        desired_slice = max(goal_size - filled_total, 0.0)
        current_slice = desired_slice
        rec = records.get(active_order) if active_order else None
        if rec is not None:
            try:
                current_slice = float(rec.get("size", desired_slice) or desired_slice)
            except Exception:
                current_slice = desired_slice

        delta = abs(current_slice - desired_slice)
        threshold = qty_controller.min_qty_delta if qty_controller is not None else _MIN_FILL_EPS
        if delta >= max(threshold, _MIN_FILL_EPS) and active_order:
            print(
                f"[MAKER][BUY] 挂单数量需调整，撤单重挂 | old_qty={current_slice:.{size_dp_active}f} new_qty={desired_slice:.{size_dp_active}f}"
            )
            _cancel_order(client, active_order)
            rec = records.get(active_order)
            if rec is not None:
                rec["status"] = "CANCELLED"
            active_order = None
            active_price = None
            if shared_active_prices is not None:
                shared_active_prices[token_id] = float(new_px or 0.0)
            if price_sum_guard is not None and new_px:
                price_sum_guard.commit_price(token_id, float(new_px))
            return True

        return False

    while True:
        if state_callback and next_probe_at <= 0:
            _emit_state("PENDING")
            next_probe_at = time.time() + max(progress_probe_interval, poll_sec, 1e-6)
        if _should_stop():
            if active_order:
                _cancel_order(client, active_order)
                rec = records.get(active_order)
                if rec is not None:
                    rec["status"] = "CANCELLED"
            final_status = "STOPPED"
            break

        if _maybe_apply_unified_qty(active_price):
            continue

        if active_order is None:
            if api_min_qty and not use_notional and remaining + _MIN_FILL_EPS < api_min_qty:
                final_status = "FILLED_TRUNCATED" if filled_total > _MIN_FILL_EPS else "SKIPPED_TOO_SMALL"
                break
            bid_info = _best_bid_info(client, token_id, best_bid_fn)
            if bid_info is None:
                missing_orderbook_count += 1
                if state_callback:
                    _emit_state("NO_ORDERBOOK")
                if missing_orderbook_count in {6, 12, 24, 48}:
                    print(
                        "[MAKER][BUY] 已连续 %d 次无法获取订单簿，继续等待报价…"
                        % missing_orderbook_count
                    )
                sleep_fn(poll_sec)
                continue
            missing_orderbook_count = 0
            bid = bid_info.price
            if bid <= 0:
                missing_orderbook_count += 1
                if state_callback:
                    _emit_state("NO_BID")
                if missing_orderbook_count in {6, 12, 24, 48}:
                    print(
                        "[MAKER][BUY] 已连续 %d 次买一价为 0，继续等待报价…"
                        % missing_orderbook_count
                    )
                sleep_fn(poll_sec)
                continue
            _maybe_update_price_dp(bid_info.decimals)
            px = _round_up_to_dp(bid, price_dp_active)
            if px <= 0:
                sleep_fn(poll_sec)
                continue
            min_qty = 0.0
            if min_quote_amt and min_quote_amt > 0:
                min_qty = _ceil_to_dp(min_quote_amt / max(px, 1e-9), size_dp_active)
            if use_notional:
                remaining_quote = max((goal_notional or 0.0) - notional_sum, 0.0)
                desired_qty = remaining_quote / max(px, 1e-9)
                if api_min_qty and desired_qty + _MIN_FILL_EPS < api_min_qty:
                    msg = (
                        "[MAKER][BUY] 按名义金额倒推出的下单份数 %.6f 低于官方最小下单量 %.6f，终止执行。"
                        % (desired_qty, api_min_qty)
                    )
                    print(msg)
                    raise SystemExit(msg)
                eff_qty = max(desired_qty, min_qty, api_min_qty)
                # 按市场精度向下取整，使 price*size 不至于显著超过目标 quote。
                eff_qty = _floor_to_dp(eff_qty, size_dp_active)
                # 如果下取整后不满足最小下单约束，则再补齐到约束值。
                min_bound = max(min_qty, api_min_qty)
                if eff_qty + _MIN_FILL_EPS < min_bound:
                    eff_qty = _ceil_to_dp(min_bound, size_dp_active)
            else:
                eff_qty = max(remaining, min_qty)
                if api_min_qty:
                    eff_qty = max(eff_qty, api_min_qty)
                eff_qty = _ceil_to_dp(eff_qty, size_dp_active)
            if eff_qty <= 0:
                final_status = "SKIPPED"
                break
            if not _validate_price_cap(px):
                final_status = "STOPPED"
                if price_sum_guard is not None:
                    price_sum_guard.commit_price(token_id, px)
                break
            payload = {
                "tokenId": token_id,
                "side": "BUY",
                "price": px,
                "size": eff_qty,
                "timeInForce": "GTC",
                "type": "GTC",
                "allowPartial": True,
            }
            try:
                response = adapter.create_order(payload)
            except Exception as exc:
                print(
                    "[MAKER][BUY] 下单异常，payload=%s decimals=%s use_notional=%s exc=%r"
                    % (payload, price_dp_active, use_notional, exc)
                )
                min_viable = max(min_qty or 0.0, api_min_qty or 0.0)
                precision_hint = str(getattr(exc, "args", [None])[0]).lower()
                precision_error = "precision" in precision_hint or "decimal" in precision_hint
                if precision_error and price_dp_active < BUY_SIZE_DP:
                    fallback_qty = _floor_to_dp(eff_qty, size_dp_active)
                    if fallback_qty + _MIN_FILL_EPS < min_viable:
                        fallback_qty = _ceil_to_dp(min_viable, size_dp_active)
                    if fallback_qty > 0:
                        payload["size"] = fallback_qty
                        try:
                            response = adapter.create_order(payload)
                        except Exception:
                            raise
                        eff_qty = fallback_qty
                        print(
                            "[MAKER][BUY] 检测到精度相关拒单，按 %d 位小数重试 -> qty=%.6f"
                            % (price_dp_active, eff_qty)
                        )
                        # 成功重试后继续正常流程
                    else:
                        raise
                elif _is_insufficient_balance(exc):
                    should_stop = _handle_balance_shortage(
                        "[MAKER][BUY] 下单失败，疑似余额不足，尝试缩减买入目标后重试。",
                        min_viable,
                        fatal=True,
                    )
                    if should_stop:
                        break
                    continue
                else:
                    raise
            order_id = str(response.get("orderId"))
            record = {
                "id": order_id,
                "side": "buy",
                "price": px,
                "size": eff_qty,
                "status": "OPEN",
                "filled": 0.0,
            }
            orders.append(record)
            records[order_id] = record
            accounted[order_id] = 0.0
            active_order = order_id
            active_price = px
            _reset_shortage_recovery("[MAKER][BUY] 挂单成功，退出余额不足重试模式。")
            if shared_active_prices is not None:
                shared_active_prices[token_id] = float(px)
            if price_sum_guard is not None:
                price_sum_guard.commit_price(token_id, float(px))
            if progress_probe:
                interval = max(progress_probe_interval, poll_sec, 1e-6)
                try:
                    progress_probe()
                except Exception as probe_exc:
                    print(f"[MAKER][BUY] 进度探针执行异常：{probe_exc}")
                next_probe_at = time.time() + interval
            if state_callback:
                _emit_state("OPEN")
                next_probe_at = time.time() + max(progress_probe_interval, poll_sec, 1e-6)
            print(
                f"[MAKER][BUY] 挂单 -> price={px:.{price_dp_active}f} qty={eff_qty:.{size_dp_active}f} remaining={remaining:.{size_dp_active}f}"
            )
            continue

        sleep_fn(poll_sec)
        if (
            active_order
            and progress_probe_interval > 0
            and time.time() >= max(next_probe_at, 0.0)
        ):
            if progress_probe:
                try:
                    progress_probe()
                except Exception as probe_exc:
                    print(f"[MAKER][BUY] 进度探针执行异常：{probe_exc}")
            if state_callback:
                _emit_state("OPEN")
            interval = max(progress_probe_interval, poll_sec, 1e-6)
            next_probe_at = time.time() + interval
        try:
            status_payload = adapter.get_order_status(active_order)
        except Exception as exc:
            print(f"[MAKER][BUY] 查询订单状态异常：{exc}")
            status_payload = {"status": "UNKNOWN", "filledAmount": accounted.get(active_order, 0.0)}

        record = records.get(active_order)
        status_text = str(status_payload.get("status", "UNKNOWN"))
        record_size = None
        if record is not None:
            try:
                record_size = float(record.get("size", 0.0) or 0.0)
            except Exception:
                record_size = None
        last_price_hint = active_price
        if last_price_hint is None:
            last_price_hint = _coerce_float(status_payload.get("avgPrice"))
        if last_price_hint is None:
            last_price_hint = 0.0
        previous_filled_total = filled_total
        current_bid: Optional[float] = active_price

        filled_amount, avg_price, notional_sum = _update_fill_totals(
            active_order,
            status_payload,
            accounted,
            notional_sum,
            float(last_price_hint),
            status_text=status_text,
            expected_full_size=record_size,
        )
        filled_total = sum(accounted.values())
        if external_fill_probe is not None:
            try:
                external_filled = external_fill_probe()
            except Exception as probe_exc:
                print(f"[MAKER][BUY] 外部持仓校对异常：{probe_exc}")
                external_filled = None
                if external_filled is not None and external_filled > filled_total + _MIN_FILL_EPS:
                    filled_total = external_filled
                    remaining = _remaining_qty_est(current_bid)
                    print(
                        f"[MAKER][BUY] 校对持仓后更新累计成交 -> filled={filled_total:.{size_dp_active}f} "
                        f"remaining={remaining:.{size_dp_active}f}"
                    )
        if price_sum_guard is not None:
            price_sum_guard.record_fill(token_id, filled_total, avg_price)
        if filled_total > previous_filled_total + _MIN_FILL_EPS:
            no_fill_poll_count = 0
        elif shortage_retry_count > 0:
            no_fill_poll_count += 1
        else:
            no_fill_poll_count = 0
        if shortage_retry_count > 0 and no_fill_poll_count >= 30:
            print(
                "[MAKER][BUY] 挂单连续 30 次未检测到新增成交，强制校对仓位/余额后重挂。"
            )
            if external_fill_probe is not None:
                try:
                    external_filled = external_fill_probe()
                except Exception as probe_exc:
                    print(f"[MAKER][BUY] 外部持仓校对异常：{probe_exc}")
                    external_filled = None
                if external_filled is not None and external_filled > filled_total + _MIN_FILL_EPS:
                    filled_total = external_filled
                    print(
                        f"[MAKER][BUY] 二次校对后更新累计成交 -> filled={filled_total:.{size_dp_active}f}"
                    )
            remaining = _remaining_qty_est(current_bid)
            _cancel_order(client, active_order)
            rec = records.get(active_order)
            if rec is not None:
                rec["status"] = "CANCELLED"
            active_order = None
            active_price = None
            no_fill_poll_count = 0
            continue
        remaining = _remaining_qty_est(current_bid)
        status_text_upper = status_text.upper()
        if record is not None:
            record["filled"] = filled_amount
            record["status"] = status_text_upper
            if avg_price is not None:
                record["avg_price"] = avg_price
            price_display = record.get("price", active_price)
            total_size = float(record.get("size", 0.0) or 0.0)
            remaining_slice = max(total_size - filled_amount, 0.0)
            if price_display is not None:
                print(
                    f"[MAKER][BUY] 挂单状态 -> price={float(price_display):.{price_dp_active}f} "
                    f"filled={filled_amount:.{size_dp_active}f} remaining={remaining_slice:.{size_dp_active}f} "
                    f"status={status_text_upper}"
                )
        if state_callback:
            _emit_state(status_text_upper)

        current_bid_info = _best_bid_info(client, token_id, best_bid_fn)
        current_bid = current_bid_info.price if current_bid_info is not None else None
        if current_bid_info is not None:
            _maybe_update_price_dp(current_bid_info.decimals)
        min_buyable = 0.0
        if min_quote_amt and min_quote_amt > 0 and current_bid and current_bid > 0:
            min_buyable = _ceil_to_dp(min_quote_amt / max(current_bid, 1e-9), size_dp_active)
        if api_min_qty:
            min_buyable = max(min_buyable, api_min_qty)

        remaining_quote = max((goal_notional or 0.0) - notional_sum, 0.0) if use_notional else None
        if (
            (not use_notional and remaining <= _MIN_FILL_EPS)
            or (use_notional and remaining_quote is not None and remaining_quote <= _MIN_FILL_EPS)
            or (min_buyable and remaining < min_buyable)
        ):
            if active_order:
                _cancel_order(client, active_order)
                rec = records.get(active_order)
                if rec is not None:
                    rec["status"] = "CANCELLED"
                active_order = None
            if (not use_notional and remaining <= _MIN_FILL_EPS) or (use_notional and remaining_quote is not None and remaining_quote <= _MIN_FILL_EPS):
                final_status = "FILLED"
            else:
                final_status = "FILLED_TRUNCATED" if filled_total > _MIN_FILL_EPS else "SKIPPED_TOO_SMALL"
            break

        if current_bid is not None and active_price is not None and current_bid >= active_price + tick - 1e-12:
            proposed = dict(shared_active_prices) if shared_active_prices is not None else {}
            proposed[token_id] = float(current_bid)
            if not _validate_price_cap(proposed.get(token_id)):
                final_status = "STOPPED"
                _cancel_order(client, active_order)
                rec = records.get(active_order)
                if rec is not None:
                    rec["status"] = "CANCELLED"
                active_order = None
                active_price = None
                break
            print(
                f"[MAKER][BUY] 买一上行 -> 撤单重挂 | old={active_price:.{price_dp_active}f} new={current_bid:.{price_dp_active}f}"
            )
            _cancel_order(client, active_order)
            rec = records.get(active_order)
            if rec is not None:
                rec["status"] = "CANCELLED"
            active_order = None
            active_price = None
            if shared_active_prices is not None:
                shared_active_prices[token_id] = float(current_bid)
            if price_sum_guard is not None:
                price_sum_guard.commit_price(token_id, float(current_bid))
            continue

        final_states = {"FILLED", "MATCHED", "COMPLETED", "EXECUTED"}
        cancel_states = {"CANCELLED", "CANCELED", "REJECTED", "EXPIRED"}
        invalid_states = {"INVALID"}
        status_shortage = _is_insufficient_balance(status_text) or _is_insufficient_balance(status_payload)
        if shortage_retry_count > 0 and not status_shortage:
            _reset_shortage_recovery("[MAKER][BUY] 状态恢复正常，退出余额不足重试模式。")
        if status_text_upper in invalid_states or status_shortage:
            reason = "[MAKER][BUY] 订单被撮合层标记为 INVALID，尝试调整买入目标后重试。"
            if status_shortage and status_text_upper not in invalid_states:
                reason = "[MAKER][BUY] 订单状态提示余额不足，尝试调整买入目标后重试。"
            min_viable = max(min_buyable or 0.0, api_min_qty or 0.0)
            should_stop = _handle_balance_shortage(reason, min_viable, fatal=status_shortage)
            if should_stop:
                break
            continue
        if status_text_upper in final_states:
            active_order = None
            active_price = None
            continue
        if status_text_upper in cancel_states:
            active_order = None
            active_price = None
            continue

    avg_price = notional_sum / filled_total if filled_total > 0 else None
    remaining_value = max((goal_notional or 0.0) - notional_sum, 0.0) if use_notional else max(goal_size - filled_total, 0.0)
    if shared_active_prices is not None:
        shared_active_prices.pop(token_id, None)
    if price_sum_guard is not None:
        price_sum_guard.clear_price(token_id)
    _emit_state(final_status)
    return {
        "status": final_status,
        "avg_price": avg_price,
        "filled": notional_sum if use_notional else filled_total,
        "remaining": remaining_value,
        "filled_size": filled_total,
        "target_notional": goal_notional,
        "orders": orders,
    }


def _parse_market_entry(entry: Any) -> Tuple[Optional[str], Optional[str]]:
    market_id: Optional[str] = None
    label: Optional[str] = None

    if isinstance(entry, Mapping):
        raw_id = entry.get("id") or entry.get("token_id") or entry.get("market_id")
        market_id = str(raw_id).strip() if raw_id is not None else None
        name_field = entry.get("name") or entry.get("title") or entry.get("label")
        label = str(name_field).strip() if name_field is not None else None
    elif entry is not None:
        market_id = str(entry).strip()
        label = market_id

    return market_id, label


def maker_multi_buy_follow_bid(
    client: Any,
    submarkets: Iterable[Any],
    target_size: Optional[float] = None,
    *,
    target_notional: Optional[float] = None,
    state_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
    best_bid_fns: Optional[Mapping[str, Callable[[], Optional[float]]]] = None,
    preload_best_bids: bool = True,
    price_warmup_timeout: float = 30.0,
    price_warmup_poll: float = 0.5,
    qty_controller: Optional[UnifiedQtyController] = None,
    **kwargs: Any,
) -> Dict[str, Dict[str, Any]]:
    """在多个子市场按相同逻辑挂买单。

    Args:
        client: 交易客户端。
        submarkets: 子市场配置，支持字符串或包含 ``id``/``token_id``/``market_id`` 的字典。
        target_size: 每个子市场的目标买入数量（与 target_notional 二选一）。
        target_notional: 每个子市场的目标买入名义金额（USDC），会在每次重挂时
            根据最新买一价倒推份数。
        state_callback: 可选的状态回调，用于跟踪整体进度。
        best_bid_fns: 按 token_id 提供的买一价回调映射（优先于 **kwargs 中的
            ``best_bid_fn``），便于结合 WS 价格源按子问题传入。
        **kwargs: 透传给 :func:`maker_buy_follow_bid` 的其余参数。

    Returns:
        以子市场 ID 为键的结果字典，包含 ``name`` 与 ``result`` 等信息。
    """

    summary: Dict[str, Dict[str, Any]] = {}
    lock = threading.Lock()
    shared_active_prices: Dict[str, float] = {}
    price_guard = PriceSumArbitrageGuard()

    def _wrap_stop_check(user_stop: Optional[Callable[[], bool]]) -> Callable[[], bool]:
        def _combined() -> bool:
            if price_guard.should_stop():
                return True
            return bool(user_stop and user_stop())

        return _combined

    def _wrap_price_guard(user_guard: Optional[Callable[[Mapping[str, float]], bool]]):
        if user_guard is None:
            return price_guard.validate_proposed_prices

        def _guard(proposed: Mapping[str, float]) -> bool:
            return price_guard.validate_proposed_prices(proposed) and bool(user_guard(proposed))

        return _guard

    def _emit_state_locked() -> None:
        if state_callback is None:
            return
        snapshot: Dict[str, Dict[str, Any]] = {}
        for mid, payload in summary.items():
            result = payload.get("result") if isinstance(payload, dict) else None
            if not isinstance(result, dict):
                continue
            snapshot[mid] = {
                "status": result.get("status"),
                "filled": result.get("filled"),
                "remaining": result.get("remaining"),
            }
        try:
            state_callback({"markets": snapshot})
        except Exception as exc:
            print(f"[MAKER][BUY][CALLBACK] 状态回调异常：{exc}")

    def _update_summary(mid: str, payload: Dict[str, Any]) -> None:
        with lock:
            existing = summary.get(mid) if isinstance(summary.get(mid), Mapping) else {}
            name = payload.get("name") or existing.get("name") or mid
            base_result = existing.get("result") if isinstance(existing, Mapping) else {}
            if not isinstance(base_result, Mapping):
                base_result = {}
            new_result = dict(base_result)
            update_res = payload.get("result") if isinstance(payload, Mapping) else {}
            if isinstance(update_res, Mapping):
                for key in ("status", "filled", "remaining"):
                    if key in update_res:
                        new_result[key] = update_res.get(key)
                for k, v in update_res.items():
                    if k not in new_result:
                        new_result[k] = v
            summary[mid] = {"name": name, "result": new_result}
            _emit_state_locked()

    def _run_market(entry: Any, market_id: Optional[str], label: Optional[str]) -> None:
        if not market_id:
            print("[MAKER][BUY][WARN] 跳过缺少 market/token id 的子市场条目：", entry)
            return

        def _per_market_state(update: Dict[str, Any]) -> None:
            if not isinstance(update, Mapping):
                return
            _update_summary(market_id, {"name": label or market_id, "result": dict(update)})

        try:
            per_market_kwargs = dict(kwargs)
            user_stop = per_market_kwargs.get("stop_check")
            per_market_kwargs["stop_check"] = _wrap_stop_check(user_stop)
            user_guard = per_market_kwargs.get("price_update_guard")
            per_market_kwargs["price_update_guard"] = _wrap_price_guard(user_guard)
            per_market_kwargs["shared_active_prices"] = shared_active_prices
            per_market_kwargs["price_sum_guard"] = price_guard
            if qty_controller is not None:
                per_market_kwargs["qty_controller"] = qty_controller
            if best_bid_fns is not None:
                best_fn = best_bid_fns.get(market_id) if isinstance(best_bid_fns, Mapping) else None
                if best_fn is not None:
                    per_market_kwargs["best_bid_fn"] = best_fn
            result = maker_buy_follow_bid(
                client,
                token_id=market_id,
                target_size=target_size,
                target_notional=target_notional,
                state_callback=_per_market_state,
                **per_market_kwargs,
            )
            _update_summary(market_id, {"name": label or market_id, "result": result})
        except Exception as exc:
            _update_summary(
                market_id,
                {
                    "name": label or market_id,
                    "result": {"status": "ERROR", "error": str(exc)},
                },
            )

    entries = [(entry, *_parse_market_entry(entry)) for entry in submarkets]

    warmup_failed = False
    failed_ids: List[str] = []
    if preload_best_bids:
        pending: Dict[str, Optional[Callable[[], Optional[float]]]] = {}
        for entry, mid, _ in entries:
            if not mid:
                continue
            best_fn = None
            if best_bid_fns is not None:
                best_fn = best_bid_fns.get(mid) if isinstance(best_bid_fns, Mapping) else None
            if best_fn is None:
                best_fn = kwargs.get("best_bid_fn")
            pending[mid] = best_fn

        if pending:
            start = time.time()
            poll_interval = max(price_warmup_poll, 1e-6)
            timeout = float(price_warmup_timeout)
            while pending:
                for mid, fn in list(pending.items()):
                    info = _best_price_info(client, mid, fn, "bid")
                    if info is not None and info.price > 0:
                        shared_active_prices[mid] = float(info.price)
                        pending.pop(mid, None)
                if not pending:
                    break
                if timeout > 0 and time.time() - start >= timeout:
                    failed_ids = sorted(pending.keys())
                    warmup_failed = True
                    print(
                        "[MAKER][BUY] 价格预热超时，跳过下单：", ", ".join(failed_ids)
                    )
                    break
                time.sleep(poll_interval)

    if warmup_failed:
        for _, mid, label in entries:
            if mid in failed_ids:
                _update_summary(mid, {"name": label or mid, "result": {"status": "NO_PRICE"}})
        states: List[str] = []
        for mid, payload in summary.items():
            if mid is None:
                continue
            res = payload.get("result") if isinstance(payload, Mapping) else None
            if isinstance(res, Mapping) and "status" in res:
                states.append(str(res.get("status")))
        states.append("PRICE_WARMUP_TIMEOUT")
        summary["_meta"] = {"states": states, "balance_ok": True}
        return summary

    threads: List[threading.Thread] = []
    for entry, mid, label in entries:
        t = threading.Thread(target=_run_market, args=(entry, mid, label), daemon=True)
        threads.append(t)
        t.start()

    for t in threads:
        t.join()

    if price_guard.should_stop() and price_guard.violation_total() is not None:
        filled_entries: List[Tuple[str, float, float]] = []
        for mid, payload in summary.items():
            if mid is None or mid == "_meta":
                continue
            res = payload.get("result") if isinstance(payload, Mapping) else None
            if not isinstance(res, Mapping):
                continue
            filled_size = float(res.get("filled_size") or 0.0)
            avg_price = _coerce_float(res.get("avg_price"))
            if avg_price is None:
                fill_snapshot = price_guard.fills.get(mid)
                if fill_snapshot is not None:
                    avg_price = _coerce_float(fill_snapshot.get("avg_price"))
            if filled_size > _MIN_FILL_EPS and avg_price is not None:
                filled_entries.append((mid, filled_size, avg_price))

        if not filled_entries:
            print("此时所有子市场price之和大于1，已不满足套利条件，程序停止")
            raise SystemExit("arbitrage condition violated")

        for mid, size, avg in filled_entries:
            floor_px = float(avg) * 1.001
            maker_sell_follow_ask_with_floor_wait(
                client,
                token_id=mid,
                position_size=size,
                floor_X=floor_px,
                min_order_size=kwargs.get("min_order_size", DEFAULT_MIN_ORDER_SIZE),
            )
        print("此时所有子市场price之和大于1，已不满足套利条件，现有仓位已全部卖出，程序停止")
        raise SystemExit("arbitrage condition violated with fills unwound")

    states: List[str] = []
    for mid, payload in summary.items():
        if mid is None:
            continue
        res = payload.get("result") if isinstance(payload, Mapping) else None
        if isinstance(res, Mapping) and "status" in res:
            states.append(str(res.get("status")))
    summary["_meta"] = {"states": states, "balance_ok": True}

    return summary


def maker_sell_follow_ask_with_floor_wait(
    client: Any,
    token_id: str,
    position_size: float,
    floor_X: float,
    *,
    poll_sec: float = 10.0,
    min_order_size: float = DEFAULT_MIN_ORDER_SIZE,
    best_ask_fn: Optional[Callable[[], Optional[float]]] = None,
    stop_check: Optional[Callable[[], bool]] = None,
    sleep_fn: Callable[[float], None] = time.sleep,
    sell_mode: str = "conservative",
    aggressive_step: float = 0.01,
    aggressive_timeout: float = 300.0,
    progress_probe: Optional[Callable[[], None]] = None,
    progress_probe_interval: float = 60.0,
    position_fetcher: Optional[Callable[[], Optional[float]]] = None,
    position_refresh_interval: float = 30.0,
    ask_validation_interval: float = 60.0,
) -> Dict[str, Any]:
    """Maintain a maker sell order while respecting a profit floor."""

    goal_size = max(_floor_to_dp(float(position_size), SELL_SIZE_DP), 0.0)
    api_min_qty = 0.0
    if min_order_size and min_order_size > 0:
        api_min_qty = _ceil_to_dp(float(min_order_size), SELL_SIZE_DP)
    if goal_size < 0.01:
        return {
            "status": "SKIPPED",
            "avg_price": None,
            "filled": 0.0,
            "remaining": 0.0,
            "orders": [],
        }

    adapter = ClobPolymarketAPI(client)
    orders: List[Dict[str, Any]] = []
    records: Dict[str, Dict[str, Any]] = {}
    accounted: Dict[str, float] = {}

    remaining = goal_size
    filled_total = 0.0
    notional_sum = 0.0

    active_order: Optional[str] = None
    active_price: Optional[float] = None

    final_status = "PENDING"
    tick = _order_tick(SELL_PRICE_DP)

    waiting_for_floor = False
    aggressive_mode = str(sell_mode).lower() == "aggressive"
    aggressive_timer_start: Optional[float] = None
    aggressive_timer_anchor_fill: Optional[float] = None
    aggressive_floor_locked = False
    aggressive_next_price_override: Optional[float] = None
    aggressive_locked_price: Optional[float] = None
    next_price_override: Optional[float] = None
    # 统一两级缩减步长，先用 0.01，必要时再升级到 0.1
    shrink_tick = 0.01
    shortage_retry_count = 0
    try:
        aggressive_timeout = float(aggressive_timeout)
    except (TypeError, ValueError):
        aggressive_timeout = 300.0
    try:
        aggressive_step = float(aggressive_step)
    except (TypeError, ValueError):
        aggressive_step = 0.01
    if aggressive_step <= 0:
        aggressive_mode = False
    floor_float = float(floor_X)

    try:
        position_refresh_interval = float(position_refresh_interval)
    except (TypeError, ValueError):
        position_refresh_interval = 30.0
    if position_refresh_interval < 0:
        position_fetcher = None

    try:
        ask_validation_interval = float(ask_validation_interval)
    except (TypeError, ValueError):
        ask_validation_interval = 60.0
    if ask_validation_interval <= 0:
        ask_validation_interval = None

    next_probe_at = 0.0
    next_position_refresh = 0.0
    next_ask_validation = 0.0

    while True:
        if stop_check and stop_check():
            if active_order:
                _cancel_order(client, active_order)
                rec = records.get(active_order)
                if rec is not None:
                    rec["status"] = "CANCELLED"
                aggressive_timer_start = None
                aggressive_timer_anchor_fill = None
            final_status = "STOPPED"
            break

        now = time.time()
        if (
            position_fetcher
            and now >= max(next_position_refresh, 0.0)
        ):
            interval = max(position_refresh_interval, poll_sec, 1e-6)
            next_position_refresh = now + interval
            try:
                live_position = position_fetcher()
            except Exception as exc:
                print(f"[MAKER][SELL] 仓位刷新失败：{exc}")
                live_position = None
            if live_position is not None:
                try:
                    live_target = max(_floor_to_dp(float(live_position), SELL_SIZE_DP), 0.0)
                except (TypeError, ValueError):
                    live_target = None
                if live_target is not None:
                    min_goal = max(filled_total, 0.0)
                    new_goal = max(live_target, min_goal)
                    if abs(new_goal - goal_size) > _MIN_FILL_EPS:
                        change = "扩充" if new_goal > goal_size else "收缩"
                        prev_goal = goal_size
                        goal_size = new_goal
                        remaining = max(goal_size - filled_total, 0.0)
                        print(
                            "[MAKER][SELL] 仓位更新 -> "
                            f"{change}目标至 {goal_size:.{SELL_SIZE_DP}f}"
                        )
                        if remaining <= _MIN_FILL_EPS:
                            if active_order:
                                _cancel_order(client, active_order)
                                rec = records.get(active_order)
                                if rec is not None:
                                    rec["status"] = "CANCELLED"
                                active_order = None
                                active_price = None
                            final_status = "FILLED"
                            break
                        if new_goal < prev_goal - _MIN_FILL_EPS and active_order:
                            print("[MAKER][SELL] 仓位降低，撤销当前挂单以调整数量")
                            _cancel_order(client, active_order)
                            rec = records.get(active_order)
                            if rec is not None:
                                rec["status"] = "CANCELLED"
                            active_order = None
                            active_price = None
                            aggressive_timer_start = None
                            aggressive_timer_anchor_fill = None
                            aggressive_next_price_override = None
                            next_price_override = None
                            continue

        if api_min_qty and remaining + _MIN_FILL_EPS < api_min_qty:
            final_status = "FILLED_TRUNCATED" if filled_total > _MIN_FILL_EPS else "SKIPPED_TOO_SMALL"
            break

        ask = _best_ask(client, token_id, best_ask_fn)
        if ask_validation_interval and now >= max(next_ask_validation, 0.0):
            interval = max(ask_validation_interval, poll_sec, 1e-6)
            next_ask_validation = now + interval
            validated = _fetch_best_price(client, token_id, "ask")
            if validated is not None and validated.price > 0:
                validated_price = float(validated.price)
                tolerance = max(tick * 0.5, 1e-6)
                if ask is None or abs(validated_price - ask) > tolerance:
                    prev = ask
                    ask = validated_price
                    direction = "下行" if prev is not None and validated_price < prev else "上行"
                    if prev is None:
                        print(
                            f"[MAKER][SELL] 卖一校验覆盖：无本地价，采用最新卖一 {ask:.{SELL_PRICE_DP}f}"
                        )
                    else:
                        print(
                            "[MAKER][SELL] 卖一校验覆盖（" + direction + ") -> "
                            f"old={prev:.{SELL_PRICE_DP}f} new={ask:.{SELL_PRICE_DP}f}"
                        )
        if not aggressive_mode:
            if ask is None or ask <= 0:
                waiting_for_floor = True
                if active_order:
                    _cancel_order(client, active_order)
                    rec = records.get(active_order)
                    if rec is not None:
                        rec["status"] = "CANCELLED"
                    active_order = None
                    active_price = None
                    aggressive_timer_start = None
                    aggressive_timer_anchor_fill = None
                    aggressive_next_price_override = None
                    next_price_override = None
                sleep_fn(poll_sec)
                continue
            if ask < floor_X - 1e-12:
                if not waiting_for_floor:
                    print(
                        f"[MAKER][SELL] 卖一跌破地板，撤单等待 | ask={ask:.{SELL_PRICE_DP}f} floor={floor_X:.{SELL_PRICE_DP}f}"
                    )
                waiting_for_floor = True
                if active_order:
                    _cancel_order(client, active_order)
                    rec = records.get(active_order)
                    if rec is not None:
                        rec["status"] = "CANCELLED"
                    active_order = None
                    active_price = None
                    aggressive_timer_start = None
                    aggressive_timer_anchor_fill = None
                    aggressive_next_price_override = None
                    next_price_override = None
                sleep_fn(poll_sec)
                continue
            if waiting_for_floor and ask >= floor_X:
                waiting_for_floor = False
        else:
            if ask is None or ask <= 0:
                sleep_fn(poll_sec)
                continue
            if ask <= floor_float + 1e-12:
                aggressive_floor_locked = True
                aggressive_locked_price = floor_float
            elif aggressive_floor_locked and ask > floor_float + 1e-12:
                aggressive_floor_locked = False
                aggressive_locked_price = None

        if active_order is None:
            px_candidate = max(_round_down_to_dp(ask, SELL_PRICE_DP), floor_float)
            if next_price_override is not None:
                px_candidate = max(
                    _round_down_to_dp(next_price_override, SELL_PRICE_DP),
                    floor_float,
                )
                next_price_override = None
            if aggressive_mode:
                if aggressive_next_price_override is not None:
                    px_candidate = max(
                        _round_down_to_dp(aggressive_next_price_override, SELL_PRICE_DP),
                        floor_float,
                    )
                    aggressive_next_price_override = None
                elif aggressive_locked_price is not None:
                    px_candidate = max(
                        _round_down_to_dp(aggressive_locked_price, SELL_PRICE_DP),
                        floor_float,
                    )
                if px_candidate <= floor_float + 1e-12:
                    aggressive_floor_locked = True
                    aggressive_locked_price = floor_float
                else:
                    aggressive_locked_price = None
                    aggressive_floor_locked = False
            else:
                aggressive_next_price_override = None
            px = px_candidate
            qty = _floor_to_dp(remaining, SELL_SIZE_DP)
            if qty < 0.01:
                final_status = "FILLED"
                break
            if api_min_qty and qty + _MIN_FILL_EPS < api_min_qty:
                final_status = "FILLED_TRUNCATED" if filled_total > _MIN_FILL_EPS else "SKIPPED_TOO_SMALL"
                break
            payload = {
                "tokenId": token_id,
                "side": "SELL",
                "price": px,
                "size": qty,
                "timeInForce": "GTC",
                "type": "GTC",
                "allowPartial": True,
            }
            try:
                response = adapter.create_order(payload)
            except Exception as exc:
                msg = str(exc).lower()
                insufficient = any(
                    keyword in msg for keyword in ("insufficient", "balance", "position")
                )
                if insufficient:
                    forced_remaining: Optional[float] = None
                    if position_fetcher:
                        try:
                            live_position = position_fetcher()
                        except Exception as fetch_exc:
                            print(f"[MAKER][SELL] 强制刷新仓位失败：{fetch_exc}")
                            live_position = None
                        if live_position is not None:
                            try:
                                live_target = max(
                                    _floor_to_dp(float(live_position), SELL_SIZE_DP), 0.0
                                )
                            except (TypeError, ValueError):
                                live_target = None
                            if live_target is not None:
                                refreshed_goal = max(filled_total + live_target, filled_total)
                                forced_remaining = max(refreshed_goal - filled_total, 0.0)
                                if forced_remaining >= 0.01 and (
                                    not api_min_qty or forced_remaining + _MIN_FILL_EPS >= api_min_qty
                                ):
                                    print(
                                        "[MAKER][SELL] 可用仓位不足，刷新后改用实际仓位 -> "
                                        f"remain={forced_remaining:.{SELL_SIZE_DP}f}"
                                    )
                                    goal_size = refreshed_goal
                                    remaining = forced_remaining
                                    shortage_retry_count = 0
                                    continue
                    if forced_remaining is not None and (
                        forced_remaining < 0.01
                        or (api_min_qty and forced_remaining + _MIN_FILL_EPS < api_min_qty)
                    ):
                        final_status = (
                            "FILLED_TRUNCATED" if filled_total > _MIN_FILL_EPS else "SKIPPED_TOO_SMALL"
                        )
                        remaining = max(goal_size - filled_total, 0.0)
                        print(
                            "[MAKER][SELL] 仓位刷新后已无可交易数量，退出卖出流程。"
                        )
                        break
                    shortage_retry_count += 1
                    if shortage_retry_count > 100 and shrink_tick < 0.1 - 1e-12:
                        shrink_tick = 0.1
                    current_remaining = max(goal_size - filled_total, 0.0)
                    shrink_qty = _floor_to_dp(
                        max(current_remaining - shrink_tick, 0.0), SELL_SIZE_DP
                    )
                    if shrink_qty >= 0.01 and (
                        not api_min_qty or shrink_qty + _MIN_FILL_EPS >= api_min_qty
                    ):
                        print(
                            "[MAKER][SELL] 可用仓位不足，调整卖出数量后重试 -> "
                            f"old={qty:.{SELL_SIZE_DP}f} new={shrink_qty:.{SELL_SIZE_DP}f}"
                        )
                        goal_size = filled_total + shrink_qty
                        remaining = max(goal_size - filled_total, 0.0)
                        continue
                    final_status = (
                        "FILLED_TRUNCATED" if filled_total > _MIN_FILL_EPS else "SKIPPED_TOO_SMALL"
                    )
                    remaining = max(goal_size - filled_total, 0.0)
                    print(
                        "[MAKER][SELL] 可用仓位低于最小挂单量，放弃后续卖出尝试。"
                    )
                    break
                raise
            order_id = str(response.get("orderId"))
            record = {
                "id": order_id,
                "side": "sell",
                "price": px,
                "size": qty,
                "status": "OPEN",
                "filled": 0.0,
            }
            orders.append(record)
            records[order_id] = record
            accounted[order_id] = 0.0
            active_order = order_id
            active_price = px
            if aggressive_mode:
                if px <= floor_float + 1e-12:
                    aggressive_locked_price = floor_float
                    aggressive_floor_locked = True
                    aggressive_timer_start = None
                    aggressive_timer_anchor_fill = 0.0
                else:
                    aggressive_locked_price = None
                    aggressive_floor_locked = False
                    aggressive_timer_start = time.time()
                    aggressive_timer_anchor_fill = 0.0
            print(
                f"[MAKER][SELL] 挂单 -> price={px:.{SELL_PRICE_DP}f} qty={qty:.{SELL_SIZE_DP}f} remaining={remaining:.{SELL_SIZE_DP}f}"
            )
            if progress_probe:
                interval = max(progress_probe_interval, poll_sec, 1e-6)
                try:
                    progress_probe()
                except Exception as probe_exc:
                    print(f"[MAKER][SELL] 进度探针执行异常：{probe_exc}")
                next_probe_at = time.time() + interval
            continue

        sleep_fn(poll_sec)
        if (
            progress_probe
            and active_order
            and progress_probe_interval > 0
            and time.time() >= max(next_probe_at, 0.0)
        ):
            try:
                progress_probe()
            except Exception as probe_exc:
                print(f"[MAKER][SELL] 进度探针执行异常：{probe_exc}")
            interval = max(progress_probe_interval, poll_sec, 1e-6)
            next_probe_at = time.time() + interval
        try:
            status_payload = adapter.get_order_status(active_order)
        except Exception as exc:
            print(f"[MAKER][SELL] 查询订单状态异常：{exc}")
            status_payload = {"status": "UNKNOWN", "filledAmount": accounted.get(active_order, 0.0)}

        record = records.get(active_order)
        status_text = str(status_payload.get("status", "UNKNOWN"))
        record_size = None
        if record is not None:
            try:
                record_size = float(record.get("size", 0.0) or 0.0)
            except Exception:
                record_size = None
        last_price_hint = active_price
        if last_price_hint is None:
            last_price_hint = _coerce_float(status_payload.get("avgPrice"))
        if last_price_hint is None:
            last_price_hint = floor_X
        filled_amount, avg_price, notional_sum = _update_fill_totals(
            active_order,
            status_payload,
            accounted,
            notional_sum,
            float(last_price_hint),
            status_text=status_text,
            expected_full_size=record_size,
        )
        filled_total = sum(accounted.values())
        remaining = max(goal_size - filled_total, 0.0)
        status_text_upper = status_text.upper()
        if record is not None:
            record["filled"] = filled_amount
            record["status"] = status_text_upper
            if avg_price is not None:
                record["avg_price"] = avg_price
            price_display = record.get("price", active_price)
            total_size = float(record.get("size", 0.0) or 0.0)
            remaining_slice = max(total_size - filled_amount, 0.0)
            if price_display is not None:
                print(
                    f"[MAKER][SELL] 挂单状态 -> price={float(price_display):.{SELL_PRICE_DP}f} "
                    f"sold={filled_amount:.{SELL_SIZE_DP}f} remaining={remaining_slice:.{SELL_SIZE_DP}f} "
                    f"status={status_text_upper}"
                )

        if api_min_qty and remaining < api_min_qty:
            if active_order:
                _cancel_order(client, active_order)
                rec = records.get(active_order)
                if rec is not None:
                    rec["status"] = "CANCELLED"
                active_order = None
                active_price = None
                aggressive_timer_start = None
                aggressive_timer_anchor_fill = None
                aggressive_next_price_override = None
                next_price_override = None
            final_status = "FILLED_TRUNCATED" if filled_total > _MIN_FILL_EPS else "SKIPPED_TOO_SMALL"
            break

        if remaining <= 0.0 or _floor_to_dp(remaining, SELL_SIZE_DP) < 0.01:
            if active_order:
                _cancel_order(client, active_order)
                rec = records.get(active_order)
                if rec is not None:
                    rec["status"] = "CANCELLED"
                active_order = None
                aggressive_timer_start = None
                aggressive_timer_anchor_fill = None
                aggressive_next_price_override = None
                next_price_override = None
            final_status = "FILLED"
            break

        ask = _best_ask(client, token_id, best_ask_fn)
        if not aggressive_mode:
            if ask is None:
                continue
            if ask < floor_X - 1e-12:
                print(
                    f"[MAKER][SELL] 卖一再次跌破地板，撤单等待 | ask={ask:.{SELL_PRICE_DP}f} floor={floor_X:.{SELL_PRICE_DP}f}"
                )
                _cancel_order(client, active_order)
                rec = records.get(active_order)
                if rec is not None:
                    rec["status"] = "CANCELLED"
                active_order = None
                active_price = None
                waiting_for_floor = True
                aggressive_timer_start = None
                aggressive_timer_anchor_fill = None
                aggressive_next_price_override = None
                next_price_override = None
                continue
        else:
            if ask is None:
                continue
            if ask <= floor_float + 1e-12:
                aggressive_floor_locked = True
                aggressive_locked_price = floor_float
            elif aggressive_floor_locked and ask > floor_float + 1e-12:
                aggressive_floor_locked = False
                aggressive_locked_price = None

        if aggressive_mode and active_order:
            if aggressive_timer_anchor_fill is None:
                aggressive_timer_anchor_fill = accounted.get(active_order, 0.0)
            if aggressive_timer_start is None and not aggressive_floor_locked:
                aggressive_timer_start = time.time()
                aggressive_timer_anchor_fill = accounted.get(active_order, 0.0)
            current_filled = accounted.get(active_order, 0.0)
            if current_filled > (aggressive_timer_anchor_fill or 0.0) + _MIN_FILL_EPS:
                aggressive_timer_start = time.time()
                aggressive_timer_anchor_fill = current_filled
            if not aggressive_floor_locked and aggressive_timer_start is not None:
                elapsed = time.time() - aggressive_timer_start
                if elapsed >= aggressive_timeout and active_price is not None:
                    target_price = active_price - aggressive_step
                    if target_price <= floor_float + 1e-12:
                        aggressive_floor_locked = True
                        aggressive_locked_price = floor_float
                        aggressive_timer_start = None
                        aggressive_timer_anchor_fill = current_filled
                        if active_price > floor_float + 1e-12:
                            print(
                                "[MAKER][SELL][激进] 触及地板价，保持地板挂单"
                            )
                            _cancel_order(client, active_order)
                            rec = records.get(active_order)
                            if rec is not None:
                                rec["status"] = "CANCELLED"
                            active_order = None
                            active_price = None
                            aggressive_next_price_override = floor_float
                            next_price_override = floor_float
                        continue
                    next_px = max(
                        _round_down_to_dp(target_price, SELL_PRICE_DP),
                        floor_float,
                    )
                    if next_px < active_price - 1e-12:
                        print(
                            "[MAKER][SELL][激进] 挂单超时未成交，下调挂价 -> "
                            f"old={active_price:.{SELL_PRICE_DP}f} new={next_px:.{SELL_PRICE_DP}f}"
                        )
                        _cancel_order(client, active_order)
                        rec = records.get(active_order)
                        if rec is not None:
                            rec["status"] = "CANCELLED"
                        active_order = None
                        active_price = None
                        aggressive_next_price_override = next_px
                        aggressive_timer_start = None
                        aggressive_timer_anchor_fill = current_filled
                        continue

        if active_price is not None and ask <= active_price - tick - 1e-12:
            new_px = max(_round_down_to_dp(ask, SELL_PRICE_DP), float(floor_X))
            if aggressive_mode:
                if active_price <= floor_float + 1e-12:
                    continue
                if new_px <= floor_float + 1e-12:
                    aggressive_floor_locked = True
                    aggressive_locked_price = floor_float
                    if active_price <= floor_float + 1e-12:
                        continue
                    print(
                        "[MAKER][SELL][激进] 卖一跌至地板价，保持地板挂单"
                    )
                    _cancel_order(client, active_order)
                    rec = records.get(active_order)
                    if rec is not None:
                        rec["status"] = "CANCELLED"
                    active_order = None
                    active_price = None
                    aggressive_timer_start = None
                    aggressive_timer_anchor_fill = None
                    aggressive_next_price_override = floor_float
                    next_price_override = floor_float
                    continue
            print(
                f"[MAKER][SELL] 卖一下行 -> 撤单重挂 | old={active_price:.{SELL_PRICE_DP}f} new={new_px:.{SELL_PRICE_DP}f}"
            )
            if aggressive_mode and new_px > floor_float + 1e-12:
                aggressive_floor_locked = False
                aggressive_locked_price = None
            _cancel_order(client, active_order)
            rec = records.get(active_order)
            if rec is not None:
                rec["status"] = "CANCELLED"
            active_order = None
            active_price = None
            aggressive_timer_start = None
            aggressive_timer_anchor_fill = None
            aggressive_next_price_override = new_px if aggressive_mode else None
            next_price_override = new_px
            continue

        final_states = {"FILLED", "MATCHED", "COMPLETED", "EXECUTED"}
        cancel_states = {"CANCELLED", "CANCELED", "REJECTED", "EXPIRED"}
        if status_text_upper in final_states:
            active_order = None
            active_price = None
            aggressive_timer_start = None
            aggressive_timer_anchor_fill = None
            aggressive_next_price_override = None
            next_price_override = None
            continue
        if status_text_upper in cancel_states:
            active_order = None
            active_price = None
            aggressive_timer_start = None
            aggressive_timer_anchor_fill = None
            aggressive_next_price_override = None
            next_price_override = None
            continue

    avg_price = notional_sum / filled_total if filled_total > 0 else None
    remaining = max(goal_size - filled_total, 0.0)
    return {
        "status": final_status,
        "avg_price": avg_price,
        "filled": filled_total,
        "remaining": remaining,
        "orders": orders,
    }
