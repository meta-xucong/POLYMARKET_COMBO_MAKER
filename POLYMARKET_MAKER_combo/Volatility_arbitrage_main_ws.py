# Volatility_arbitrage_main_ws.py
# -*- coding: utf-8 -*-
"""
最小 WS 连接器（只负责连接与订阅，不做格式化/节流/查询展示）。
外部可传入 on_event 回调来处理每条事件。支持 verbose 开关（默认关闭，不输出）。

用法：
  from Volatility_arbitrage_main_ws import ws_watch_by_ids
  ws_watch_by_ids([YES_id, NO_id], label="...", on_event=handler, verbose=False)

依赖：pip install websocket-client
"""
from __future__ import annotations

import json, time, threading, ssl
from typing import Callable, List, Optional, Any, Dict

try:
    import websocket  # websocket-client
except Exception:
    raise RuntimeError("缺少依赖，请先安装： pip install websocket-client")

try:
    # 仅用于在 WS 收到首条买一价时打点标记；不依赖于 maker_execution 的其余逻辑。
    from maker_execution import _extract_best_price  # type: ignore
    from maker_execution import _best_bid_info  # type: ignore
except Exception:
    _extract_best_price = None  # type: ignore
    _best_bid_info = None  # type: ignore

WS_BASE = "wss://ws-subscriptions-clob.polymarket.com"
CHANNEL = "market"

def _now() -> str:
    from datetime import datetime
    return datetime.now().strftime("%H:%M:%S")

def ws_watch_by_ids(asset_ids: List[str],
                    label: str = "",
                    on_event: Optional[Callable[[Dict[str, Any]], None]] = None,
                    verbose: bool = False,
                    stop_event: Optional[threading.Event] = None,
                    bid_ready_flags: Optional[Dict[str, threading.Event]] = None):
    """
    只负责：连接 → 订阅 → 将 WS 事件回调给 on_event（逐条 dict）。
    - asset_ids: 订阅的 token_ids（字符串）
    - label: 可选，仅用于启动打印（不参与逻辑）
    - on_event: 回调函数，参数是一条事件（dict）。若服务端下发 list，将按条回调。
    - verbose: 默认 False。为 True 时打印 OPEN/SUB/ERROR/CLOSED 及无回调时的事件。
    """
    ids = [str(x) for x in asset_ids if x]
    if not ids:
        raise ValueError("asset_ids 为空")

    if verbose and label:
        print(f"[INIT] 订阅: {label}")
    if verbose:
        for i, tid in enumerate(ids):
            print(f"  - token_id[{i}] = {tid}")

    stop_event = stop_event or threading.Event()

    reconnect_delay = 1
    max_reconnect_delay = 60

    headers = [
        "Origin: https://polymarket.com",
        "User-Agent: Mozilla/5.0",
    ]

    def _extract_token_id(payload: Dict[str, Any]) -> Optional[str]:
        for key in (
            "token_id",
            "tokenId",
            "id",
            "market_id",
            "marketId",
            "asset_id",
            "assetId",
        ):
            val = payload.get(key)
            if val:
                return str(val)
        return None

    def _extract_bid_price(payload: Dict[str, Any]) -> Optional[float]:
        if _extract_best_price is not None:
            try:
                sample = _extract_best_price(payload, "bid")
                if sample is not None and getattr(sample, "price", None):
                    price_val = float(sample.price)
                    # 忽略明显占位的低价（如 0.01）以避免误判为首条有效买一价。
                    if price_val > 0.01:
                        return price_val
            except Exception:
                pass

        for key in ("best_bid", "bestBid", "bid", "buy"):
            if key in payload:
                    try:
                        val = float(payload.get(key))
                        if val > 0.01:
                            return val
                    except Exception:
                        continue

        bids = payload.get("bids")
        if isinstance(bids, list):
            for entry in bids:
                if isinstance(entry, dict) and "price" in entry:
                    try:
                        val = float(entry.get("price"))
                        if val > 0.01:
                            return val
                    except Exception:
                        continue
        return None

    def _flag_first_bid(payload: Dict[str, Any]):
        if not bid_ready_flags:
            return
        if not isinstance(payload, dict):
            return

        if payload.get("event_type") == "price_change" or "price_changes" in payload:
            changes = payload.get("price_changes", [])
            if isinstance(changes, list):
                for pc in changes:
                    if not isinstance(pc, dict):
                        continue
                    token_id = _extract_token_id(pc)
                    if not token_id:
                        continue
                    price_val = _extract_bid_price(pc)
                    if price_val is None:
                        continue
                    evt = bid_ready_flags.get(token_id)
                    if evt:
                        evt.set()
            return

        token_id = _extract_token_id(payload)
        if not token_id:
            return
        price_val = _extract_bid_price(payload)
        if price_val is None:
            return
        evt = bid_ready_flags.get(token_id)
        if evt:
            evt.set()


