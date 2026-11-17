#!/bin/bash
cd "$(dirname "$0")"
UV="$HOME/.local/bin/uv"
[ -x "$UV" ] || UV="$(command -v uv)"
if [ -z "$UV" ]; then
  echo "找不到 uv，請重新執行安裝指令。"
  read -n 1 -s -r -p "按任意鍵關閉視窗…"
  echo
  exit 1
fi
# 金鑰存在 ~/.zprofile 的助理區塊；只取這一段載入，不載入整個檔案
if [ -z "$GEMINI_API_KEY" ] && [ -f "$HOME/.zprofile" ]; then
  eval "$(sed -n '/^# >>> teaching-assistant >>>$/,/^# <<< teaching-assistant <<<$/p' "$HOME/.zprofile")"
fi
exec "$UV" run python -m xiaoshen
