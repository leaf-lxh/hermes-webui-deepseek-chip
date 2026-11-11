#!/usr/bin/env python3
"""Interactively set DEEPSEEK_PLATFORM_TOKEN in ~/.hermes/.env.

Why a script instead of a shell one-liner: the token must never be typed as a
command argument (argv is readable via /proc, and chat UIs may mask or rewrite
KEY=$VAR text). getpass keeps it out of argv and shell history.

- removes every existing DEEPSEEK_PLATFORM_TOKEN= line (including a literal
  "***" placeholder line), so the first-match reader cannot pick a stale value
- prompts with hidden input (the token is never echoed)
- writes atomically with mode 600
- verifies through the running sidecar without printing the token

Usage:  python3 scripts/set_token.py
"""
from __future__ import annotations

import getpass
import json
import os
import sys
import urllib.request
from pathlib import Path

KEY = "DEEPSEEK_PLATFORM_TOKEN"
ENV = Path(os.environ.get("DSQ_ENV_PATH") or (Path.home() / ".hermes" / ".env"))
TOKEN_FILE = Path.home() / ".hermes" / "webui" / "sidecar-auth" / "deepseek-quota.token"
USAGE_URL = "http://127.0.0.1:17801/api/usage?range=today&refresh=1"


def rewrite(token: str) -> None:
    old = ENV.read_text(encoding="utf-8").splitlines() if ENV.exists() else []
    kept = [line for line in old if not line.startswith(KEY + "=")]
    kept.append(f"{KEY}={token}")
    tmp = ENV.with_name(ENV.name + ".tmp")
    tmp.write_text("\n".join(kept) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, ENV)


def verify() -> str:
    if not TOKEN_FILE.exists():
        return "跳过（sidecar token 不存在，consent 未批准？）"
    try:
        req = urllib.request.Request(
            USAGE_URL,
            headers={"X-Hermes-Sidecar-Token": TOKEN_FILE.read_text(encoding="utf-8").strip()},
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if data.get("ok"):
            totals = data.get("totals") or {}
            return "OK：今日 %.0f tokens / %.4f %s" % (
                float(totals.get("tokens") or 0),
                float(totals.get("cost") or 0),
                data.get("currency") or "CNY",
            )
        return "失败：" + str(data.get("error") or data)
    except Exception as exc:  # noqa: BLE001
        return f"验证请求出错：{type(exc).__name__}: {exc}"


def main() -> int:
    print(f"目标文件：{ENV}")
    print("粘贴平台 userToken（输入不回显），回车结束：")
    token = getpass.getpass("token: ").strip()
    if token.startswith(KEY + "="):
        token = token.split("=", 1)[1].strip().strip('"').strip("'")
    if not token or token == "***" or len(token) < 20 or any(ch.isspace() for ch in token):
        print("拒绝写入：为空 / 仍是占位符 / 长度过短 / 含空白字符。", file=sys.stderr)
        return 2
    rewrite(token)
    print(f"已写入 {len(token)} 个字符，旧行（含 ***）已替换，权限 600。")
    print("验证：", verify())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