class WsBestBidTracker:
    """轻量级 WS 订阅器，用于追踪子问题的实时买一价，并在无行情时通过 REST 补价。"""

    def __init__(
        self,
        token_ids: List[str],
        client: Any,
        *,
        on_bid_change: Optional[Callable[[str, float, Optional[float]], None]] = None,
        position_refresher: Optional[Callable[[], None]] = None,
        rest_refresh_interval: float = 60.0,
    ):
        self.token_ids = [str(tid).strip() for tid in token_ids if tid]
        self._client = client
        self._best: Dict[str, float] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._first_bid_events: Dict[str, threading.Event] = {
            tid: threading.Event() for tid in self.token_ids
        }
        self._thread: Optional[threading.Thread] = None
        self._rest_guard_thread: Optional[threading.Thread] = None
        self._error: Optional[Exception] = None
        self._on_bid_change = on_bid_change
        self._position_refresher = position_refresher
        self._rest_refresh_interval = max(rest_refresh_interval, 5.0)
        # 如果 WS 始终无消息，默认值为 0 便于守护线程迅速触发 REST 补价，
        # 避免长时间等待后才尝试回落到 REST。
        self._last_ws_message_at = 0.0
        self._last_rest_refresh_at = 0.0

    def set_on_bid_change(self, handler: Optional[Callable[[str, float, Optional[float]], None]]) -> None:
        self._on_bid_change = handler

    def set_position_refresher(self, handler: Optional[Callable[[], None]]) -> None:
        self._position_refresher = handler

    def _has_all_prices_unlocked(self) -> bool:
        if self._first_bid_events:
            return all(evt.is_set() for evt in self._first_bid_events.values())
        return all((tid in self._best) and (self._best.get(tid, 0.0) > 0) for tid in self.token_ids)

    def _extract_token_id(self, payload: Dict[str, Any]) -> Optional[str]:
        if not isinstance(payload, dict):
            return None
        for key in (
            "token_id",
            "tokenId",
            "id",
            "market_id",
            "marketId",
            "asset_id",
            "assetId",
        ):
            val = payload.get(key)
            if val:
                return str(val)
        return None

    def _on_event(self, event: Dict[str, Any]) -> None:
        if not isinstance(event, dict):
            return

        if event.get("event_type") == "price_change" or "price_changes" in event:
            changes = event.get("price_changes", [])
            if isinstance(changes, list):
                for pc in changes:
                    if not isinstance(pc, dict):
                        continue
                    token_id = self._extract_token_id(pc)
                    if not token_id:
                        continue
                    sample = _extract_best_price(pc, "bid") if _extract_best_price else None
                    if sample is None:
                        continue
                    try:
                        price_val = float(sample.price)
                    except Exception:
                        continue
                    if price_val <= 0.01:
                        continue
                    with self._lock:
                        prev = self._best.get(token_id)
                    self._last_ws_message_at = time.time()
                    with self._lock:
                        self._best[token_id] = price_val
                        evt = self._first_bid_events.get(token_id)
                        if evt:
                            evt.set()
                        if self._has_all_prices_unlocked():
                            self._ready.set()
                    if self._on_bid_change and (prev is None or abs(prev - float(sample.price)) > 1e-12):
                        try:
                            self._on_bid_change(token_id, float(sample.price), prev)
                        except Exception:
                            pass
                return

        token_id = self._extract_token_id(event)
        if not token_id:
            return
        sample = _extract_best_price(event, "bid") if _extract_best_price else None
        if sample is None:
            return
        prev = self._best.get(token_id)
        self._last_ws_message_at = time.time()
        with self._lock:
            price_val = float(sample.price)
            if price_val <= 0.01:
                return
            self._best[token_id] = price_val
            evt = self._first_bid_events.get(token_id)
            if evt:
                evt.set()
            if self._has_all_prices_unlocked():
                self._ready.set()
        if self._on_bid_change and (prev is None or abs(prev - float(sample.price)) > 1e-12):
            try:
                self._on_bid_change(token_id, float(sample.price), prev)
            except Exception:
                pass

    def _refresh_best_via_rest(self, *, announce: bool = False) -> bool:
        if self._client is None or _best_bid_info is None:
            return False

        updated = False
        now = time.time()
        for tid in self.token_ids:
            try:
                info = _best_bid_info(self._client, tid, None)
            except Exception as exc:
                if announce:
                    print(f"[WARN] REST 补价失败（{tid}）：{exc}")
                continue
            if info is None or info.price is None or info.price <= 0.01:
                if announce:
                    print(f"[WARN] REST 未返回有效买一价（{tid}）")
                continue

            prev = None
            with self._lock:
                prev = self._best.get(tid)
                self._best[tid] = float(info.price)
                evt = self._first_bid_events.get(tid)
                if evt:
                    evt.set()
                if self._has_all_prices_unlocked():
                    self._ready.set()
            updated = True
            if self._on_bid_change and (prev is None or abs(prev - float(info.price)) > 1e-12):
                try:
                    self._on_bid_change(tid, float(info.price), prev)
                except Exception:
                    pass

        if updated:
            self._last_rest_refresh_at = now
            self._last_ws_message_at = now
            if announce:
                print("[INFO] 已使用 REST 报价刷新最新买一价，等待 WS 行情继续更新…")
            if self._position_refresher:
                try:
                    self._position_refresher()
                except Exception:
                    pass
        elif announce:
            print("[WARN] REST 补价未能获取任何子问题的有效买一价，将继续尝试…")
        return updated

    def start(self) -> bool:
        if not self.token_ids:
            return False

        self._refresh_best_via_rest(announce=True)

        def _runner():
            try:
                ws_watch_by_ids(
                    self.token_ids,
                    label="maker-best-bid",
                    on_event=self._on_event,
                    verbose=False,
                    stop_event=self._stop,
                    bid_ready_flags=self._first_bid_events,
                )
            except Exception as exc:
                self._error = exc

        def _rest_guard():
            while not self._stop.is_set():
                time.sleep(5.0)
                idle = time.time() - self._last_ws_message_at
                if idle < self._rest_refresh_interval:
                    continue
                recently_refreshed = time.time() - self._last_rest_refresh_at < self._rest_refresh_interval
                if recently_refreshed:
                    continue
                refreshed = self._refresh_best_via_rest(announce=True)
                if refreshed:
                    self._last_ws_message_at = time.time()

        self._thread = threading.Thread(target=_runner, daemon=True)
        self._rest_guard_thread = threading.Thread(target=_rest_guard, daemon=True)
        self._thread.start()
        self._rest_guard_thread.start()
        return True

    def wait_until_ready(self, timeout: Optional[float]) -> bool:
        missing = self.wait_for_first_bids(timeout=timeout, poll=0.2)
        return not missing

    def wait_for_first_bids(self, *, timeout: Optional[float] = 15.0, poll: float = 0.2) -> set[str]:
        poll = max(poll, 1e-3)
        deadline = None if timeout is None else time.time() + max(timeout, 0.0)
        remaining_tokens: set[str] = set(self.token_ids)

        while remaining_tokens:
            if deadline is not None:
                now = time.time()
                if now >= deadline:
                    break
                wait_time = min(poll, max(deadline - now, 0.0))
            else:
                wait_time = poll

            for tid in list(remaining_tokens):
                evt = self._first_bid_events.get(tid)
                if evt is None:
                    continue
                if evt.wait(timeout=wait_time):
                    remaining_tokens.discard(tid)
            if self._has_all_prices_unlocked():
                remaining_tokens.clear()
                break
        return remaining_tokens if deadline is not None else set()

    def stop(self):
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.0)
        if self._rest_guard_thread and self._rest_guard_thread.is_alive():
            self._rest_guard_thread.join(timeout=1.0)

    @property
    def error(self) -> Optional[Exception]:
        return self._error

    def best_bid(self, token_id: str) -> Optional[float]:
        tid = str(token_id).strip()
        with self._lock:
            return self._best.get(tid)

    def all_ready(self) -> bool:
        return self._has_all_prices_unlocked()

    while not stop_event.is_set():
        ping_stop = {"v": False}

        def on_open(ws):
            nonlocal reconnect_delay
            if verbose:
                print(f"[{_now()}][WS][OPEN] -> {WS_BASE+'/ws/'+CHANNEL}")
            payload = {"type": CHANNEL, "assets_ids": ids}
            ws.send(json.dumps(payload))
            reconnect_delay = 1

            # 文本心跳 PING（与底层 ping 帧并行存在）
            def _ping():
                while not ping_stop["v"] and not stop_event.is_set():
                    try:
                        ws.send("PING")
                        time.sleep(10)
                    except Exception:
                        break
            threading.Thread(target=_ping, daemon=True).start()

        def on_message(ws, message):
            # 忽略非 JSON 文本（如 PONG）
            try:
                data = json.loads(message)
            except Exception:
                return

            # 先根据行情事件尝试标记首条买一价。
            if isinstance(data, list):
                for item in data:
                    if isinstance(item, dict):
                        _flag_first_bid(item)
            elif isinstance(data, dict):
                _flag_first_bid(data)

            # 无回调：仅在 verbose=True 时打印，否则静默
            if on_event is None:
                if verbose:
                    print(f"[{_now()}][WS][EVENT] {data}")
                return

            # 逐条回调
            if isinstance(data, list):
                for item in data:
                    if isinstance(item, dict):
                        try:
                            on_event(item)
                        except Exception:
                            pass
            elif isinstance(data, dict):
                try:
                    on_event(data)
                except Exception:
                    pass

        def on_error(ws, error):
            if verbose:
                print(f"[{_now()}][WS][ERROR] {error}")

        def on_close(ws, status_code, msg):
            ping_stop["v"] = True
            if verbose:
                print(f"[{_now()}][WS][CLOSED] {status_code} {msg}")

        wsa = websocket.WebSocketApp(
            WS_BASE + "/ws/" + CHANNEL,
            on_open=on_open,
            on_message=on_message,
            on_error=on_error,
            on_close=on_close,
            header=headers,
        )

        try:
            wsa.run_forever(
                sslopt={"cert_reqs": ssl.CERT_REQUIRED},
                ping_interval=25,
                ping_timeout=10,
            )
        except Exception as exc:
            ping_stop["v"] = True
            if verbose:
                print(f"[{_now()}][WS][EXCEPTION] {exc}")
        finally:
            ping_stop["v"] = True

        if stop_event.is_set():
            break

        if verbose:
            print(f"[{_now()}][WS] 连接结束，{reconnect_delay}s 后重试…")
        time.sleep(reconnect_delay)
        reconnect_delay = min(reconnect_delay * 2, max_reconnect_delay)

