"""DeepSeek balance + usage routes for the deepseek-quota WebUI extension sidecar.

GET /api/summary[?refresh=1]
    Account balance (official, API-key auth) + today's cost and token usage
    (platform private endpoint, platform userToken auth).

GET /api/usage?range=today|30d[&refresh=1]
    Bucketed token usage per model + cost, for the hover card charts.

Data sources
------------
balance : GET https://api.deepseek.com/user/balance
          Authorization: Bearer <DEEPSEEK_API_KEY>   (official, documented)
usage   : GET https://platform.deepseek.com/api/v0/usage/by_api_key/amount
          GET https://platform.deepseek.com/api/v0/usage/by_api_key/cost
          Authorization: Bearer <DEEPSEEK_PLATFORM_TOKEN>  (platform SPA token,
          localStorage.userToken.value; private endpoint, undocumented)

The API key and the platform token are read server-side from the environment
or ~/.hermes/.env and never reach the browser.
"""
from __future__ import annotations

import json
import math
import os
import re
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

BALANCE_URL = "https://api.deepseek.com/user/balance"
PLATFORM_BASE = "https://platform.deepseek.com/api/v0"
CST = timezone(timedelta(hours=8))
TZ_SEC = 8 * 3600

TTL_TODAY = 60          # today's buckets change on every request; 60s is plenty
TTL_30D = 300           # yesterday-and-earlier only changes at midnight
TTL_BALANCE = 60
TTL_ERROR = 20          # a transient failure must not poison the next minute

_LOCK = threading.Lock()
_CACHE: dict[str, dict] = {}
_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
_SK_RE = re.compile(r"sk-[A-Za-z0-9_-]{6,}")


def _scrub(text: str) -> str:
    """Never let a key fragment from an upstream error reach the browser."""
    return _SK_RE.sub("sk-***", text or "")


# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------
def _load_secret(name: str) -> str | None:
    val = (os.environ.get(name) or "").strip()
    if val:
        return val
    env_path = Path(os.path.expanduser("~/.hermes/.env"))
    try:
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith(name + "="):
                v = line.split("=", 1)[1].strip().strip('"').strip("'")
                return v or None
    except OSError:
        pass
    return None


# --------------------------------------------------------------------------
# time helpers (all buckets are China Standard Time, matching tz=28800)
# --------------------------------------------------------------------------
def _day_start(ts: float) -> int:
    d = datetime.fromtimestamp(ts, CST)
    return int(d.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())


def _epoch(value) -> int | None:
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    if n > 1e11:          # milliseconds slipped in
        n /= 1000.0
    return int(n)


def _num(value) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------
def _http_json(url: str, token: str, platform: bool = True) -> dict:
    headers = {
        "Authorization": "Bearer " + token,
        "Accept": "application/json",
        "User-Agent": _UA,
    }
    if platform:
        headers["Referer"] = "https://platform.deepseek.com/usage"
        headers["Origin"] = "https://platform.deepseek.com"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        snippet = exc.read().decode("utf-8", "replace")[:300]
        raise RuntimeError(f"HTTP {exc.code}: {snippet}") from None
    try:
        raw = json.loads(body)
    except ValueError:
        raise RuntimeError(f"non-JSON response: {body[:160]}") from None
    if not isinstance(raw, dict):
        return {"data": raw}
    return raw


def _unwrap(raw: dict):
    """Platform responses are {code,msg,data:{biz_code,biz_data}} — tolerate the
    older/flat shapes too so a server-side tweak only degrades one panel."""
    code = raw.get("code")
    if code not in (0, 200, None):
        msg = raw.get("msg") or "request failed"
        if code in (40002, 40003):
            raise RuntimeError(f"platform token 无效或已过期({code} {msg})")
        raise RuntimeError(f"platform API {code}: {msg}")
    data = raw.get("data")
    if isinstance(data, dict):
        biz_code = data.get("biz_code")
        if biz_code not in (0, None):
            raise RuntimeError(
                f"platform biz {biz_code}: "
                f"{data.get('biz_msg') or data.get('msg') or 'request failed'}"
            )
        if "biz_data" in data:
            return data.get("biz_data")
    return data


