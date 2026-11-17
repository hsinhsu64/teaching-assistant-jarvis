#!/bin/bash
# 教學助理賈維斯：macOS 一行安裝／更新（Mac 版測試中）
#   curl -fsSL https://raw.githubusercontent.com/hsinhsu64/teaching-assistant-jarvis/main/install.sh | bash
# 重跑同一行＝更新。已完成的步驟會略過；config.toml 與 tools/ 不會被覆蓋。不需要系統管理員權限。
# 發行前本機測試：SOURCE=/path/to/release.zip TAJ_HOME=/tmp/taj bash release/install.sh
#   SOURCE：用本機的程式壓縮檔代替 GitHub 下載；TAJ_HOME：安裝目錄（預設 ~/TeachingAssistantJarvis）。
# 必須相容 macOS 內建的 bash 3.2：不用 bash 4 才有的語法。
# 版本值由發行腳本從 release/versions.env 同步到這裡與 install.ps1；三處必須一致。
NODE_VERSION="26.13.1"
CLOUDFLARED_VERSION="2026.11.0"
OE_MCP_COMMIT="6c6b7e85bb98e9a6e85820485d917af423422843"

set -euo pipefail

SCRIPT_PATH="${BASH_SOURCE[0]:-}"
APP_NAME="教學助理賈維斯"
NET_HINT="確認網路連線後，重新執行同一行安裝指令（已完成的步驟會自動略過）。"
CLOSE_HINT="先結束教學助理（關掉它的終端機視窗），確認網路連線後，重新執行同一行安裝指令。"
STEP=0
STEP_NAME=""
STEP_HINT=""
STAGE=""
SHORTCUT_OK=1

# 從腳本旁的 versions.env 讀版本（只在以檔案執行時；curl | bash 時用上面的檔頭值）
load_versions_env() {
  local f key val
  if [ -z "$SCRIPT_PATH" ] || [ ! -f "$SCRIPT_PATH" ]; then return 0; fi
  f="$(cd "$(dirname "$SCRIPT_PATH")" && pwd)/versions.env"
  if [ ! -f "$f" ]; then return 0; fi
  while IFS='=' read -r key val; do
    case "$key" in
      NODE_VERSION|CLOUDFLARED_VERSION|OE_MCP_COMMIT) ;;
      *) continue ;;
    esac
    val="$(printf '%s' "$val" | tr -d '\r\t ')"
    case "$val" in
      ''|*[!A-Za-z0-9._-]*) continue ;;
    esac
    printf -v "$key" '%s' "$val"
  done < "$f"
}

on_exit() {
  local code=$?
  if [ -n "$STAGE" ]; then rm -rf "$STAGE" 2>/dev/null || true; fi
  if [ "$code" -ne 0 ] && [ "$STEP" -gt 0 ]; then
    echo >&2
    echo "第 $STEP 步：${STEP_NAME}失敗，請${STEP_HINT}" >&2
  fi
}

step() {
  STEP="$1"
  STEP_NAME="$2"
  STEP_HINT="${3:-$NET_HINT}"
  echo
  echo "[$STEP/10] $STEP_NAME"
}

skip() {
  echo "  已完成，略過（$1）"
}

download() {
  echo "  下載 $1"
  curl -fL --retry 3 --connect-timeout 20 --progress-bar -o "$2" "$1"
}

marker_is() {
  [ -f "$1" ] && [ "$(cat "$1")" = "$2" ]
}

# 單一頂層資料夾的壓縮檔（GitHub 的 zip、Node 的 tar.gz）傳回那個資料夾，否則傳回解壓目錄本身
archive_root() {
  local entries
  entries="$(ls -A "$1")"
  if [ "$(printf '%s\n' "$entries" | wc -l | tr -d ' ')" = "1" ] && [ -d "$1/$entries" ]; then
    printf '%s\n' "$1/$entries"
  else
    printf '%s\n' "$1"
  fi
}

find_uv() {
  UV=""
  if [ -x "$HOME/.local/bin/uv" ]; then
    UV="$HOME/.local/bin/uv"
  elif command -v uv >/dev/null 2>&1; then
    UV="$(command -v uv)"
  fi
}

run_npm() {
  local dir="$1"
  shift
  (cd "$dir" && PATH="$TOOLS/node/bin:$PATH" "$TOOLS/node/bin/npm" "$@" </dev/null)
}

sq() {
  printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")"
}

# 最後一句：桌面捷徑沒建成時，改叫人雙擊安裝資料夾裡的 start.command
finish_message() {
  echo
  if [ "$SHORTCUT_OK" = "1" ]; then
    echo "安裝完成！雙擊桌面上的「${APP_NAME}」就能啟動。"
  else
    echo "安裝完成！請雙擊安裝資料夾裡的 start.command 啟動：$INSTALL_DIR/start.command"
  fi
  echo "第一次啟動會跳出設定視窗；Mac 會詢問終端機能否使用麥克風，請按允許。"
}

