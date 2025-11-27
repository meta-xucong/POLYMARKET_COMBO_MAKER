# Volatility_arbitrage_run.py
# -*- coding: utf-8 -*-
"""
组合买单入口：
- 支持事件页下多选子问题，或单市场直接解析。
- 按所选 token 列表一次性并行买入，屏蔽所有卖出与循环策略逻辑。
"""
from __future__ import annotations
import sys
import os
import time
import re
import hmac
import hashlib
import json
from typing import Dict, Any, Tuple, List, Optional
from decimal import Decimal, ROUND_UP, ROUND_DOWN
import requests
from datetime import datetime, timezone, timedelta, date, time as dtime
from json import JSONDecodeError
try:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
except Exception:  # pragma: no cover - 兼容无 zoneinfo 的环境
    ZoneInfo = None  # type: ignore
    class ZoneInfoNotFoundError(Exception):
        pass
from maker_execution import (
    maker_multi_buy_follow_bid,
)

# ========== 1) Client：优先 ws 版，回退 rest 版 ==========
def _get_client():
    try:
        from Volatility_arbitrage_main_ws import get_client  # 优先
        return get_client()
    except Exception as e1:
        try:
            from Volatility_arbitrage_main_rest import get_client  # 退回
            return get_client()
        except Exception as e2:
            print("[ERR] 无法导入 get_client：", e1, "|", e2)
            sys.exit(1)

# ========== 2) 保留 price_watch 的单市场解析函数（先尝试） ==========
try:
    from Volatility_arbitrage_price_watch import resolve_token_ids
except Exception as e:
    print("[ERR] 无法从 Volatility_arbitrage_price_watch 导入 resolve_token_ids：", e)
    sys.exit(1)

CLOB_API_HOST = "https://clob.polymarket.com"
GAMMA_ROOT = os.getenv("POLY_GAMMA_ROOT", "https://gamma-api.polymarket.com")
DATA_API_ROOT = os.getenv("POLY_DATA_API_ROOT", "https://data-api.polymarket.com")
API_MIN_ORDER_SIZE = 5.0
ORDERBOOK_STALE_AFTER_SEC = 5.0
POSITION_SYNC_INTERVAL = 60.0
POST_BUY_POSITION_CHECK_DELAY = 5.0


# ===== 旧版解析器（复刻 + 极小修正） =====
def _parse_yes_no_ids_literal(source: str) -> Tuple[Optional[str], Optional[str]]:
    parts = [x.strip() for x in source.split(",")]
    if len(parts) == 2 and all(parts):
        return parts[0], parts[1]
    return None, None

def _extract_event_slug(s: str) -> str:
    m = re.search(r"/event/([^/?#]+)", s)
    if m: return m.group(1)
    s = s.strip()
    if s and ("/" not in s) and ("?" not in s) and ("&" not in s):
        return s
    return ""


def _extract_market_slug(s: str) -> str:
    m = re.search(r"/market/([^/?#]+)", s)
    if m:
        return m.group(1)
    s = s.strip()
    if s and ("/" not in s) and ("?" not in s) and ("&" not in s):
        return s
    return ""


_TEXT_TIMEZONE_REGEXES = [
    (re.compile(r"\b(?:u\.s\.|us)?\s*eastern(?:\s+(?:standard|daylight))?\s+time\b", re.I), "America/New_York"),
    (re.compile(r"\b(?:et|est|edt)\b", re.I), "America/New_York"),
    (re.compile(r"\b(?:u\.s\.|us)?\s*central(?:\s+(?:standard|daylight))?\s+time\b", re.I), "America/Chicago"),
    (re.compile(r"\b(?:ct|cst|cdt)\b", re.I), "America/Chicago"),
    (re.compile(r"\b(?:u\.s\.|us)?\s*pacific(?:\s+(?:standard|daylight))?\s+time\b", re.I), "America/Los_Angeles"),
    (re.compile(r"\b(?:pt|pst|pdt)\b", re.I), "America/Los_Angeles"),
    (re.compile(r"\b(?:mountain|mt|mst|mdt)\s+time\b", re.I), "America/Denver"),
]

_JSON_LIKE_PREFIX_RE = re.compile(r"^\s*[\[{]")

_TIME_COMPONENT_RE = re.compile(r"(\d{1,2}):(\d{2})(?::(\d{2}))?")


def _describe_timezone_hint(hint: Any) -> str:
    if hint is None:
        return ""
    if isinstance(hint, dict) and "offset_minutes" in hint:
        try:
            minutes = float(hint["offset_minutes"])
        except (TypeError, ValueError):
            return str(hint)
        sign = "+" if minutes >= 0 else "-"
        mins = abs(int(minutes))
        hours, remain = divmod(mins, 60)
        return f"UTC{sign}{hours:02d}:{remain:02d}"
    return str(hint)


def _timezone_from_hint(hint: Any) -> Optional[timezone]:
    if hint is None:
        return None
    if isinstance(hint, dict) and "offset_minutes" in hint:
        try:
            minutes = float(hint["offset_minutes"])
        except (TypeError, ValueError):
            return None
        return timezone(timedelta(minutes=minutes))
    if isinstance(hint, (int, float)):
        return timezone(timedelta(minutes=float(hint)))

    text = str(hint).strip()
    if not text:
        return None

    lowered = text.lower()
    keyword_map = {
        "et": "America/New_York",
        "est": "America/New_York",
        "edt": "America/New_York",
        "eastern": "America/New_York",
        "ct": "America/Chicago",
        "cst": "America/Chicago",
        "cdt": "America/Chicago",
        "pt": "America/Los_Angeles",
        "pst": "America/Los_Angeles",
        "pdt": "America/Los_Angeles",
    }
    canonical = keyword_map.get(lowered)
    if ZoneInfo and canonical:
        try:
            return ZoneInfo(canonical)
        except ZoneInfoNotFoundError:
            pass
    if canonical and not ZoneInfo:
        # zoneinfo 不可用时退化为标准时区（不考虑夏令时）
        offsets = {
            "America/New_York": -300,
            "America/Chicago": -360,
            "America/Los_Angeles": -480,
        }
        minutes = offsets.get(canonical)
        if minutes is not None:
            return timezone(timedelta(minutes=minutes))

    # UTC±HH:MM 或 ±HH:MM
    m = re.match(r"^(?:utc)?\s*([+-])\s*(\d{1,2})(?::?(\d{2}))?$", lowered)
    if m:
        sign = 1 if m.group(1) == "+" else -1
        hours = int(m.group(2))
        mins = int(m.group(3) or 0)
        total = sign * (hours * 60 + mins)
        return timezone(timedelta(minutes=total))

    # 纯数字：视为分钟
    if lowered.replace(".", "", 1).lstrip("+-").isdigit():
        try:
            value = float(lowered)
        except ValueError:
            return None
        # 数值 <= 24 视为小时，否则视为分钟
        minutes = value * 60 if abs(value) <= 24 else value
        return timezone(timedelta(minutes=minutes))

    if ZoneInfo:
        try:
            return ZoneInfo(text)
        except ZoneInfoNotFoundError:
            return None
    return None


