#!/usr/bin/env bash
# ============================================================================
# deepseek-quota 插件包生命周期管理
#
#   manage.sh install | uninstall | status | doctor
#
# 管理的对象(全部在 WebUI 状态目录 / systemd user 单元,上游零改动):
#   ~/.hermes/webui/extensions/deepseek-quota/          扩展文件(rsync 自本包)
#   ~/.hermes/webui/extension-install-manifest.json     安装登记(files 含 sidecar/)
#   ~/.hermes/webui/extension-overrides.json            代理 consent(卸载时撤销)
#   ~/.hermes/webui/sidecar-auth/deepseek-quota.token   per-extension token(卸载时删除)
#   ~/.config/systemd/user/deepseek-quota-sidecar.service
#
# 秘密(DEEPSEEK_API_KEY / DEEPSEEK_PLATFORM_TOKEN)不进本包,
# sidecar 运行时读 ~/.hermes/.env。
# ============================================================================
set -euo pipefail

EXT_ID="deepseek-quota"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNIT_NAME="deepseek-quota-sidecar.service"
PORT=17801
HEALTH_URL="http://127.0.0.1:${PORT}/health"
WEBUI_REPO="${HERMES_WEBUI_REPO:-/root/production/hermes-webui}"

# ---- 状态目录解析 ----
STATE_DIR="${HERMES_WEBUI_STATE_DIR:-}"
if [ -z "$STATE_DIR" ] && [ -f "$HOME/.hermes/webui.ctl.env" ]; then
  STATE_DIR="$(sed -n 's/^STATE_DIR=//p' "$HOME/.hermes/webui.ctl.env" | tail -1)"
fi
STATE_DIR="${STATE_DIR:-$HOME/.hermes/webui}"

EXT_ROOT="${HERMES_WEBUI_EXTENSION_DIR:-$STATE_DIR/extensions}"
EXT_DIR="$EXT_ROOT/$EXT_ID"
INSTALL_MANIFEST="$STATE_DIR/extension-install-manifest.json"
OVERRIDES="$STATE_DIR/extension-overrides.json"
TOKEN_FILE="$STATE_DIR/sidecar-auth/$EXT_ID.token"
UNIT_SRC="$SRC_DIR/lifecycle/$UNIT_NAME"
UNIT_DST="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/$UNIT_NAME"

log()  { printf '\033[1;32m[*]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[!]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[x]\033[0m %s\n' "$*" >&2; exit 1; }

usage() {
  sed -n '2,12p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
  exit 1
}

_registered() {
  python3 - "$INSTALL_MANIFEST" "$EXT_ID" <<'PY' >/dev/null 2>&1
import json, sys
try:
    doc = json.load(open(sys.argv[1]))
    raise SystemExit(0 if sys.argv[2] in (doc.get("installed") or {}) else 1)
except (OSError, ValueError):
    raise SystemExit(1)
PY
}

_consented() {
  python3 - "$OVERRIDES" "$EXT_ID" <<'PY' >/dev/null 2>&1
import json, sys
try:
    doc = json.load(open(sys.argv[1]))
    raise SystemExit(0 if sys.argv[2] in (doc.get("sidecar_proxy_consents") or {}) else 1)
except (OSError, ValueError):
    raise SystemExit(1)
PY
}