has_key() {
  local block=""
  if [ -n "${GEMINI_API_KEY:-}" ]; then return 0; fi
  if [ -f "$HOME/.zprofile" ]; then
    block="$(sed -n '/^# >>> teaching-assistant >>>$/,/^# <<< teaching-assistant <<<$/p' "$HOME/.zprofile")"
  fi
  case "$block" in
    *"export GEMINI_API_KEY="*) return 0 ;;
  esac
  return 1
}

main() {
  load_versions_env
  trap on_exit EXIT

  if [ "$(uname -s)" != "Darwin" ]; then
    echo "這支安裝腳本只支援 macOS；Windows 請改用 PowerShell 的安裝指令。" >&2
    exit 1
  fi
  local node_arch cf_arch
  case "$(uname -m)" in
    arm64|aarch64) node_arch="arm64"; cf_arch="arm64" ;;
    x86_64) node_arch="x64"; cf_arch="amd64" ;;
    *) echo "不支援這種處理器：$(uname -m)" >&2; exit 1 ;;
  esac

  INSTALL_DIR="${TAJ_HOME:-$HOME/TeachingAssistantJarvis}"
  TOOLS="$INSTALL_DIR/tools"
  mkdir -p "$INSTALL_DIR" "$TOOLS"
  STAGE="$INSTALL_DIR/.install-staging"
  rm -rf "$STAGE"
  mkdir -p "$STAGE"
  echo "安裝${APP_NAME}到：$INSTALL_DIR"

  # STEPS-BEGIN：順序固定（設計文件 §8.2）
  step 1 "安裝 uv"
  find_uv
  if [ -n "$UV" ]; then
    skip "$UV"
  else
    download "https://astral.sh/uv/install.sh" "$STAGE/uv-install.sh"
    sh "$STAGE/uv-install.sh" </dev/null
    find_uv
    if [ -z "$UV" ]; then echo "  安裝後仍找不到 uv" >&2; exit 1; fi
  fi

  step 2 "下載程式" "$CLOSE_HINT"
  local zip sum root item name
  if [ -n "${SOURCE:-}" ]; then
    if [ ! -f "$SOURCE" ]; then echo "  找不到 SOURCE 指定的檔案：$SOURCE" >&2; exit 1; fi
    zip="$SOURCE"
    echo "  使用本機壓縮檔 $zip"
  else
    zip="$STAGE/program.zip"
    download "https://github.com/hsinhsu64/teaching-assistant-jarvis/archive/refs/heads/main.zip" "$zip"
  fi
  sum="$(shasum -a 256 "$zip" | cut -d ' ' -f 1)"
  if [ -f "$INSTALL_DIR/start.command" ] && marker_is "$INSTALL_DIR/.program.sha256" "$sum"; then
    skip "程式已是最新版"
  else
    mkdir -p "$STAGE/program"
    unzip -q "$zip" -d "$STAGE/program"
    root="$(archive_root "$STAGE/program")"
    if [ ! -f "$root/pyproject.toml" ]; then echo "  壓縮檔裡找不到程式" >&2; exit 1; fi
    # 更新時保留 config.toml、tools/（和 .venv），其他頂層項目整個換新
    for item in "$root"/* "$root"/.[!.]*; do
      if [ ! -e "$item" ]; then continue; fi
      name="$(basename "$item")"
      case "$name" in
        config.toml|tools|.venv|.install-staging|.program.sha256) continue ;;
      esac
      rm -rf "${INSTALL_DIR:?}/$name"
      mv "$item" "$INSTALL_DIR/$name"
    done
    chmod +x "$INSTALL_DIR/start.command" "$INSTALL_DIR/set-key.command"
    printf '%s\n' "$sum" > "$INSTALL_DIR/.program.sha256"
  fi

  step 3 "安裝 Python 套件（uv sync）" "$CLOSE_HINT"
  (cd "$INSTALL_DIR" && "$UV" sync </dev/null)

  step 4 "下載 cloudflared"
  if [ -x "$TOOLS/cloudflared" ] && marker_is "$TOOLS/cloudflared.version" "$CLOUDFLARED_VERSION"; then
    skip "cloudflared $CLOUDFLARED_VERSION"
  else
    mkdir -p "$STAGE/cf"
    download "https://github.com/cloudflare/cloudflared/releases/download/$CLOUDFLARED_VERSION/cloudflared-darwin-$cf_arch.tgz" "$STAGE/cf/cf.tgz"
    tar -xzf "$STAGE/cf/cf.tgz" -C "$STAGE/cf"
    if [ ! -f "$STAGE/cf/cloudflared" ]; then echo "  壓縮檔裡找不到 cloudflared" >&2; exit 1; fi
    chmod +x "$STAGE/cf/cloudflared"
    mv -f "$STAGE/cf/cloudflared" "$TOOLS/cloudflared"
    printf '%s\n' "$CLOUDFLARED_VERSION" > "$TOOLS/cloudflared.version"
  fi

  step 5 "下載 Node.js"
  if [ -x "$TOOLS/node/bin/node" ] && marker_is "$TOOLS/node.version" "$NODE_VERSION"; then
    skip "Node.js $NODE_VERSION"
  else
    mkdir -p "$STAGE/node"
    download "https://nodejs.org/dist/v$NODE_VERSION/node-v$NODE_VERSION-darwin-$node_arch.tar.gz" "$STAGE/node.tar.gz"
    tar -xzf "$STAGE/node.tar.gz" -C "$STAGE/node"
    root="$(archive_root "$STAGE/node")"
    if [ ! -x "$root/bin/node" ]; then echo "  壓縮檔裡找不到 node" >&2; exit 1; fi
    rm -f "$TOOLS/node.version"
    rm -rf "$TOOLS/node"
    mv "$root" "$TOOLS/node"
    printf '%s\n' "$NODE_VERSION" > "$TOOLS/node.version"
  fi

  step 6 "下載並建置 OpenEvidence 轉接程式與擴充功能" "$CLOSE_HINT"
  local oe="$TOOLS/openevidence-mcp"
  if [ -f "$oe/dist/relay-daemon.js" ] && [ -f "$oe/extension/dist/manifest.json" ] \
      && marker_is "$TOOLS/openevidence-mcp.version" "$OE_MCP_COMMIT"; then
    skip "openevidence-mcp ${OE_MCP_COMMIT:0:7}"
  else
    mkdir -p "$STAGE/oe"
    download "https://github.com/htlin222/openevidence-mcp/archive/$OE_MCP_COMMIT.zip" "$STAGE/oe.zip"
    unzip -q "$STAGE/oe.zip" -d "$STAGE/oe"
    root="$(archive_root "$STAGE/oe")"
    if [ ! -f "$root/package.json" ]; then echo "  壓縮檔裡找不到 openevidence-mcp" >&2; exit 1; fi
    rm -f "$TOOLS/openevidence-mcp.version"
    rm -rf "$oe"
    mv "$root" "$oe"
    run_npm "$oe" install --no-audit --no-fund
    run_npm "$oe" run build
    run_npm "$oe/extension" install --no-audit --no-fund
    run_npm "$oe/extension" run build
    if [ ! -f "$oe/dist/relay-daemon.js" ] || [ ! -f "$oe/extension/dist/manifest.json" ]; then
      echo "  建置後找不到 relay-daemon.js 或 extension/dist" >&2
      exit 1
    fi
    printf '%s\n' "$OE_MCP_COMMIT" > "$TOOLS/openevidence-mcp.version"
  fi

  step 7 "下載喚醒詞模型"
  (cd "$INSTALL_DIR" && "$UV" run python scripts/get_wake_model.py </dev/null)

  step 8 "建立桌面捷徑" "重新執行同一行安裝指令；或直接雙擊安裝資料夾裡的 start.command 啟動。"
  local desk="$HOME/Desktop" shortcut content
  shortcut="$desk/$APP_NAME.command"
  content="#!/bin/bash
exec $(sq "$INSTALL_DIR/start.command")"
  if [ -f "$shortcut" ] && [ "$(cat "$shortcut")" = "$content" ]; then
    skip "$shortcut"
  elif ! { mkdir -p "$desk" && printf '%s\n' "$content" > "$shortcut" && chmod +x "$shortcut"; } 2>/dev/null; then
    # 捷徑只是方便：沒允許「終端機」取用桌面時不中止，繼續設定金鑰
    SHORTCUT_OK=0
    echo "  無法在桌面建立捷徑（可能是沒有允許「終端機」取用桌面）。之後請直接雙擊安裝資料夾裡的 start.command 啟動：$INSTALL_DIR/start.command" >&2
  else
    chmod +x "$shortcut"
  fi

  step 9 "設定 Gemini 金鑰"
  if has_key; then
    skip "已有 GEMINI_API_KEY"
  elif { : < /dev/tty; } 2>/dev/null \
      && (cd "$INSTALL_DIR" && "$UV" run python -m xiaoshen.keysetup < /dev/tty); then
    :
  else
    echo "  金鑰沒有設定成功；之後可以雙擊安裝資料夾裡的 set-key.command 再設定。"
  fi

  step 10 "打開 OpenEvidence 擴充功能說明"
  echo "  擴充功能資料夾（在 Chrome 或 Edge「載入未封裝項目」時選這個）："
  echo "  $oe/extension/dist"
  open "https://github.com/hsinhsu64/teaching-assistant-jarvis#openevidence-%E6%93%B4%E5%85%85%E5%8A%9F%E8%83%BD" \
    >/dev/null 2>&1 || echo "  請在瀏覽器打開說明：https://github.com/hsinhsu64/teaching-assistant-jarvis#openevidence-%E6%93%B4%E5%85%85%E5%8A%9F%E8%83%BD"

  STEP=0
  finish_message
}

main "$@"