def _timezone_hint_from_text_block(block: Any) -> Optional[str]:
    """Parse free-text fields (e.g. rules) to infer well-known timezones."""

    if block is None:
        return None
    if isinstance(block, str):
        lowered = block.lower()
        for regex, zone in _TEXT_TIMEZONE_REGEXES:
            if regex.search(lowered):
                return zone
        return None
    if isinstance(block, dict):
        for value in block.values():
            hint = _timezone_hint_from_text_block(value)
            if hint:
                return hint
        return None
    if isinstance(block, (list, tuple)):
        for item in block:
            hint = _timezone_hint_from_text_block(item)
            if hint:
                return hint
    return None


def _parse_json_like_string(source: Any) -> Optional[Any]:
    if not isinstance(source, str):
        return None
    if not _JSON_LIKE_PREFIX_RE.match(source):
        return None
    try:
        return json.loads(source)
    except (ValueError, JSONDecodeError, TypeError):
        return None


def _infer_timezone_hint(obj: Any) -> Optional[Any]:
    direct_keys = (
        "eventTimezone",
        "event_timezone",
        "timezone",
        "timeZone",
        "marketTimezone",
        "timezoneName",
        "timezone_name",
        "timezoneShort",
        "timezone_short",
    )
    offset_keys = (
        "timezoneOffsetMinutes",
        "timezone_offset_minutes",
        "timezoneOffset",
        "timezone_offset",
        "eventTimezoneOffsetMinutes",
    )
    text_keys = (
        "rules",
        "resolutionRules",
        "resolutionCriteria",
        "resolutionDescription",
        "resolutionSources",
        "resolutionSource",
        "description",
        "details",
        "notes",
        "info",
        "longDescription",
        "shortDescription",
        "question",
        "title",
        "subtitle",
        "body",
        "text",
    )
    nested_keys = (
        "event",
        "parentMarket",
        "eventInfo",
        "collection",
        "extraInfo",
        "metadata",
        "meta",
    )

    seen: set[int] = set()

    def _scan(value: Any) -> Optional[Any]:
        if isinstance(value, dict):
            oid = id(value)
            if oid in seen:
                return None
            seen.add(oid)
            for key in direct_keys:
                val = value.get(key)
                if isinstance(val, str) and val.strip():
                    return val.strip()
            for key in offset_keys:
                val = value.get(key)
                if isinstance(val, (int, float)):
                    return {"offset_minutes": float(val)}
            for key in text_keys:
                if key in value:
                    hint = _timezone_hint_from_text_block(value.get(key))
                    if hint:
                        return hint
            for key in nested_keys:
                if key in value:
                    hint = _scan(value.get(key))
                    if hint:
                        return hint
            for val in value.values():
                hint = _scan(val)
                if hint:
                    return hint
            return None
        if isinstance(value, (list, tuple)):
            for item in value:
                hint = _scan(item)
                if hint:
                    return hint
            return None
        if isinstance(value, str):
            parsed = _parse_json_like_string(value)
            if parsed is not None:
                hint = _scan(parsed)
                if hint:
                    return hint
            return _timezone_hint_from_text_block(value)
        return None

    return _scan(obj) if isinstance(obj, (dict, list, tuple)) else None


def _parse_timestamp(val: Any, timezone_hint: Optional[Any] = None) -> Optional[float]:
    if val is None:
        return None
    if isinstance(val, (int, float)):
        ts = float(val)
        if ts > 1e12:
            ts = ts / 1000.0
        return ts
    if isinstance(val, str):
        raw = val.strip()
        if not raw:
            return None
        try:
            ts = float(raw)
            if ts > 1e12:
                ts = ts / 1000.0
            return ts
        except ValueError:
            pass
        iso = raw.replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(iso)
            tzinfo = dt.tzinfo
            if tzinfo is None:
                tzinfo = _timezone_from_hint(timezone_hint) or timezone.utc
                dt = dt.replace(tzinfo=tzinfo)
            return dt.astimezone(timezone.utc).timestamp()
        except ValueError:
            pass
        for fmt in (
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d",
            "%Y/%m/%d %H:%M:%S",
            "%Y/%m/%d",
        ):
            try:
                dt = datetime.strptime(raw, fmt)
                tzinfo = _timezone_from_hint(timezone_hint) or timezone.utc
                dt = dt.replace(tzinfo=tzinfo)
                return dt.astimezone(timezone.utc).timestamp()
            except ValueError:
                continue
    return None


def _value_has_meaningful_time_component(value: Any) -> bool:
    if isinstance(value, (int, float)):
        return True
    if not isinstance(value, str):
        return False
    match = _TIME_COMPONENT_RE.search(value)
    if not match:
        return False
    try:
        hour = int(match.group(1) or 0)
        minute = int(match.group(2) or 0)
        second = int(match.group(3) or 0)
    except (TypeError, ValueError):
        return False
    return not (hour == 0 and minute == 0 and second == 0)


def _get_zoneinfo_or_fallback(name: str, fallback_offset_minutes: int) -> timezone:
    if ZoneInfo:
        try:
            return ZoneInfo(name)  # type: ignore[arg-type]
        except ZoneInfoNotFoundError:
            pass
    return timezone(timedelta(minutes=fallback_offset_minutes))


def _apply_manual_deadline_override_meta(
    meta: Optional[Dict[str, Any]], override_ts: Optional[float]
) -> Dict[str, Any]:
    base_meta: Dict[str, Any] = dict(meta or {})
    if not override_ts:
        return base_meta
    if base_meta.get("resolved_ts"):
        return base_meta
    base_meta["end_ts"] = override_ts
    base_meta["end_ts_precise"] = True
    return base_meta


def _should_offer_common_deadline_options(meta: Optional[Dict[str, Any]]) -> bool:
    if not isinstance(meta, dict):
        return False
    if meta.get("resolved_ts"):
        return False
    end_ts = meta.get("end_ts")
    if not isinstance(end_ts, (int, float)):
        return False
    return not bool(meta.get("end_ts_precise"))