cmd_install() {
  [ -f "$SRC_DIR/manifest.json" ] || die "包不完整:缺 manifest.json"
  [ -f "$UNIT_SRC" ] || die "包不完整:缺 lifecycle/$UNIT_NAME"

  log "1/4 同步包文件 -> $EXT_DIR"
  mkdir -p "$EXT_DIR"
  rm -rf "$EXT_DIR/sidecar/__pycache__" "$EXT_DIR/scripts/__pycache__"
  rsync -a --delete \
    --exclude 'lifecycle/' --exclude '.git/' --exclude '.gitignore' \
    --exclude 'manage.sh' --exclude 'preview/' \
    --exclude '__pycache__/' --exclude '*.pyc' --exclude '.DS_Store' \
    "$SRC_DIR"/ "$EXT_DIR"/
  # --delete does not remove excluded files already on the destination; sweep
  # stale caches explicitly so the registered file list only has real assets.
  find "$EXT_DIR" -type d -name '__pycache__' -prune -exec rm -rf {} + 2>/dev/null || true

  log "2/4 登记 install-manifest(files 列表含 sidecar/)"
  python3 - "$EXT_DIR" "$INSTALL_MANIFEST" <<'PYEOF'
import json, os, sys
from datetime import datetime, timezone
from pathlib import Path

ext_dir, manifest_path = Path(sys.argv[1]), Path(sys.argv[2])
version = "unknown"
try:
    version = json.loads((ext_dir / "extension.json").read_text(encoding="utf-8")).get("version", "unknown")
except Exception:
    pass
files = sorted(p.relative_to(ext_dir).as_posix() for p in ext_dir.rglob("*") if p.is_file())
doc = {}
if manifest_path.exists():
    try:
        doc = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as e:
        sys.exit(f"install-manifest 解析失败: {e}")
if not isinstance(doc.get("installed"), dict):
    doc["installed"] = {}
doc.setdefault("version", 1)
doc["installed"][ext_dir.name] = {
    "version": version,
    "files": files,
    "installed_at": datetime.now(timezone.utc).isoformat(),
}
tmp = manifest_path.with_name(manifest_path.name + ".tmp")
tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
os.chmod(tmp, 0o644)
os.replace(tmp, manifest_path)
print(f"    registered {len(files)} files, version {version}")
PYEOF

  log "3/4 部署 systemd 单元并重启 sidecar"
  mkdir -p "$(dirname "$UNIT_DST")"
  install -m 644 "$UNIT_SRC" "$UNIT_DST"
  systemctl --user daemon-reload
  systemctl --user enable "$UNIT_NAME" >/dev/null 2>&1
  systemctl --user restart "$UNIT_NAME"

  log "4/4 等待 /health 探活(最多 15s)"
  ok=""
  for _ in $(seq 1 15); do
    code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 2 "$HEALTH_URL" 2>/dev/null || true)"
    if [ "$code" = "200" ]; then ok=1; break; fi
    sleep 1
  done
  if [ -z "$ok" ]; then
    journalctl --user -u "$UNIT_NAME" -n 20 --no-pager || true
    die "sidecar 健康检查失败(见上方日志)"
  fi
  log "    health OK"

  if _consented && [ -f "$TOKEN_FILE" ]; then
    log "代理 consent 已存在,无需操作"
  else
    warn "代理 consent 尚未批准,芯片还拿不到数据:"
    warn "    Settings -> Extensions -> deepseek-quota sidecar 卡片 -> 批准代理"
    warn "    (批准时 WebUI 会铸造 token;批准后刷新页面即可)"
  fi
  log "install 完成"
}

cmd_uninstall() {
  log "1/4 停止并移除 systemd 单元"
  systemctl --user stop "$UNIT_NAME" 2>/dev/null || true
  systemctl --user disable "$UNIT_NAME" 2>/dev/null || true
  rm -f "$UNIT_DST"
  systemctl --user daemon-reload
  systemctl --user reset-failed "$UNIT_NAME" 2>/dev/null || true

  log "2/4 撤销代理 consent 并删除 token"
  if [ -f "$OVERRIDES" ]; then
    python3 - "$OVERRIDES" "$EXT_ID" <<'PYEOF'
import json, os, sys
from pathlib import Path

path, ext_id = Path(sys.argv[1]), sys.argv[2]
try:
    doc = json.loads(path.read_text(encoding="utf-8"))
except Exception as e:
    sys.exit(f"overrides 解析失败: {e}")
cons = doc.get("sidecar_proxy_consents") or {}
if ext_id in cons:
    del cons[ext_id]
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)
    print("    consent revoked")
else:
    print("    consent 本来就不存在")
PYEOF
  fi
  rm -f "$TOKEN_FILE"

  log "3/4 删除扩展文件 $EXT_DIR"
  rm -rf "$EXT_DIR"

  log "4/4 清除 install-manifest 登记"
  if [ -f "$INSTALL_MANIFEST" ]; then
    python3 - "$INSTALL_MANIFEST" "$EXT_ID" <<'PYEOF'
import json, os, sys
from pathlib import Path

path, ext_id = Path(sys.argv[1]), sys.argv[2]
try:
    doc = json.loads(path.read_text(encoding="utf-8"))
except Exception as e:
    sys.exit(f"install-manifest 解析失败: {e}")
installed = doc.get("installed") or {}
if ext_id in installed:
    del installed[ext_id]
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)
    print("    deregistered")
else:
    print("    本来就没登记")
PYEOF
  fi
  log "uninstall 完成 —— 刷新 WebUI 页面,芯片即消失"
}

