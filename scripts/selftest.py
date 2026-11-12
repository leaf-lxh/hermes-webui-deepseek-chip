#!/usr/bin/env python3
"""Offline parser self-test — no network, no credentials.

Feeds fixture payloads shaped like the documented platform responses through
the parsers and checks totals/buckets. Run: python3 scripts/selftest.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "sidecar"))
import routes_impl as r  # noqa: E402

START = 1794240000  # 2026-11-10 00:00 +08 (fixture only)


def amount_fixture() -> dict:
    return {
        "start": START,
        "end": START + 86400,
        "bucket": 3600,
        "models": ["deepseek-v4-flash", "deepseek-v4-pro"],
        "series": [
            {
                "api_key": {"tracking_id": "k1", "name": "main", "valid": True},
                "model": "deepseek-v4-flash",
                "buckets": [
                    {"time": START, "usage": {
                        "PROMPT_CACHE_HIT_TOKEN": "1000",
                        "PROMPT_CACHE_MISS_TOKEN": "2000",
                        "RESPONSE_TOKEN": "3000",
                        "REQUEST": "5",
                    }},
                    {"time": START + 3600, "usage": {
                        "PROMPT_TOKEN": "7000",
                        "RESPONSE_TOKEN": "1000",
                        "REQUEST": "2",
                    }},
                ],
            },
            {
                "api_key": {"tracking_id": "k1", "name": "main", "valid": True},
                "model": "deepseek-v4-pro",
                "buckets": [
                    {"time": START, "usage": {
                        "PROMPT_CACHE_HIT_TOKEN": "500",
                        "PROMPT_CACHE_MISS_TOKEN": "500",
                        "RESPONSE_TOKEN": "1000",
                        "REQUEST": "1",
                    }},
                ],
            },
        ],
    }


def cost_fixture() -> list:
    return [{
        "currency": "CNY",
        "series": [
            {"api_key": {"tracking_id": "k1"}, "model": "deepseek-v4-flash",
             "buckets": [{"time": START, "cost": "1.5270522000000000"},
                         {"time": START + 3600, "cost": "0.2"}]},
            {"api_key": {"tracking_id": "k1"}, "model": "deepseek-v4-pro",
             "buckets": [{"time": START, "cost": "0.5"}]},
        ],
    }]


def main() -> int:
    failures: list[str] = []

    parsed = r._parse_amount(amount_fixture())
    # 1000+2000+3000 = 6000 (flash @0) ; flash @1h: 7000+1000 = 8000
    # pro @0: 500+500+1000 = 2000 -> day total 16000
    if abs(sum(parsed["by_model"].values()) - 16000) > 1e-9:
        failures.append(f"amount total {sum(parsed['by_model'].values())} != 16000")
    if parsed["requests"] != 8:
        failures.append(f"requests {parsed['requests']} != 8")
    if parsed["bucket"] != 3600:
        failures.append(f"bucket {parsed['bucket']} != 3600")
    # slot 0 split: flash 1000 hit / 2000 miss / 3000 out + pro 500/500/1000
    if abs(parsed["by_model_detail"]["deepseek-v4-flash"]["total"] - 14000) > 1e-9:
        failures.append("by_model_detail flash total != 14000")
    if abs(parsed["by_model_detail"]["deepseek-v4-flash"]["cache"] - 1000) > 1e-9:
        failures.append("by_model_detail flash cache != 1000")

    cost_map, currency = r._parse_cost(cost_fixture())
    if abs(sum(cost_map.values()) - 2.2270522) > 1e-6:
        failures.append(f"cost total {sum(cost_map.values())} != 2.2270522")
    if currency != "CNY":
        failures.append(f"currency {currency} != CNY")

    # wrapped shape: data[].currency (some server versions)
    wrapped, currency2 = r._parse_cost({"data": cost_fixture()})
    if abs(sum(wrapped.values()) - sum(cost_map.values())) > 1e-9 or currency2 != "CNY":
        failures.append("wrapped cost shape not handled")

    filled = r._fill_slots(START, START + 86400, 3600,
                           parsed["by_bucket"], cost_map, "%H")
    if len(filled["slots"]) != 24:
        failures.append(f"slots {len(filled['slots'])} != 24")
    if filled["slots"][0]["label"] != "00":
        failures.append(f"label {filled['slots'][0]['label']} != 00")
    if abs(filled["totals"]["tokens"] - 16000) > 1e-9:
        failures.append(f"filled tokens {filled['totals']['tokens']} != 16000")
    if abs(filled["slots"][0]["cost"] - 2.0270522) > 1e-6:
        failures.append(f"slot0 cost {filled['slots'][0]['cost']} != 2.0270522")
    # slot 0: input 2500, cache 1500, output 4000; slot 1 (legacy PROMPT_TOKEN)
    # must count as uncached input, not vanish.
    s0 = filled["slots"][0]
    if (abs(s0["input"] - 2500) > 1e-9 or abs(s0["cache"] - 1500) > 1e-9
            or abs(s0["output"] - 4000) > 1e-9):
        failures.append(f"slot0 split {s0['input']}/{s0['cache']}/{s0['output']} != 2500/1500/4000")
    s1 = filled["slots"][1]
    if abs(s1["input"] - 7000) > 1e-9 or abs(s1["cache"]) > 1e-9:
        failures.append("legacy PROMPT_TOKEN not mapped to uncached input")
    if (abs(filled["totals"]["input"] - 9500) > 1e-9
            or abs(filled["totals"]["cache"] - 1500) > 1e-9
            or abs(filled["totals"]["output"] - 5000) > 1e-9):
        failures.append("totals split != 9500/1500/5000")

    # 30-day slot count must be exactly 30, even at 00:00:05
    start30 = r._day_start(START) - 29 * 86400
    end30 = max(START + 5, start30 + 29 * 86400 + 60)
    filled30 = r._fill_slots(start30, end30, 86400, {}, {}, "%m-%d")
    if len(filled30["slots"]) != 30:
        failures.append(f"30d slots {len(filled30['slots'])} != 30")

    if failures:
        print("FAIL")
        for f in failures:
            print("  -", f)
        return 1
    print("OK: amount/cost parsing, 24h and 30d bucket filling")
    print("   ", json.dumps(filled["slots"][0], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