def _prompt_common_deadline_override(
    base_date_utc: date,
    *,
    allow_skip: bool = False,
    intro_text: Optional[str] = None,
) -> Tuple[Optional[float], bool]:
    if intro_text:
        print(intro_text)
    else:
        print(
            "[WARN] 自动获取的截止时间缺少明确的时区/时刻信息，",
            "将基于事件标注日期",
            f" {base_date_utc.isoformat()} 进行人工选择。",
        )
        print(
            "请选择常用结束时间点（默认 1）：\\n",
            "  [1] 12:00 PM ET（金融 / 市场预测类）\\n",
            "  [2] 23:59 ET（天气 / 逐日统计类）\\n",
            "  [3] UTC 00:00（跨时区国际事件）\\n",
            "  [4] 不设定结束时间点",
        )
    options = {
        "1": {"label": "12:00 PM ET", "hour": 12, "minute": 0, "tz": "America/New_York", "fallback": -240},
        "2": {"label": "23:59 ET", "hour": 23, "minute": 59, "tz": "America/New_York", "fallback": -240},
        "3": {"label": "00:00 UTC", "hour": 0, "minute": 0, "tz": "UTC", "fallback": 0},
        "4": {"label": "不设定结束时间点", "no_deadline": True},
    }
    if allow_skip:
        print(
            "如需使用以上常用时间点覆盖市场截止时间，请输入编号；"
            "直接回车则沿用自动识别的截止日期。"
        )

    choice = ""
    while choice not in options:
        raw = input().strip()
        if allow_skip and not raw:
            print("[INFO] 已沿用自动识别的截止时间。")
            return None, False
        choice = raw or "1"
        if choice not in options:
            print("[ERR] 请输入 1、2、3 或 4：")
    spec = options[choice]
    if spec.get("no_deadline"):
        print("[INFO] 已选择不设定结束时间点，将跳过截止时间校验与倒计时。")
        return None, True
    tzinfo = (
        timezone.utc
        if spec["tz"] == "UTC"
        else _get_zoneinfo_or_fallback(spec["tz"], spec["fallback"])
    )
    local_dt = datetime.combine(
        base_date_utc,
        dtime(hour=spec["hour"], minute=spec["minute"]),
    ).replace(tzinfo=tzinfo)
    utc_dt = local_dt.astimezone(timezone.utc)
    print(
        "[INFO] 已手动指定该市场监控截止为 "
        f"{local_dt.isoformat()} ({spec['label']})，即 UTC {utc_dt.isoformat()}。",
    )
    return utc_dt.timestamp(), False

def _market_meta_from_obj(m: dict, timezone_override: Optional[Any] = None) -> Dict[str, Any]:
    meta: Dict[str, Any] = {}
    if not isinstance(m, dict):
        return meta
    meta["slug"] = m.get("slug") or m.get("marketSlug") or m.get("market_slug")
    meta["market_id"] = (
        m.get("marketId")
        or m.get("id")
        or m.get("market_id")
        or m.get("conditionId")
        or m.get("condition_id")
    )

    tz_hint = timezone_override if timezone_override is not None else _infer_timezone_hint(m)
    if tz_hint:
        meta["timezone_hint"] = tz_hint

    end_keys = (
        "endDate",
        "endTime",
        "closeTime",
        "closeDate",
        "closedTime",
        "expiry",
        "expirationTime",
    )
    for key in end_keys:
        raw_value = m.get(key)
        ts = _parse_timestamp(raw_value, tz_hint)
        if ts:
            meta["end_ts"] = ts
            meta["end_ts_precise"] = _value_has_meaningful_time_component(raw_value)
            break

    resolve_keys = (
        "resolvedTime",
        "resolutionTime",
        "resolveTime",
        "resolvedAt",
        "finalizationTime",
        "finalizedTime",
        "settlementTime",
    )
    for key in resolve_keys:
        ts = _parse_timestamp(m.get(key), tz_hint)
        if ts:
            meta["resolved_ts"] = ts
            break

    if "end_ts" not in meta and "resolved_ts" in meta:
        meta["end_ts"] = meta["resolved_ts"]

    meta["raw"] = m
    return meta


def _apply_timezone_override_meta(
    meta: Optional[Dict[str, Any]], override_hint: Optional[Any]
) -> Dict[str, Any]:
    """Rebuild meta info with the provided timezone override when possible."""

    base_meta: Dict[str, Any] = dict(meta or {})
    if not override_hint:
        return base_meta

    raw_meta = base_meta.get("raw") if isinstance(base_meta, dict) else None
    if isinstance(raw_meta, dict):
        return _market_meta_from_obj(raw_meta, override_hint)

    base_meta["timezone_hint"] = override_hint
    return base_meta


def _maybe_fetch_market_meta_from_source(source: str) -> Dict[str, Any]:
    slug = _extract_market_slug(source)
    if not slug:
        return {}
    m = _fetch_market_by_slug(slug)
    if m:
        return _market_meta_from_obj(m)
    return {}


def _market_has_ended(meta: Dict[str, Any], now: Optional[float] = None) -> bool:
    if not meta:
        return False
    if now is None:
        now = time.time()
    candidates: List[float] = []
    for key in ("resolved_ts", "end_ts"):
        ts = meta.get(key)
        if isinstance(ts, (int, float)):
            candidates.append(float(ts))
    if not candidates:
        return False
    return now >= min(candidates)


def _extract_position_size(status: Dict[str, Any]) -> float:
    if not isinstance(status, dict):
        return 0.0
    for key in ("position_size", "position", "size"):
        val = status.get(key)
        if val is None:
            continue
        try:
            size = float(val)
            if size > 0:
                return size
        except (TypeError, ValueError):
            continue
    return 0.0


def _merge_remote_position_size(
    current_size: Optional[float],
    remote_size: Optional[float],
    *,
    eps: float = 1e-6,
    dust_floor: Optional[float] = None,
) -> Tuple[Optional[float], bool]:
    """Return normalized remote size and whether it differs from the current state."""

    def _normalize(value: Optional[float], *, apply_dust: bool) -> Optional[float]:
        if value is None:
            return None
        try:
            normalized = float(value)
        except (TypeError, ValueError):
            return None
        floor = eps
        if apply_dust and isinstance(dust_floor, (int, float)):
            floor = max(float(dust_floor), floor)
        if normalized <= floor:
            return None
        return normalized

    current_norm = _normalize(current_size, apply_dust=False)
    remote_norm = _normalize(remote_size, apply_dust=True)

    if current_norm is None and remote_norm is None:
        return None, False
    if current_norm is None and remote_norm is not None:
        return remote_norm, True
    if current_norm is not None and remote_norm is None:
        return None, True
    assert current_norm is not None and remote_norm is not None
    if abs(current_norm - remote_norm) > eps:
        return remote_norm, True
    return remote_norm, False


