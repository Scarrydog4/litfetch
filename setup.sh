#!/bin/bash
# litfetch 即用包一键安装器：装到 ~/.litfetch 并注册到检测到的 MCP 客户端
# 用法：bash setup.sh [--test] [--only zcode,claude-desktop,claude-code,cursor,codex]
#   --test  安装后跑一次真实检索验证链路
set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
DEST="${LITFETCH_DEST:-$HOME/.litfetch}"
PY="$(command -v python3 || true)"
ONLY="all"
ONLY_NEXT=0
TEST=0
for a in "$@"; do
  case "$a" in
    --test) TEST=1 ;;
    --only) ONLY_NEXT=1 ;;
    *) if [ "$ONLY_NEXT" = 1 ]; then ONLY="$a"; ONLY_NEXT=0; fi ;;
  esac
done

say() { printf '[setup] %s\n' "$*"; }
warn() { printf '[setup][警告] %s\n' "$*"; }

if [ -z "$PY" ]; then
  warn "未找到 python3。macOS: brew install python3（或 xcode-select --install）"
  exit 1
fi
if ! "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3,8) else 1)'; then
  warn "需要 python3 >= 3.8（当前 $("$PY" --version)）"
  exit 1
fi

say "安装文件到 $DEST"
mkdir -p "$DEST"
cp -f "$SRC/litfetch.py" "$SRC/mcp_server.py" "$DEST/"
if [ -f "$SRC/session.json" ]; then
  cp -f "$SRC/session.json" "$DEST/"
else
  warn "包里没有 session.json（未带会员凭据），装完后需自行放入"
fi
chmod +x "$DEST/litfetch.py" "$DEST/mcp_server.py"

"$PY" - "$DEST" <<'PYEOF'
import sys
sys.path.insert(0, sys.argv[1])
import litfetch
assert litfetch._aes_ecb_encrypt(b"Q5vGEmoCW59MW4Qc", b"x" * 16)
print("[setup] 核心模块自检通过（纯标准库，无第三方依赖）")
PYEOF

REG_JSON='
import json, os, shutil, sys
path, key_path, server_json, py, script = sys.argv[1:6]
os.makedirs(os.path.dirname(path), exist_ok=True)
cfg = {}
if os.path.exists(path):
    try:
        cfg = json.load(open(path))
    except Exception as e:
        sys.exit("配置不是合法JSON，已跳过，请手工注册: %s (%s)" % (path, e))
    shutil.copy(path, path + ".bak-litfetch")
node = cfg
for k in key_path.split("."):
    node = node.setdefault(k, {})
if isinstance(node.get("litfetch"), dict):
    print("[setup] %s: 已存在，更新" % path)
node["litfetch"] = json.loads(server_json)
json.dump(cfg, open(path, "w"), ensure_ascii=False, indent=2)
print("[setup] 已注册: %s" % path)
'

regtmp="$(mktemp /tmp/litfetch_reg.XXXXXX)"
printf '%s' "$REG_JSON" > "$regtmp"

register_zcode() {
  "$PY" "$regtmp" "$HOME/.zcode/cli/config.json" "mcp.servers" \
    "{\"type\":\"stdio\",\"command\":\"$PY\",\"args\":[\"$DEST/mcp_server.py\"],\"timeoutMs\":300000,\"enabled\":true}" \
    "$PY" "$DEST/mcp_server.py" || warn "ZCode 注册失败"
}

register_claude_desktop() {
  "$PY" "$regtmp" "$HOME/Library/Application Support/Claude/claude_desktop_config.json" "mcpServers" \
    "{\"command\":\"$PY\",\"args\":[\"$DEST/mcp_server.py\"]}" \
    "$PY" "$DEST/mcp_server.py" || warn "Claude Desktop 注册失败"
}

register_cursor() {
  "$PY" "$regtmp" "$HOME/.cursor/mcp.json" "mcpServers" \
    "{\"command\":\"$PY\",\"args\":[\"$DEST/mcp_server.py\"]}" \
    "$PY" "$DEST/mcp_server.py" || warn "Cursor 注册失败"
}

register_claude_code() {
  if command -v claude >/dev/null 2>&1; then
    if claude mcp list 2>/dev/null | grep -q "litfetch"; then
      say "Claude Code: 已存在，跳过"
    else
      if claude mcp add --scope user litfetch -- "$PY" "$DEST/mcp_server.py"; then
        say "Claude Code: 已注册（user 级）"
      else
        warn "Claude Code 注册失败，可手工执行: claude mcp add --scope user litfetch -- $PY $DEST/mcp_server.py"
      fi
    fi
  else
    warn "未检测到 claude 命令，跳过 Claude Code"
  fi
}

register_codex() {
  f="$HOME/.codex/config.toml"
  if [ ! -f "$f" ]; then
    warn "未检测到 ${f}，跳过 Codex"
    return
  fi
  if grep -q 'mcp_servers\.litfetch' "$f"; then
    say "Codex: 已存在，跳过"
    return
  fi
  cp "$f" "$f.bak-litfetch"
  {
    printf '\n[mcp_servers.litfetch]\n'
    printf 'command = "%s"\n' "$PY"
    printf 'args = ["%s"]\n' "$DEST/mcp_server.py"
  } >> "$f"
  say "已注册: $f"
}

wants() {
  if [ "$ONLY" = "all" ]; then return 0; fi
  case ",$ONLY," in
    *",$1,"*) return 0 ;;
    *) return 1 ;;
  esac
}

say "检测并注册 MCP 客户端（范围: ${ONLY}）"
if wants zcode;          then register_zcode; fi
if wants claude-desktop; then register_claude_desktop; fi
if wants cursor;         then register_cursor; fi
if wants claude-code;    then register_claude_code; fi
if wants codex;          then register_codex; fi

if [ "$TEST" = "1" ]; then
  say "真实检索自检（不下载文件）"
  if "$PY" "$DEST/litfetch.py" search "碳中和" >/dev/null 2>&1; then
    say "链路验证通过"
  else
    warn "检索自检失败：检查 session.json 是否过期、网络是否可达"
  fi
fi

cat <<'EOF'

安装完成。各客户端重启后生效：
  ZCode:          重启会话，Settings -> MCP 应显示 litfetch
  Claude Code:    新会话里 /mcp 查看
  Claude Desktop: 重启应用
  Cursor:         重启应用
  Codex:          重启会话
工具：search（检索）/ fetch（检索+下载+参考文献）/ download（单篇）
命令行也可直接用：~/.litfetch/litfetch.py search "关键词"
EOF
