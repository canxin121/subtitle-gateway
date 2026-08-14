#!/bin/bash
# 安装 subtitle-gateway 为 macOS launchd LaunchAgent (开机自启 + 崩溃自动重启)
# 用法:
#   ./launchd/install-macos.sh [--device auto] [--port 8000] [--cache-dir <dir>]
#   ./launchd/install-macos.sh --uninstall
# 前置: 已在本仓库根运行过 ./setup.sh (venv 就绪)
set -euo pipefail
cd "$(dirname "$0")/.."

if [ "$(uname -s)" != "Darwin" ]; then
  echo "错误: 这是 macOS launchd 安装脚本 (当前: $(uname -s))。Linux 请用 systemd/install-systemd.sh。" >&2
  exit 1
fi
if [ ! -x .venv/bin/python ]; then
  echo "错误: 未找到 .venv/bin/python, 请先在仓库根运行 ./setup.sh" >&2
  exit 1
fi

LABEL="com.subtitle-gateway"
REPO_DIR="$(pwd)"
# 日志固定放 ~/Library/Logs (系统卷)。仓库若在外部卷, launchd 服务首次访问
# 外部卷会触发 TCC 授权弹窗, 批准后即可正常访问 (未批准则 spawn 失败 78)。
LOG_DIR="$HOME/Library/Logs/subtitle-gateway"
DEVICE="auto"
PORT="8000"
CACHE_ARG=""
UNINSTALL=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --device) DEVICE="$2"; shift 2 ;;
    --port) PORT="$2"; shift 2 ;;
    --cache-dir) CACHE_ARG="<string>--cache-dir</string><string>$2</string>"; shift 2 ;;
    --uninstall) UNINSTALL=1; shift ;;
    *) echo "未知参数: $1" >&2; exit 1 ;;
  esac
done

PLIST_DEST="$HOME/Library/LaunchAgents/$LABEL.plist"

if [ "$UNINSTALL" = "1" ]; then
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
  rm -f "$PLIST_DEST"
  echo "已卸载 launchd 服务: $PLIST_DEST"
  exit 0
fi

# 端口占用检查: KeepAlive 会让启动失败的实例反复重试, 先提醒用户
if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "警告: 端口 :$PORT 已被占用, launchd 新实例会启动失败并反复重试。" >&2
  echo "      请先停掉占用进程 (lsof -nP -iTCP:$PORT -sTCP:LISTEN), 或改用 --port。" >&2
fi

mkdir -p "$HOME/Library/LaunchAgents" "$LOG_DIR"

sed -e "s|__REPO_DIR__|$REPO_DIR|g" \
    -e "s|__DEVICE__|$DEVICE|g" \
    -e "s|__PORT__|$PORT|g" \
    -e "s|__LOG_DIR__|$LOG_DIR|g" \
    -e "s|<!-- __CACHE_ARG__ -->|$CACHE_ARG|" \
    launchd/com.subtitle-gateway.plist > "$PLIST_DEST"

plutil -lint "$PLIST_DEST" || { echo "错误: 生成的 plist 校验失败" >&2; exit 1; }

# 先卸载旧实例(如有), 再加载新实例
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST_DEST"

echo "已安装并启动 launchd 服务:"
echo "  $PLIST_DEST"
echo "查看状态: launchctl print gui/$(id -u)/$LABEL"
echo "查看日志: tail -f $LOG_DIR/gateway.log"
echo "卸载:     ./launchd/install-macos.sh --uninstall"