def _should_attempt_claim(
    meta: Dict[str, Any],
    status: Dict[str, Any],
    closed_by_ws: bool,
) -> bool:
    pos_size = _extract_position_size(status)
    if pos_size <= 0:
        return False
    if closed_by_ws:
        return True
    return _market_has_ended(meta)


def _resolve_client_host(client) -> str:
    env_host = os.getenv("POLY_HOST")
    if isinstance(env_host, str) and env_host.strip():
        return env_host.strip().rstrip("/")

    for attr in ("host", "_host", "base_url", "api_url"):
        val = getattr(client, attr, None)
        if isinstance(val, str) and val.strip():
            host = val.strip().rstrip("/")
            if "gamma-api" in host:
                return host.replace("gamma-api", "clob")
            return host

    return CLOB_API_HOST


def _extract_api_creds(client) -> Optional[Dict[str, str]]:
    def _pair_from_mapping(mp: Dict[str, Any]) -> Optional[Dict[str, str]]:
        if not isinstance(mp, dict):
            return None
        key_keys = ("key", "apiKey", "api_key", "id", "apiId", "api_id")
        secret_keys = ("secret", "apiSecret", "api_secret", "apiSecretKey")
        key_val = next((mp.get(k) for k in key_keys if mp.get(k)), None)
        secret_val = next((mp.get(k) for k in secret_keys if mp.get(k)), None)
        if key_val and secret_val:
            return {"key": str(key_val), "secret": str(secret_val)}
        return None

    def _pair_from_object(obj: Any) -> Optional[Dict[str, str]]:
        if obj is None:
            return None
        # 对部分库返回的命名元组/数据类做兼容
        for attr_key in ("key", "apiKey", "api_key", "id", "apiId", "api_id"):
            key_val = getattr(obj, attr_key, None)
            if key_val:
                break
        else:
            key_val = None
        for attr_secret in ("secret", "apiSecret", "api_secret", "apiSecretKey"):
            secret_val = getattr(obj, attr_secret, None)
            if secret_val:
                break
        else:
            secret_val = None
        if key_val and secret_val:
            return {"key": str(key_val), "secret": str(secret_val)}
        if hasattr(obj, "to_dict"):
            try:
                return _pair_from_mapping(obj.to_dict())
            except Exception:
                return None
        return None

    def _pair_from_sequence(seq: Any) -> Optional[Dict[str, str]]:
        if not isinstance(seq, (list, tuple)) or len(seq) < 2:
            return None
        key_val, secret_val = seq[0], seq[1]
        if key_val and secret_val:
            return {"key": str(key_val), "secret": str(secret_val)}
        return None

    candidates = [
        getattr(client, "api_creds", None),
        getattr(client, "_api_creds", None),
    ]
    getter = getattr(client, "get_api_creds", None)
    if callable(getter):
        try:
            candidates.append(getter())
        except Exception:
            pass
    key = getattr(client, "api_key", None)
    secret = getattr(client, "api_secret", None)
    if key and secret:
        candidates.append({"key": key, "secret": secret})
    # 兼容直接从环境变量注入 API key/secret 的场景
    env_key = os.getenv("POLY_API_KEY")
    env_secret = os.getenv("POLY_API_SECRET")
    if env_key and env_secret:
        candidates.append({"key": env_key, "secret": env_secret})

    for cand in candidates:
        if cand is None:
            continue
        pair = None
        if isinstance(cand, dict):
            pair = _pair_from_mapping(cand)
        elif isinstance(cand, (list, tuple)):
            pair = _pair_from_sequence(cand)
        else:
            pair = _pair_from_object(cand)
        if pair:
            return pair
    return None


def _sign_payload(secret: str, timestamp: str, method: str, path: str, body: str) -> str:
    payload = f"{timestamp}{method.upper()}{path}{body}"
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()


def _claim_via_http(client, market_id: str, token_id: Optional[str]) -> bool:
    creds = _extract_api_creds(client)
    if not creds:
        print("[CLAIM] 当前客户端缺少 API 凭证信息，无法调用 HTTP claim 接口。")
        return False

    host = _resolve_client_host(client)
    path = "/v1/user/clob/positions/claim"
    url = f"{host}{path}"
    payload: Dict[str, Any] = {"market": market_id}
    if token_id:
        payload["tokenIds"] = [token_id]

    body = json.dumps(payload, separators=(",", ":"))
    ts = str(int(time.time() * 1000))
    signature = _sign_payload(creds["secret"], ts, "POST", path, body)
    headers = {
        "Content-Type": "application/json",
        "X-API-Key": creds["key"],
        "X-API-Signature": signature,
        "X-API-Timestamp": ts,
    }

    try:
        resp = requests.post(url, data=body, headers=headers, timeout=10)
    except Exception as exc:
        print(f"[CLAIM] 请求 {url} 时出现异常：{exc}")
        return False

    if resp.status_code == 404:
        print("[CLAIM] 目标 claim 接口返回 404，请确认所使用的 Clob API 版本是否支持自动 claim。")
        return False
    if resp.status_code >= 500:
        print(f"[CLAIM] 服务端 {resp.status_code} 错误：{resp.text}")
        return False
    if resp.status_code in (401, 403):
        print(f"[CLAIM] 接口拒绝访问（{resp.status_code}）：{resp.text}")
        return False

    try:
        data = resp.json()
    except ValueError:
        data = resp.text

    print(f"[CLAIM] HTTP {path} 返回状态 {resp.status_code}，响应：{data}")
    if isinstance(data, dict) and data.get("error"):
        return False
    return resp.ok


def _extract_positions_from_data_api_response(payload: Any) -> Optional[List[dict]]:
    if payload is None:
        return []
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, list):
            return data
        return None
    return None


def _normalize_wallet_address(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, (bytes, bytearray)):
        try:
            hexed = value.hex()
        except Exception:
            return None
        return hexed if hexed else None
    if isinstance(value, str):
        candidate = value.strip()
        return candidate or None
    if isinstance(value, (list, tuple, set)):
        for item in value:
            candidate = _normalize_wallet_address(item)
            if candidate:
                return candidate
        return None
    if isinstance(value, dict):
        keys = (
            "address",
            "wallet",
            "walletAddress",
            "wallet_address",
            "funder",
            "owner",
            "defaultAddress",
            "default_address",
        )
        for key in keys:
            candidate = _normalize_wallet_address(value.get(key))
            if candidate:
                return candidate
    return None