def _fetch_balance(key: str) -> dict:
    raw = _http_json(BALANCE_URL, key, platform=False)
    infos = raw.get("balance_infos") or []
    if not isinstance(infos, list) or not infos:
        raise RuntimeError("balance_infos 为空")
    pick = None
    for info in infos:
        if (info.get("currency") or "").upper() == "CNY":
            pick = info
            break
    pick = pick or infos[0]
    return {
        "currency": (pick.get("currency") or "CNY").upper(),
        "total": _num(pick.get("total_balance")),
        "granted": _num(pick.get("granted_balance")),
        "topped_up": _num(pick.get("topped_up_balance")),
        "available": bool(raw.get("is_available")),
        "wallets": [
            {"currency": (i.get("currency") or "").upper(),
             "total": _num(i.get("total_balance"))}
            for i in infos if isinstance(i, dict)
        ],
    }


def _platform_url(kind: str, start: int, end: int) -> str:
    return (f"{PLATFORM_BASE}/usage/by_api_key/{kind}"
            f"?start={start}&end={end}&tz={TZ_SEC}")


def _fetch_pair(start: int, end: int, token: str) -> dict:
    """Fetch amount + cost in parallel; the WebUI proxy caps a request at ~10s."""
    with ThreadPoolExecutor(max_workers=2) as pool:
        f_amount = pool.submit(_http_json, _platform_url("amount", start, end), token)
        f_cost = pool.submit(_http_json, _platform_url("cost", start, end), token)
        amount_raw = f_amount.result()
        cost_raw = f_cost.result()
    return {"amount": _unwrap(amount_raw), "cost": _unwrap(cost_raw)}


# --------------------------------------------------------------------------
# parsing
# --------------------------------------------------------------------------
SPLIT_KEYS = ("input", "cache", "output", "total", "requests")


def _usage_split(usage: dict) -> dict:
    """Split one bucket's usage into billable token kinds.

    PROMPT_CACHE_HIT_TOKEN / PROMPT_CACHE_MISS_TOKEN are the modern shape;
    PROMPT_TOKEN is the older flat shape, treated as uncached input. Never add
    PROMPT_TOKEN on top of the hit/miss pair — that would double-count.
    """
    hit = _num(usage.get("PROMPT_CACHE_HIT_TOKEN"))
    miss = _num(usage.get("PROMPT_CACHE_MISS_TOKEN"))
    if not (hit or miss):
        miss = _num(usage.get("PROMPT_TOKEN"))
    output = _num(usage.get("RESPONSE_TOKEN"))
    return {
        "input": miss,
        "cache": hit,
        "output": output,
        "total": miss + hit + output,
        "requests": _num(usage.get("REQUEST")),
    }


def _add_split(dst: dict | None, src: dict) -> dict:
    if dst is None:
        dst = {k: 0.0 for k in SPLIT_KEYS}
    for k in SPLIT_KEYS:
        dst[k] = dst.get(k, 0.0) + _num(src.get(k))
    return dst


def _parse_amount(biz: dict) -> dict:
    biz = biz or {}
    by_bucket: dict[int, dict[str, dict]] = {}
    by_model_detail: dict[str, dict] = {}
    requests = 0.0
    for ser in biz.get("series") or []:
        if not isinstance(ser, dict):
            continue
        model = ser.get("model") or "unknown"
        for bucket in ser.get("buckets") or []:
            if not isinstance(bucket, dict):
                continue
            t = _epoch(bucket.get("time"))
            if t is None:
                continue
            split = _usage_split(bucket.get("usage") or {})
            slots = by_bucket.setdefault(t, {})
            slots[model] = _add_split(slots.get(model), split)
            by_model_detail[model] = _add_split(by_model_detail.get(model), split)
            requests += split["requests"]
    return {
        "by_bucket": by_bucket,
        "by_model": {m: d["total"] for m, d in by_model_detail.items()},
        "by_model_detail": by_model_detail,
        "requests": requests,
        "models": [m for m in (biz.get("models") or []) if isinstance(m, str)],
        "bucket": int(biz.get("bucket") or 0),
    }


def _cost_blocks(biz) -> list:
    if isinstance(biz, list):
        return biz
    if isinstance(biz, dict):
        data = biz.get("data")
        if isinstance(data, list):
            return data
        if "currency" in biz:
            return [biz]
    return []


def _parse_cost(biz) -> tuple[dict[int, float], str | None]:
    by_currency: dict[str, dict[int, float]] = {}
    for block in _cost_blocks(biz):
        if not isinstance(block, dict):
            continue
        cur = (block.get("currency") or "CNY").upper()
        bucket_map = by_currency.setdefault(cur, {})
        for ser in block.get("series") or []:
            if not isinstance(ser, dict):
                continue
            for bucket in ser.get("buckets") or []:
                if not isinstance(bucket, dict):
                    continue
                t = _epoch(bucket.get("time"))
                if t is None:
                    continue
                bucket_map[t] = bucket_map.get(t, 0.0) + _num(bucket.get("cost"))
    if not by_currency:
        return {}, None
    currency = "CNY" if "CNY" in by_currency else next(iter(by_currency))
    return by_currency[currency], currency


