#!/bin/bash
# 以用户上下文后台守护运行 subtitle-gateway (start/stop/restart/status)。
#
# 为什么需要它: launchd 服务进程无法访问外部卷 (/Volumes/*, macOS TCC 限制) —
# 不能加载外部卷上的二进制、也不能写外部卷日志。仓库若在外部卷 (如本机
# /Volumes/Rc20), launchd 方案装不上, 用本脚本:
#   1. ./run-as-service.sh start      # 后台运行 (看门狗: 崩溃 5 秒后自动重启)
#   2. 开机自启: 系统设置 > 通用 > 登录项 > "+" 添加本脚本 (勾选启动)
#   3. ./run-as-service.sh status     # 查看状态 + /health
#   4. ./run-as-service.sh stop
#
# 仓库在系统卷 (如 ~/Projects/...) 时, 更推荐 launchd 方案:
#   ./launchd/install-macos.sh
set -euo pipefail
cd "$(dirname "$0")"

PIDFILE=".gateway.pid"
HEALTH_URL="http://127.0.0.1:8000/health"

is_running() {
  [ -f "$PIDFILE" ] || return 1
  local pid; pid="$(cat "$PIDFILE")"
  [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null
}

cmd_start() {
  if is_running; then echo "服务已在运行 (watchdog pid $(cat "$PIDFILE"))"; return 0; fi
  if [ ! -x .venv/bin/python ]; then
    echo "错误: 未找到 .venv/bin/python, 请先运行 ./setup.sh" >&2
    return 1
  fi
  # 看门狗循环: 服务退出后 5 秒自动重启。用户上下文可正常访问外部卷。
  nohup bash -c '
    cd "$1"; shift
    LOG="$PWD/logs/gateway.log"; ERR="$PWD/logs/gateway-error.log"
    mkdir -p "$PWD/logs"
    echo "[watchdog] starting: python -m gateway $*" >> "$LOG"
    while true; do
      .venv/bin/python -m gateway "$@" >> "$LOG" 2>> "$ERR"
      echo "[watchdog] gateway exited ($?), restarting in 5s" >> "$LOG"
      sleep 5
    done
  ' _ "$(pwd)" ${EXTRA[@]+"${EXTRA[@]}"} >/dev/null 2>&1 &
  echo $! > "$PIDFILE"
  echo "已启动 (watchdog pid $(cat "$PIDFILE"))"
  echo "日志: $(pwd)/logs/gateway.log (stderr: gateway-error.log)"
  echo "健康: $HEALTH_URL"
}

cmd_stop() {
  if ! is_running; then echo "未运行"; return 0; fi
  local pid; pid="$(cat "$PIDFILE")"
  # 先停服务子进程, 再停看门狗
  pkill -P "$pid" 2>/dev/null || true
  kill "$pid" 2>/dev/null || true
  rm -f "$PIDFILE"
  echo "已停止"
}

cmd_status() {
  if is_running; then
    echo "运行中 (watchdog pid $(cat "$PIDFILE"))"
    local gw; gw="$(pgrep -P "$(cat "$PIDFILE")" 2>/dev/null | head -1)"
    [ -n "${gw:-}" ] && echo "gateway 进程: $gw" || echo "gateway 进程: (未运行, 看门狗等待重启)"
    curl -s -m 2 "$HEALTH_URL" 2>/dev/null && echo || echo "(健康检查失败)"
  else
    echo "未运行"
  fi
}

case "${1:-start}" in
  start)   shift; EXTRA=("$@"); cmd_start ;;
  stop)    cmd_stop ;;
  restart) cmd_stop; sleep 1; cmd_start ;;
  status)  cmd_status ;;
  *)
    echo "用法: $0 [start|stop|restart|status] [-- gateway 参数...]"
    echo "示例: $0 start --port 8000 --device auto"
    exit 1
    ;;
esac