def _resolve_wallet_address(client) -> Tuple[Optional[str], str]:
    if client is not None:
        direct_attrs = (
            "funder",
            "owner",
            "address",
            "wallet",
            "wallet_address",
            "walletAddress",
            "default_address",
            "defaultAddress",
            "deposit_address",
            "depositAddress",
        )
        for attr in direct_attrs:
            try:
                cand = getattr(client, attr, None)
            except Exception:
                continue
            address = _normalize_wallet_address(cand)
            if address:
                return address, f"client.{attr}"

        try:
            attrs = list(dir(client))
        except Exception:
            attrs = []
        for attr in attrs:
            if "address" not in attr.lower():
                continue
            if attr in direct_attrs:
                continue
            try:
                cand = getattr(client, attr, None)
            except Exception:
                continue
            address = _normalize_wallet_address(cand)
            if address:
                return address, f"client.{attr}"

    env_candidates = (
        "POLY_DATA_ADDRESS",
        "POLY_FUNDER",
        "POLY_WALLET",
        "POLY_ADDRESS",
    )
    for env_name in env_candidates:
        cand = os.getenv(env_name)
        address = _normalize_wallet_address(cand)
        if address:
            return address, f"env:{env_name}"

    return None, "缺少地址，无法从数据接口拉取持仓。"


def _fetch_positions_from_data_api(client) -> Tuple[List[dict], bool, str]:
    address, origin_hint = _resolve_wallet_address(client)

    if not address:
        return [], False, origin_hint

    url = f"{DATA_API_ROOT}/positions"

    limit = 500
    offset = 0
    collected: List[dict] = []
    total_records: Optional[int] = None

    while True:
        params = {
            "user": address,
            "limit": limit,
            "offset": offset,
            "sizeThreshold": 0,
        }
        try:
            resp = requests.get(url, params=params, timeout=10)
        except requests.RequestException as exc:
            return [], False, f"数据接口请求失败：{exc}"

        if resp.status_code == 404:
            return [], False, "数据接口返回 404（请确认使用 Proxy/Deposit 地址查询 user 参数）"

        try:
            resp.raise_for_status()
        except requests.RequestException as exc:
            return [], False, f"数据接口请求失败：{exc}"

        try:
            payload = resp.json()
        except ValueError:
            return [], False, "数据接口响应解析失败"

        positions = _extract_positions_from_data_api_response(payload)
        if positions is None:
            return [], False, "数据接口返回格式异常，缺少 data 字段。"

        collected.extend(positions)
        meta = payload.get("meta") if isinstance(payload, dict) else {}
        if isinstance(meta, dict):
            raw_total = meta.get("total") or meta.get("count")
            try:
                if raw_total is not None:
                    total_records = int(raw_total)
            except (TypeError, ValueError):
                total_records = None

        if not positions or (total_records is not None and len(collected) >= total_records):
            break

        offset += len(positions)

    total = total_records if total_records is not None else len(collected)
    origin_detail = f" via {origin_hint}" if origin_hint else ""
    origin = f"data-api positions(limit={limit}, total={total}, param=user){origin_detail}"
    return collected, True, origin


def _coerce_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        if isinstance(value, str):
            try:
                return float(value.strip())
            except (TypeError, ValueError):
                return None
        return None


def _position_dict_candidates(entry: Dict[str, Any]) -> List[Dict[str, Any]]:
    candidates: List[Dict[str, Any]] = []
    if isinstance(entry, dict):
        candidates.append(entry)
        for key in ("position", "token", "asset", "outcome"):
            nested = entry.get(key)
            if isinstance(nested, dict):
                candidates.append(nested)
    return candidates


def _position_matches_token(entry: Dict[str, Any], token_id: str) -> bool:
    token_str = str(token_id)
    if not token_str:
        return False
    id_keys = (
        "tokenId",
        "token_id",
        "clobTokenId",
        "clob_token_id",
        "assetId",
        "asset_id",
        "outcomeTokenId",
        "outcome_token_id",
        "token",
        "asset",
        "id",
    )
    for cand in _position_dict_candidates(entry):
        for key in id_keys:
            val = cand.get(key)
            if val is None:
                continue
            if str(val) == token_str:
                return True
    return False


def _extract_position_size_from_entry(entry: Dict[str, Any]) -> Optional[float]:
    size_keys = (
        "size",
        "positionSize",
        "position_size",
        "position",
        "quantity",
        "qty",
        "balance",
        "amount",
    )
    for cand in _position_dict_candidates(entry):
        for key in size_keys:
            val = _coerce_float(cand.get(key))
            if val is not None and val > 0:
                return val
    return None


def _plan_manual_buy_size(
    manual_size: Optional[float],
    owned_size: Optional[float],
    *,
    enforce_target: bool,
    eps: float = 1e-9,
) -> Tuple[Optional[float], bool]:
    """Compute the effective order size for manual buy mode."""

    if manual_size is None:
        return None, False

    try:
        requested = float(manual_size)
    except (TypeError, ValueError):
        return None, False

    if requested <= 0:
        return None, True

    current_owned = 0.0
    if owned_size is not None:
        try:
            current_owned = max(float(owned_size), 0.0)
        except (TypeError, ValueError):
            current_owned = 0.0

    if not enforce_target:
        return requested, False

    remaining = max(requested - current_owned, 0.0)
    if remaining <= eps:
        return None, True
    return remaining, False


def _extract_avg_price_from_entry(entry: Dict[str, Any]) -> Optional[float]:
    avg_keys = (
        "avg_price",
        "avgPrice",
        "average_price",
        "averagePrice",
        "avgExecutionPrice",
        "avg_execution_price",
        "averageExecutionPrice",
        "average_execution_price",
        "entry_price",
        "entryPrice",
        "entryAveragePrice",
        "entry_average_price",
        "execution_price",
        "executionPrice",
    )
    for cand in _position_dict_candidates(entry):
        for key in avg_keys:
            val = _coerce_float(cand.get(key))
            if val is not None and val > 0:
                return val

    notional_keys = (
        "total_cost",
        "totalCost",
        "net_cost",
        "netCost",
        "cost",
        "position_cost",
        "positionCost",
        "purchase_value",
        "purchaseValue",
        "buy_value",
        "buyValue",
    )
    size = _extract_position_size_from_entry(entry)
    if size is None or size <= 0:
        return None
    for cand in _position_dict_candidates(entry):
        for key in notional_keys:
            notional = _coerce_float(cand.get(key))
            if notional is None:
                continue
            if abs(size) < 1e-12:
                continue
            price = notional / size
            if price > 0:
                return price
    return None


