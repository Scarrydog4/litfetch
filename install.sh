#!/bin/bash
# litfetch 一行命令安装器（配合 GitHub 仓库使用）
# 标准用法:  curl -fsSL https://cdn.jsdelivr.net/gh/Scarrydog4/litfetch@main/install.sh | bash
# 备用源:    把上面域名换成 https://raw.githubusercontent.com/Scarrydog4/litfetch/main/install.sh
# 带参数:    curl -fsSL <上面的地址> | bash -s -- --only zcode,cursor --test
set -euo pipefail

BASE="${LITFETCH_BASE:-https://cdn.jsdelivr.net/gh/Scarrydog4/litfetch@main}"
DEST="${LITFETCH_DEST:-$HOME/.litfetch}"

say() { printf '[install] %s\n' "$*"; }

TMP="$(mktemp -d /tmp/litfetch-pkg.XXXXXX)"
trap 'rm -rf "$TMP"' EXIT

say "从 $BASE 拉取文件"
for f in litfetch.py mcp_server.py setup.sh README.md; do
  curl -fsSL --retry 2 "$BASE/$f" -o "$TMP/$f" || {
    echo "[install] 拉取失败，试试备用源：" >&2
    echo "  LITFETCH_BASE=https://raw.githubusercontent.com/Scarrydog4/litfetch/main bash <(curl -fsSL https://raw.githubusercontent.com/Scarrydog4/litfetch/main/install.sh)" >&2
    exit 1
  }
done

bash "$TMP/setup.sh" "$@"

if [ ! -f "$DEST/session.json" ]; then
  echo
  say "还差最后一步：把发你的 session.json 放进 $DEST/"
  say "（找不到文件夹就在终端执行: open $DEST 拖进去即可；没有 session.json 则只有命令行框架，无法检索下载）"
  command -v open >/dev/null 2>&1 && open "$DEST" 2>/dev/null || true
fi
