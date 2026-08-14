#!/bin/bash
# 安装 subtitle-gateway 为 systemd 服务 (仅 Linux)
# 用法:
#   sudo ./install-systemd.sh [--cache-dir /path/to/models_cache] [--user]
# 前置: 已在本仓库根运行过 ./setup.sh (venv 就绪)
set -euo pipefail
cd "$(dirname "$0")"

if [ "$(uname -s)" != "Linux" ]; then
  echo "systemd 仅支持 Linux (当前: $(uname -s))。macOS 用 ./run.sh 启动。"
  exit 1
fi

UNIT="subtitle-gateway"
REPO_DIR="$(cd .. && pwd)"
USER_NAME="$(id -un)"
CACHE_LINE=""
MODE="system"
ARGS=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --cache-dir) CACHE_LINE="Environment=SUBTITLE_GATEWAY_CACHE_DIR=$2"; shift 2 ;;
    --user) MODE="user"; shift ;;
    *) ARGS+=("$1"); shift ;;
  esac
done

# 校验 venv 存在
if [ ! -x "$REPO_DIR/.venv/bin/python" ]; then
  echo "错误: $REPO_DIR/.venv/bin/python 不存在, 请先在仓库根运行 ./setup.sh"
  exit 1
fi

sed -e "s|__USER__|$USER_NAME|g" \
    -e "s|__REPO_DIR__|$REPO_DIR|g" \
    -e "s|# __CACHE_LINE__.*|$CACHE_LINE|" \
    subtitle-gateway.service > "/tmp/$UNIT.service"

if [ "$MODE" = "user" ]; then
  DEST="$HOME/.config/systemd/user/$UNIT.service"
  mkdir -p "$(dirname "$DEST")"
  cp "/tmp/$UNIT.service" "$DEST"
  systemctl --user daemon-reload
  systemctl --user enable --now "$UNIT"
  echo "已安装用户级服务: $DEST (systemctl --user status $UNIT)"
else
  DEST="/etc/systemd/system/$UNIT.service"
  if [ "$(id -u)" -ne 0 ]; then
    echo "需要 root 权限写入 $DEST, 请用 sudo ./install-systemd.sh 重试"
    exit 1
  fi
  cp "/tmp/$UNIT.service" "$DEST"
  systemctl daemon-reload
  systemctl enable --now "$UNIT"
  echo "已安装系统级服务: $DEST (systemctl status $UNIT)"
fi