def _lookup_position_avg_price(
    client,
    token_id: str,
) -> Tuple[Optional[float], Optional[float], str]:
    if not token_id:
        return None, None, "token_id 缺失"

    retry_times = 5
    retry_interval = 1.0
    last_info: Optional[str] = None

    for attempt in range(retry_times):
        positions, ok, origin = _fetch_positions_from_data_api(client)

        if positions:
            for pos in positions:
                if not isinstance(pos, dict):
                    continue
                if not _position_matches_token(pos, token_id):
                    continue
                avg_price = _extract_avg_price_from_entry(pos)
                pos_size = _extract_position_size_from_entry(pos)
                return avg_price, pos_size, origin

            last_info = f"未在 {origin or 'positions'} 中找到 token {token_id}"
        else:
            if ok:
                last_info = origin if origin else "数据接口返回空列表"
            else:
                last_info = origin if origin else "未知原因"

        if attempt < retry_times - 1:
            time.sleep(retry_interval)

    return None, None, last_info or f"未在 positions 中找到 token {token_id}"


def _attempt_claim(client, meta: Dict[str, Any], token_id: str) -> None:
    market_id = meta.get("market_id") if isinstance(meta, dict) else None
    print(f"[CLAIM] 检测到需处理的未平仓仓位，token_id={token_id}，开始尝试 claim…")
    if not market_id:
        print("[CLAIM] 未找到 market_id，无法自动 claim，请手动处理。")
        return

    claim_fn = getattr(client, "claim_positions", None)
    if callable(claim_fn):
        claim_kwargs = {"market": market_id}
        if token_id:
            claim_kwargs["token_ids"] = [token_id]
        try:
            print(f"[CLAIM] 尝试调用 claim_positions({claim_kwargs})…")
            resp = claim_fn(**claim_kwargs)
            print(f"[CLAIM] 响应: {resp}")
            return
        except TypeError as exc:
            print(f"[CLAIM] claim_positions 参数不匹配: {exc}，改用 HTTP 接口。")
        except Exception as exc:
            print(f"[CLAIM] 调用 claim_positions 失败: {exc}，改用 HTTP 接口。")

    if _claim_via_http(client, market_id, token_id):
        return

    print("[CLAIM] 未找到可用的 claim 方法，请手动处理。")

def _http_json(url: str, params=None) -> Optional[Any]:
    try:
        r = requests.get(url, params=params or {}, timeout=10)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()
    except Exception:
        return None

def _list_markets_under_event(event_slug: str) -> List[dict]:
    if not event_slug:
        return []
    # A) /events?slug=<slug>
    for closed_flag in ("false", "true", None):
        params = {"slug": event_slug}
        if closed_flag is not None:
            params["closed"] = closed_flag
        data = _http_json(f"{GAMMA_ROOT}/events", params=params)
        evs = []
        if isinstance(data, dict) and "data" in data:
            evs = data["data"]
        elif isinstance(data, list):
            evs = data
        if isinstance(evs, list):
            for ev in evs:
                mkts = ev.get("markets") or []
                if mkts:
                    return mkts
        # 若找到事件但 markets 为空，则无需继续尝试其它 closed_flag
        if evs:
            break
    # B) /markets?search=<slug> 精确过滤 eventSlug
    data = _http_json(f"{GAMMA_ROOT}/markets", params={"limit": 200, "search": event_slug})
    mkts = []
    if isinstance(data, dict) and "data" in data:
        mkts = data["data"]
    elif isinstance(data, list):
        mkts = data
    if isinstance(mkts, list):
        return [m for m in mkts if str(m.get("eventSlug") or "") == str(event_slug)]
    return []

def _fetch_market_by_slug(market_slug: str) -> Optional[dict]:
    return _http_json(f"{GAMMA_ROOT}/markets/slug/{market_slug}")

def _pick_market_subquestion(markets: List[dict]) -> dict:
    print("[CHOICE] 该事件下存在多个子问题，请选择其一，或直接粘贴具体子问题URL：")
    for i, m in enumerate(markets):
        title = m.get("title") or m.get("question") or m.get("slug")
        end_ts = m.get("endDate") or m.get("endTime") or ""
        mslug = m.get("slug") or ""
        url = f"https://polymarket.com/market/{mslug}" if mslug else "(no slug)"
        print(f"  [{i}] {title}  (end={end_ts})  -> {url}")
    while True:
        s = input("请输入序号或粘贴URL：").strip()
        if s.startswith(("http://", "https://")):
            return {"__direct_url__": s}
        if s.isdigit():
            idx = int(s)
            if 0 <= idx < len(markets):
                return markets[idx]
        print("请输入有效序号或URL。")


def _pick_market_subquestions(markets: List[dict]) -> List[dict]:
    print(
        "[CHOICE] 该事件下存在多个子问题，可多选。输入序号（可逗号分隔或填 all 全选），"
        "或直接粘贴具体子问题URL："
    )
    for i, m in enumerate(markets):
        title = m.get("title") or m.get("question") or m.get("slug")
        end_ts = m.get("endDate") or m.get("endTime") or ""
        mslug = m.get("slug") or ""
        url = f"https://polymarket.com/market/{mslug}" if mslug else "(no slug)"
        print(f"  [{i}] {title}  (end={end_ts})  -> {url}")

    while True:
        raw = input("请输入序号列表或URL：").strip()
        if raw.startswith(("http://", "https://")):
            return [{"__direct_url__": raw}]
        if raw.lower() == "all":
            return markets
        parts = [p.strip() for p in raw.split(",") if p.strip()]
        if not parts:
            print("请输入有效序号或URL。")
            continue
        indices: List[int] = []
        valid = True
        for part in parts:
            if not part.isdigit():
                valid = False
                break
            idx = int(part)
            if idx < 0 or idx >= len(markets):
                valid = False
                break
            indices.append(idx)
        if not valid:
            print("请输入有效序号或URL。")
            continue
        # 去重保持顺序
        seen = set()
        selected = []
        for idx in indices:
            if idx in seen:
                continue
            seen.add(idx)
            selected.append(markets[idx])
        return selected

