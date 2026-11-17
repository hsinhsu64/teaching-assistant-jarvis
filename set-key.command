#!/bin/bash
cd "$(dirname "$0")"
UV="$HOME/.local/bin/uv"
[ -x "$UV" ] || UV="$(command -v uv)"
if [ -z "$UV" ]; then
  echo "找不到 uv，請重新執行安裝指令。"
else
  "$UV" run python -m xiaoshen.keysetup
fi
read -n 1 -s -r -p "按任意鍵關閉視窗…"
echo