def _fill_slots(start: int, end: int, bucket: int,
                amount_by_bucket: dict[int, dict[str, dict]],
                cost_by_bucket: dict[int, float], label_fmt: str) -> dict:
    bucket = int(bucket or 3600)
    if bucket <= 0:
        bucket = 3600
    count = max(1, int(math.ceil((end - start) / bucket)))
    slots = []
    total_models: dict[str, float] = {}
    total_tokens = 0.0
    total_cost = 0.0
    total_input = 0.0
    total_cache = 0.0
    total_output = 0.0
    total_requests = 0.0
    for i in range(count):
        t = start + i * bucket
        models = amount_by_bucket.get(t)
        if models is None:
            # tolerate server-side bucket alignment surprises: fold by index
            merged: dict[str, dict] = {}
            for bt, bm in amount_by_bucket.items():
                if int((bt - start) // bucket) == i:
                    for name, det in bm.items():
                        merged[name] = _add_split(merged.get(name), det)
            models = merged
        slot_models: dict[str, float] = {}
        detail: dict[str, dict] = {}
        s_input = s_cache = s_output = s_requests = 0.0
        for name, det in models.items():
            tokens = _num(det.get("total"))
            if tokens <= 0:
                continue
            slot_input = _num(det.get("input"))
            slot_cache = _num(det.get("cache"))
            slot_output = _num(det.get("output"))
            slot_models[name] = tokens
            detail[name] = {
                "input": slot_input,
                "cache": slot_cache,
                "output": slot_output,
            }
            s_input += slot_input
            s_cache += slot_cache
            s_output += slot_output
            s_requests += _num(det.get("requests"))
        tokens = sum(slot_models.values())
        cost = cost_by_bucket.get(t)
        if cost is None:
            cost = sum(v for bt, v in cost_by_bucket.items()
                       if int((bt - start) // bucket) == i)
        label = datetime.fromtimestamp(t, CST).strftime(label_fmt)
        slots.append({
            "t": t,
            "label": label,
            "total": tokens,
            "cost": cost,
            "input": s_input,
            "cache": s_cache,
            "output": s_output,
            "requests": s_requests,
            "models": slot_models,
            "detail": detail,
        })
        total_tokens += tokens
        total_cost += cost
        total_input += s_input
        total_cache += s_cache
        total_output += s_output
        total_requests += s_requests
        for name, val in slot_models.items():
            total_models[name] = total_models.get(name, 0.0) + val
    model_list = [
        {"name": name, "tokens": val}
        for name, val in sorted(total_models.items(), key=lambda kv: -kv[1])
        if val > 0
    ]
    return {
        "slots": slots,
        "models": model_list,
        "totals": {
            "tokens": total_tokens,
            "cost": total_cost,
            "input": total_input,
            "cache": total_cache,
            "output": total_output,
            "requests": total_requests,
        },
        "bucket": bucket,
    }


# --------------------------------------------------------------------------
# cached upstream bundles
# --------------------------------------------------------------------------
def _cached(name: str, ttl: int, builder, refresh: bool):
    now = time.time()
    with _LOCK:
        ent = _CACHE.get(name)
        if ent and not refresh and now - ent["at"] < ent["ttl"]:
            return ent["payload"]
    try:
        payload = builder()
        use_ttl = ttl
    except Exception as exc:  # noqa: BLE001 — surface as JSON, never crash
        payload = {"__error__": f"{type(exc).__name__}: {_scrub(str(exc))}"}
        use_ttl = TTL_ERROR
    with _LOCK:
        _CACHE[name] = {"at": now, "payload": payload, "ttl": use_ttl}
    return payload


def _today_bundle(refresh: bool):
    now = int(time.time())
    start = _day_start(now)
    # The platform rejects unaligned ranges with biz 1 INVALID_PARAM. A full
    # [today 00:00, tomorrow 00:00) range is aligned and yields exactly 24
    # hourly buckets; future hours simply come back empty.
    end = start + 86400
    token = _load_secret("DEEPSEEK_PLATFORM_TOKEN")
    if not token:
        return {"__error__": "未配置 DEEPSEEK_PLATFORM_TOKEN"}
    return _cached(
        f"today:{start}", TTL_TODAY,
        lambda: _fetch_pair(start, end, token), refresh,
    )


def _days_bundle(days: int, refresh: bool):
    now = int(time.time())
    start = _day_start(now) - (days - 1) * 86400
    end = start + days * 86400       # exactly `days` aligned daily buckets
    token = _load_secret("DEEPSEEK_PLATFORM_TOKEN")
    if not token:
        return {"__error__": "未配置 DEEPSEEK_PLATFORM_TOKEN"}
    return _cached(
        f"{days}d:{start}", TTL_30D,
        lambda: _fetch_pair(start, end, token), refresh,
    )


# --------------------------------------------------------------------------
# route registration
# --------------------------------------------------------------------------
def register(app) -> None:
    @app.route("GET", "/api/summary")
    def summary(req):
        refresh = req.query_one("refresh") in ("1", "true", "yes", "on")
        payload = {
            "ok": False,
            "balance": None,
            "balance_error": None,
            "usage_ok": False,
            "usage_error": None,
            "today": None,
            "currency": "CNY",
            "fetched_at": time.time(),
        }

        api_key = _load_secret("DEEPSEEK_API_KEY")
        if not api_key:
            payload["balance_error"] = "未配置 DEEPSEEK_API_KEY"
        else:
            bal = _cached("balance", TTL_BALANCE,
                          lambda: _fetch_balance(api_key), refresh)
            if "__error__" in bal:
                payload["balance_error"] = bal["__error__"]
            else:
                payload["balance"] = bal
                payload["currency"] = bal.get("currency") or "CNY"
                payload["ok"] = True

        bundle = _today_bundle(refresh)
        if "__error__" in bundle:
            payload["usage_error"] = bundle["__error__"]
        else:
            amount = bundle.get("amount") or {}
            cost_biz = bundle.get("cost")
            parsed_amount = _parse_amount(amount)
            cost_map, currency = _parse_cost(cost_biz)
            start = _epoch(amount.get("start")) or _day_start(time.time())
            end = _epoch(amount.get("end")) or int(time.time())
            filled = _fill_slots(start, end,
                                 parsed_amount.get("bucket") or 3600,
                                 parsed_amount["by_bucket"], cost_map, "%H")
            total_cost = sum(cost_map.values())
            payload["today"] = {
                "tokens": sum(parsed_amount["by_model"].values()),
                "cost": total_cost,
                "currency": currency or payload["currency"],
                "requests": parsed_amount["requests"],
                "by_model": parsed_amount["by_model"],
                "by_model_detail": parsed_amount["by_model_detail"],
                "input": filled["totals"].get("input", 0),
                "cache": filled["totals"].get("cache", 0),
                "output": filled["totals"].get("output", 0),
                "window_start": start,
                "window_end": end,
                "hourly_peak": max((s["total"] for s in filled["slots"]), default=0),
            }
            payload["usage_ok"] = True
            payload["ok"] = True
            if currency:
                payload["currency"] = currency

        return app.json(payload)

    @app.route("GET", "/api/usage")
    def usage(req):
        rng = req.query_one("range", "today")
        refresh = req.query_one("refresh") in ("1", "true", "yes", "on")
        if rng not in ("today", "30d"):
            return app.json({"ok": False, "error": "range must be today|30d"})

        if rng == "today":
            bundle = _today_bundle(refresh)
            label_fmt = "%H"
        else:
            bundle = _days_bundle(30, refresh)
            label_fmt = "%m-%d"

        if "__error__" in bundle:
            return app.json({
                "ok": False,
                "range": rng,
                "error": bundle["__error__"],
                "slots": [],
                "models": [],
                "totals": {"tokens": 0, "cost": 0},
                "fetched_at": time.time(),
            })

        amount_biz = bundle.get("amount") or {}
        parsed_amount = _parse_amount(amount_biz)
        cost_map, currency = _parse_cost(bundle.get("cost"))
        start = _epoch(amount_biz.get("start")) or _day_start(time.time())
        end = _epoch(amount_biz.get("end")) or int(time.time())
        filled = _fill_slots(start, end,
                             parsed_amount.get("bucket") or
                             (3600 if rng == "today" else 86400),
                             parsed_amount["by_bucket"], cost_map, label_fmt)
        return app.json({
            "ok": True,
            "range": rng,
            "currency": currency or "CNY",
            "tz": TZ_SEC,
            "window_start": start,
            "window_end": end,
            "slots": filled["slots"],
            "models": filled["models"],
            "totals": filled["totals"],
            "bucket": filled["bucket"],
            "fetched_at": time.time(),
        })