def _tokens_from_market_obj(m: dict) -> Tuple[str, str, str]:
    title = m.get("title") or m.get("question") or m.get("slug") or ""
    yes_id = no_id = ""
    ids = m.get("clobTokenIds") or m.get("clobTokens")
    # 兼容字符串形式的 clobTokenIds
    if isinstance(ids, str):
        try:
            import json as _json
            ids = _json.loads(ids)
        except Exception:
            ids = None
    if isinstance(ids, (list, tuple)) and len(ids) >= 2:
        return str(ids[0]), str(ids[1]), title
    # 兼容 tokens/outcomes 结构
    outcomes = m.get("outcomes") or m.get("tokens") or []
    if isinstance(outcomes, list) and outcomes and isinstance(outcomes[0], dict):
        for o in outcomes:
            name = (o.get("name") or o.get("outcome") or o.get("position") or "").strip().lower()
            tid = o.get("tokenId") or o.get("clobTokenId") or o.get("token_id") or o.get("id") or ""
            if not tid:
                continue
            if name in ("yes", "y", "true", "yes token", "yes_token"):
                yes_id = str(tid)
            elif name in ("no", "n", "false", "no token", "no_token"):
                no_id = str(tid)
        if yes_id and no_id:
            return yes_id, no_id, title
    # 兼容直接字段
    y = m.get("yesTokenId") or m.get("yes_token_id")
    n = m.get("noTokenId") or m.get("no_token_id")
    if y and n:
        return str(y), str(n), title
    return yes_id, no_id, title


def _token_entries_from_market(m: dict) -> List[Dict[str, str]]:
    yes_id, no_id, title = _tokens_from_market_obj(m)
    entries: List[Dict[str, str]] = []
    if yes_id:
        entries.append({"id": yes_id, "name": f"{title} | YES"})
    if no_id:
        entries.append({"id": no_id, "name": f"{title} | NO"})
    return entries


def _prompt_token_selection(candidates: List[Dict[str, str]]) -> List[Dict[str, str]]:
    if not candidates:
        return []
    print("[CHOICE] 请选择需要买入的子问题/方向（可多选，输入 all 全选）：")
    for i, entry in enumerate(candidates):
        name = entry.get("name") or entry.get("title") or entry.get("id")
        token_id = entry.get("id") or ""
        print(f"  [{i}] {name} -> token_id={token_id}")
    while True:
        raw = input("请输入序号列表：").strip()
        if raw.lower() in {"", "all"}:
            return candidates
        parts = [p.strip() for p in raw.split(",") if p.strip()]
        if not parts:
            print("请输入有效序号。")
            continue
        indices: List[int] = []
        valid = True
        for part in parts:
            if not part.isdigit():
                valid = False
                break
            idx = int(part)
            if idx < 0 or idx >= len(candidates):
                valid = False
                break
            indices.append(idx)
        if not valid:
            print("请输入有效序号。")
            continue
        seen = set()
        selected: List[Dict[str, str]] = []
        for idx in indices:
            if idx in seen:
                continue
            seen.add(idx)
            selected.append(candidates[idx])
        return selected

def _looks_like_event_source(source: str) -> bool:
    """Return True when *source* clearly refers to an event rather than a market."""

    if not isinstance(source, str):
        return False
    lower = source.strip().lower()
    if not lower:
        return False
    if "/event/" in lower:
        return True
    # 兼容直接输入 event-xxx 这类 slug（旧脚本支持粘贴 slug）。
    if lower.startswith("event-"):
        return True
    return False


def _resolve_with_fallback(source: str) -> Tuple[str, str, str, Dict[str, Any]]:
    # 1) "YES_id,NO_id"
    y, n = _parse_yes_no_ids_literal(source)
    if y and n:
        return y, n, "(Manual IDs)", {}
    # 2) 先尝试旧解析器（单一市场 URL/slug）
    if not _looks_like_event_source(source):
        try:
            y1, n1, title1, raw1 = resolve_token_ids(source)
            if y1 and n1:
                meta = _market_meta_from_obj(raw1 or {}) if raw1 else {}
                if not meta:
                    meta = _maybe_fetch_market_meta_from_source(source)
                return y1, n1, title1, meta
        except Exception:
            pass
    # 2.5) 若上一步失败：把输入当作可能的 market slug（含 /event 路由别名）
    cand_slugs: List[str] = []
    if not _looks_like_event_source(source):
        ms = _extract_market_slug(source)
        if ms:
            cand_slugs.append(ms)
        es = _extract_event_slug(source)
        if es and es not in cand_slugs:
            cand_slugs.append(es)
    for slug in cand_slugs:
        # A) 直接按 /markets/slug/<slug>
        m = _fetch_market_by_slug(slug)
        if isinstance(m, dict):
            yx, nx, tx = _tokens_from_market_obj(m)
            if yx and nx:
                return yx, nx, tx or (m.get("title") or m.get("question") or slug), _market_meta_from_obj(m)
        # B) 用 /markets?search=<slug> 兜底（先 active，再放宽）
        for params in ({"limit": 200, "search": slug, "active": "true"}, {"limit": 200, "search": slug}):
            data = _http_json(f"{GAMMA_ROOT}/markets", params=params)
            mkts = []
            if isinstance(data, dict) and "data" in data:
                mkts = data["data"]
            elif isinstance(data, list):
                mkts = data
            if isinstance(mkts, list) and mkts:
                hit = None
                # 优先 slug 精确命中
                for m2 in mkts:
                    if str(m2.get("slug") or "") == slug:
                        hit = m2; break
                # 其次 eventSlug 命中
                if not hit:
                    for m2 in mkts:
                        if str(m2.get("eventSlug") or "") == slug:
                            hit = m2; break
                if hit:
                    yx, nx, tx = _tokens_from_market_obj(hit)
                    if yx and nx:
                        return yx, nx, tx, _market_meta_from_obj(hit)
    # 3) 事件页/事件 slug 回退链路
    event_slug = _extract_event_slug(source)
    if not event_slug:
        raise ValueError("无法从输入中提取事件 slug，且直接解析失败。")
    mkts = _list_markets_under_event(event_slug)
    if not mkts:
        raise ValueError(f"未在事件 {event_slug} 下检索到子问题列表。")
    chosen = _pick_market_subquestion(mkts)
    if "__direct_url__" in chosen:
        y2, n2, title2, raw2 = resolve_token_ids(chosen["__direct_url__"])
        if y2 and n2:
            meta = _market_meta_from_obj(raw2 or {}) if raw2 else {}
            if not meta:
                meta = _maybe_fetch_market_meta_from_source(chosen["__direct_url__"])
            return y2, n2, title2, meta
        raise ValueError("无法从粘贴的URL解析出 tokenId。")
    y3, n3, title3 = _tokens_from_market_obj(chosen)
    if y3 and n3:
        meta = _market_meta_from_obj(chosen)
        return y3, n3, title3, meta
    slug2 = chosen.get("slug") or ""
    if slug2:
        # 兜底：拉完整市场详情；若还不行，再把 /market/<slug> 丢给旧解析器
        m_full = _fetch_market_by_slug(slug2)
        if m_full:
            y4, n4, title4 = _tokens_from_market_obj(m_full)
            if y4 and n4:
                meta = _market_meta_from_obj(m_full)
                return y4, n4, title4, meta
        y5, n5, title5, raw5 = resolve_token_ids(f"https://polymarket.com/market/{slug2}")
        if y5 and n5:
            meta = _market_meta_from_obj(raw5 or {}) if raw5 else {}
            if not meta:
                meta = _maybe_fetch_market_meta_from_source(f"https://polymarket.com/market/{slug2}")
            return y5, n5, title5, meta
    raise ValueError("子问题未包含 tokenId，且兜底解析失败。")