cmd_status() {
  echo "== extension =="
  if [ -d "$EXT_DIR" ]; then
    echo "  目录:     $EXT_DIR ($(find "$EXT_DIR" -type f | wc -l) 个文件)"
  else
    echo "  目录:     (不存在)"
  fi
  reg="no";  _registered && reg="yes"
  cons="no"; _consented && cons="yes"
  echo "  登记:     $reg    代理consent: $cons"
  if [ -f "$TOKEN_FILE" ]; then
    echo "  token:    存在(权限 $(stat -c '%a' "$TOKEN_FILE"))"
  else
    echo "  token:    不存在"
  fi
  echo "== sidecar =="
  echo "  服务:     $(systemctl --user is-active "$UNIT_NAME" 2>/dev/null || echo unknown)" \
       "/ $(systemctl --user is-enabled "$UNIT_NAME" 2>/dev/null || echo 未安装)"
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 2 "$HEALTH_URL" 2>/dev/null || true)"
  echo "  health:   HTTP ${code:-000}  ($HEALTH_URL)"
}

cmd_doctor() {
  echo "== 1/5 凭证 =="
  if grep -q '^DEEPSEEK_API_KEY=' "$HOME/.hermes/.env" 2>/dev/null; then
    echo "  ~/.hermes/.env 有 DEEPSEEK_API_KEY  OK(余额)"
  else
    warn "  ~/.hermes/.env 没有 DEEPSEEK_API_KEY —— 余额查不了"
  fi
  if grep -q '^DEEPSEEK_PLATFORM_TOKEN=' "$HOME/.hermes/.env" 2>/dev/null; then
    echo "  ~/.hermes/.env 有 DEEPSEEK_PLATFORM_TOKEN  OK(用量/费用)"
  else
    warn "  没有 DEEPSEEK_PLATFORM_TOKEN —— 今日费用/token 与图表会显示未配置"
  fi

  echo "== 2/5 端口 $PORT =="
  if ss -ltn 2>/dev/null | grep -q ":$PORT "; then
    echo "  已监听  OK"
  else
    warn "  未监听(服务没起?)"
  fi

  echo "== 3/5 token 文件 =="
  if [ -f "$TOKEN_FILE" ]; then
    echo "  存在,权限 $(stat -c '%a' "$TOKEN_FILE")(要求 600)  OK"
  else
    warn "  不存在 —— consent 未批准或 WebUI 没铸造"
  fi

  echo "== 4/5 WebUI 注入链路 =="
  if [ -d "$WEBUI_REPO" ]; then
    tmpcheck="$(mktemp /tmp/deepseek-quota-inject.XXXXXX.py)"
    cat > "$tmpcheck" <<'PYEOF'
import json, sys
sys.path.insert(0, sys.argv[1])
from api.extensions import get_extension_status
s = get_extension_status()
print(json.dumps({
    "inject_js":  any("/extensions/deepseek-quota/assets/deepseek-quota.js" in u for u in (s.get("script_urls") or [])),
    "inject_css": any("/extensions/deepseek-quota/assets/deepseek-quota.css" in u for u in (s.get("stylesheet_urls") or [])),
    "warnings":   s.get("warnings") or [],
}))
PYEOF
    out="$(cd "$WEBUI_REPO" && python3 "$tmpcheck" "$WEBUI_REPO" 2>&1 || true)"
    rm -f "$tmpcheck"
    echo "$out" | grep -v 'No module named' | sed 's/^/  /'
    echo "$out" | grep -q '"inject_js": true' \
      && echo "  注入链路 OK" \
      || warn "  注入链路异常(看上方 warnings)"
  else
    warn "  WebUI 仓库不存在: $WEBUI_REPO(可用 HERMES_WEBUI_REPO 覆盖)"
  fi

  echo "== 5/5 端到端取数(sidecar + token) =="
  if [ -f "$TOKEN_FILE" ]; then
    hdr="$(mktemp /tmp/deepseek-quota-hdr.XXXXXX)"
    trap 'rm -f "$hdr"' RETURN
    printf 'X-Hermes-Sidecar-Token: %s\n' "$(cat "$TOKEN_FILE")" > "$hdr"
    chmod 600 "$hdr"
    for path in "/api/summary" "/api/usage?range=30d"; do
      resp="$(curl -s --max-time 12 -H @"$hdr" "http://127.0.0.1:$PORT$path" || true)"
      echo "  $path -> $(echo "$resp" | head -c 320)"
    done
    rm -f "$hdr"
  else
    warn "  跳过(无 token)"
  fi
}

case "${1:-}" in
  install)   cmd_install ;;
  uninstall) cmd_uninstall ;;
  status)    cmd_status ;;
  doctor)    cmd_doctor ;;
  *)         usage ;;
esac