# --- 仅供独立运行调试 ---
def _parse_cli(argv: List[str]) -> Optional[str]:
    for i, a in enumerate(argv):
        if a == "--source" and i + 1 < len(argv):
            return argv[i + 1].strip()
        if a.startswith("--source="):
            return a.split("=", 1)[1].strip()
    return None

def _resolve_ids_via_rest(source: str):
    import urllib.parse, requests, json
    GAMMA_API = "https://gamma-api.polymarket.com/markets"

    def _is_url(s: str) -> bool:
        return s.startswith("http://") or s.startswith("https://")

    def _extract_market_slug(url: str):
        p = urllib.parse.urlparse(url)
        parts = [x for x in p.path.split("/") if x]
        if len(parts) >= 2 and parts[0] == "event":
            return parts[-1]
        if len(parts) >= 2 and parts[0] == "market":
            return parts[1]
        return None

    if _is_url(source):
        slug = _extract_market_slug(source)
        if not slug:
            raise ValueError("无法从 URL 解析出 market slug")
        r = requests.get(GAMMA_API, params={"limit": 1, "slug": slug}, timeout=10)
        r.raise_for_status()
        arr = r.json()
        if not (isinstance(arr, list) and arr):
            raise ValueError("gamma-api 未找到该市场")
        m = arr[0]
        title = m.get("question") or slug
        token_ids_raw = m.get("clobTokenIds", "[]")
        token_ids = json.loads(token_ids_raw) if isinstance(token_ids_raw, str) else (token_ids_raw or [])
        return [x for x in token_ids if x], title

    if "," in source:
        a, b = [x.strip() for x in source.split(",", 1)]
        title = "manual-token-ids"
        return [x for x in (a, b) if x], title

    raise ValueError("未识别的输入。")

if __name__ == "__main__":
    import sys
    src = _parse_cli(sys.argv[1:])
    if not src:
        print('请输入 Polymarket 市场 URL：')
        src = input().strip()
        if not src:
            raise SystemExit(1)
    ids, label = _resolve_ids_via_rest(src)

    # 独立运行调试：开启 verbose 以便观察
    def _dbg(ev): print(ev)
    ws_watch_by_ids(ids, label=label, on_event=_dbg, verbose=True)