# ====== 下单执行工具 ======
def _floor(x: float, dp: int) -> float:
    q = Decimal(str(x)).quantize(Decimal("1." + "0"*dp), rounding=ROUND_DOWN)
    return float(q)

def _normalize_sell_pair(price: float, size: float) -> Tuple[float, float]:
    # 价格 4dp；份数 2dp（下单时再 floor 一次，确保不超）
    return _floor(price, 4), _floor(size, 2)

def _place_buy_fak(client, token_id: str, price: float, size: float) -> Dict[str, Any]:
    return execute_auto_buy(client=client, token_id=token_id, price=price, size=size)

def _place_sell_fok(client, token_id: str, price: float, size: float) -> Dict[str, Any]:
    from py_clob_client.clob_types import OrderArgs, OrderType
    from py_clob_client.order_builder.constants import SELL
    eff_p, eff_s = _normalize_sell_pair(price, size)
    order = OrderArgs(token_id=str(token_id), side=SELL, price=float(eff_p), size=float(eff_s))
    signed = client.create_order(order)
    return client.post_order(signed, OrderType.FOK)


# ===== 主流程 =====
def main():
    client = _get_client()
    creds_check = _extract_api_creds(client)
    if not creds_check or not creds_check.get("key") or not creds_check.get("secret"):
        print("[ERR] 无法获取完整 API 凭证，请检查配置后重试。")
        return
    print("[INIT] API 凭证已验证。")
    print("[INIT] ClobClient 就绪。")

    print('请输入 Polymarket 市场 URL（支持事件页可多选子问题）：')
    source = input().strip()
    if not source:
        print("[ERR] 未输入，退出。")
        return

    selected_markets: List[dict] = []
    market_meta: Dict[str, Any] = {}
    title_hint = ""

    try:
        if _looks_like_event_source(source):
            event_slug = _extract_event_slug(source)
            markets = _list_markets_under_event(event_slug)
            if not markets:
                print(f"[ERR] 未能在事件 {event_slug} 下解析到子问题。")
                return
            chosen = _pick_market_subquestions(markets)
            for m in chosen:
                if "__direct_url__" in m:
                    y, n, t, raw = resolve_token_ids(m["__direct_url__"])
                    selected_markets.append({
                        "title": t,
                        "clobTokenIds": [y, n],
                        "raw": raw or {},
                    })
                    continue
                selected_markets.append(m)
        else:
            yes_id, no_id, title, market_meta = _resolve_with_fallback(source)
            selected_markets.append({"title": title, "clobTokenIds": [yes_id, no_id]})
            title_hint = title
    except Exception as exc:
        print(f"[ERR] 解析输入失败：{exc}")
        return

    token_candidates: List[Dict[str, str]] = []
    for m in selected_markets:
        token_candidates.extend(_token_entries_from_market(m))

    if not token_candidates:
        print("[ERR] 未能提取到可用的 tokenId。")
        return

    chosen_tokens = _prompt_token_selection(token_candidates)
    if not chosen_tokens:
        print("[ERR] 未选择任何 token，退出。")
        return

    print("请输入每个子问题的买入份数（留空则默认按 1 份执行）：")
    size_raw = input().strip()
    target_size: float = 1.0
    if size_raw:
        try:
            target_size = float(size_raw)
        except Exception:
            print("[ERR] 份数输入非法，退出。")
            return
        if target_size <= 0:
            print("[ERR] 份数必须大于 0。")
            return

    print(f"[RUN] 即将按份数 {target_size} 仅买入 {len(chosen_tokens)} 个子问题…")

    latest_state: Dict[str, Dict[str, Any]] = {}
    last_state_log = 0.0

    def _print_state(update: Dict[str, Any]) -> None:
        nonlocal latest_state, last_state_log
        if not isinstance(update, dict):
            return
        markets = update.get("markets") if isinstance(update.get("markets"), dict) else None
        if markets is None:
            return
        latest_state = markets
        now = time.time()
        if now - last_state_log < 10.0:
            return
        last_state_log = now
        print("[PROGRESS] 当前买入进度：")
        for mid, payload in sorted(latest_state.items()):
            status = payload.get("status") if isinstance(payload, dict) else None
            filled = payload.get("filled") if isinstance(payload, dict) else None
            remaining = payload.get("remaining") if isinstance(payload, dict) else None
            print(
                f"  token_id={mid} | status={status or 'N/A'} | filled={filled if filled is not None else '-'} | remaining={remaining if remaining is not None else '-'}"
            )

    try:
        summary = maker_multi_buy_follow_bid(
            client,
            chosen_tokens,
            target_size=target_size,
            min_quote_amt=1.0,
            min_order_size=API_MIN_ORDER_SIZE,
            progress_probe_interval=10.0,
            state_callback=_print_state,
        )
    except Exception as exc:
        print(f"[ERR] 下单过程中出现异常：{exc}")
        return

    print("[TRADE][BUY][MAKER] 组合下单完成，摘要：")
    for mid, payload in summary.items():
        name = payload.get("name") if isinstance(payload, dict) else None
        result = payload.get("result") if isinstance(payload, dict) else None
        status = None
        filled = None
        if isinstance(result, dict):
            status = result.get("status")
            filled = result.get("filled")
        print(
            f"  token_id={mid} | name={name or mid} | status={status or 'N/A'} | filled={filled if filled is not None else '-'}"
        )

    print("[DONE] 全部买单流程结束。")


if __name__ == "__main__":
    main()
