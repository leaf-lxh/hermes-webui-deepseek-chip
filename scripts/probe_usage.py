#!/usr/bin/env python3
"""Read-only probe for the DeepSeek platform usage endpoints.

Run this after putting the platform token in ~/.hermes/.env:

    python3 scripts/probe_usage.py            # compact summary + shape
    python3 scripts/probe_usage.py --raw      # full JSON (no secrets)

It never prints the token. Use it to confirm the response shape if the
sidecar reports a parse error after a DeepSeek platform change.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "sidecar"))
import routes_impl as r  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--range", choices=["today", "30d"], default="today")
    ap.add_argument("--raw", action="store_true")
    args = ap.parse_args()

    token = r._load_secret("DEEPSEEK_PLATFORM_TOKEN")
    if not token:
        print("DEEPSEEK_PLATFORM_TOKEN 未配置(环境变量或 ~/.hermes/.env)", file=sys.stderr)
        return 2

    now = int(time.time())
    if args.range == "today":
        start = r._day_start(now)
        end = start + 86400            # aligned; unaligned end => INVALID_PARAM
        label = "%H"
    else:
        start = r._day_start(now) - 29 * 86400
        end = start + 30 * 86400
        label = "%m-%d"

    pair = r._fetch_pair(start, end, token)
    if args.raw:
        print(json.dumps(pair, ensure_ascii=False, indent=2))
        return 0

    amount = pair["amount"] or {}
    cost_biz = pair["cost"]
    parsed_amount = r._parse_amount(amount)
    cost_map, currency = r._parse_cost(cost_biz)
    filled = r._fill_slots(
        int(amount.get("start") or start),
        int(amount.get("end") or end),
        parsed_amount.get("bucket") or (3600 if args.range == "today" else 86400),
        parsed_amount["by_bucket"], cost_map, label,
    )
    print(f"window       : {start} -> {end} (tz +08)")
    print(f"bucket       : {filled['bucket']}s, slots {len(filled['slots'])}")
    print(f"models       : {[m['name'] for m in filled['models']]}")
    print(f"total tokens : {filled['totals']['tokens']:.0f}")
    print(f"total cost   : {filled['totals']['cost']:.4f} {currency or ''}")
    print(f"requests     : {parsed_amount['requests']:.0f}")
    print("first slot   :", json.dumps(filled["slots"][0], ensure_ascii=False))
    print("last slot    :", json.dumps(filled["slots"][-1], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
